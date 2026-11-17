"""Trusted preparation CLI. Candidate clients cannot create or reset sessions."""
import argparse
import json
from pathlib import Path

from engine.budget import Policy
from engine.controller import Controller
from engine.problems import load_problem


from workspace import export_workspace, WORKFLOW
from engine.admin_ops import calibrate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "calibrate", "recover", "configure-launcher", "configure-tasks"))
    parser.add_argument("--state", required=True)
    parser.add_argument("--database", help="Node-local SQLite path; required when artifact state is on NFS")
    parser.add_argument("--mode", choices=("development", "formal"), default="formal")
    parser.add_argument("--problem")
    parser.add_argument("--track", choices=("from_scratch", "optimize"), default="from_scratch")
    parser.add_argument("--workspace")
    parser.add_argument("--project-root", help="Project receiving a one-time automatic launcher configuration")
    parser.add_argument("--baseline", help="Validated v2 baseline; default uses the calibrated baseline in judge state")
    parser.add_argument("--purpose", choices=("agent_evaluation", "entry_smoke"), default="agent_evaluation")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765")
    parser.add_argument("--owner", default="administrator-assigned")
    parser.add_argument("--resource", default="default")
    parser.add_argument("--max-feedback", type=int, default=20)
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--operation-id")
    parser.add_argument("--worker-terminated", action="store_true")
    parser.add_argument('--tasks', default='all', help='Subtask selectors for configure-tasks')
    parser.add_argument('--calibrate-missing', action='store_true', help='Measure a fresh baseline for each uncalibrated subtask')
    args = parser.parse_args()
    controller = Controller(args.state, mode=args.mode, database=args.database)
    if args.command == 'configure-tasks':
        if not args.problem or not args.project_root:
            parser.error('configure-tasks requires --problem and --project-root with an existing launcher')
        from engine.task_catalog import list_tasks, resolve_tasks, materialize_task
        from engine.launching import Launcher
        from engine.research import Research
        root = Path(args.project_root).resolve()
        config = json.loads((root / '.eval/launcher.json').read_text())
        research = Research(Launcher(controller))
        catalog = {t['id']: t for t in list_tasks()}
        results = []
        for task_id in resolve_tasks(args.tasks):
            if task_id == 'full':
                continue
            destination = controller.root / 'task_packages' / task_id
            package_path = materialize_task(Path(args.problem), task_id, destination)
            package = load_problem(package_path)
            try:
                controller._baseline(package)
            except ValueError:
                if not args.calibrate_missing:
                    raise
                print(json.dumps({'event': 'calibrating', 'task_id': task_id}), flush=True)
                calibrate(controller, package_path)
            result = research.admin_add_task(config['launcher_id'], config['token'], task_id, package_path, catalog[task_id])
            results.append(result)
            print(json.dumps({'event': 'task_ready', **result}), flush=True)
        print(json.dumps({'installed': results, 'sessions_created': 0,
                          'note': 'Reference calibration sessions are separate from Agent trials.'}), flush=True)
        return
    if args.command == "recover":
        if not args.operation_id or not args.worker_terminated:
            parser.error("Recovery requires an operation id and administrator confirmation that its worker has terminated")
        result = controller.store.complete_operation(args.operation_id, {
            "outcome": "infrastructure_error", "error_category": "worker_lost",
            "stages": {k: "not_run" for k in ("build", "runtime", "correctness", "performance")}, "score": None})
        print(json.dumps(result, indent=2))
        return
    if args.command == "calibrate":
        if not args.problem:
            parser.error("calibrate requires --problem")
        print(json.dumps(calibrate(controller, args.problem), ensure_ascii=False, indent=2))
        return
    if args.command == "configure-launcher":
        if not args.problem or not args.project_root:
            parser.error("configure-launcher requires --problem and --project-root")
        from assigned import _safe_path, _atomic_write
        from engine.launching import Launcher
        from launch_routes import install_launch_routes
        root = Path(args.project_root).resolve()
        if not (root / "launch.py").is_file():
            parser.error("Project root must contain launch.py")
        target = _safe_path(root, ".eval/launcher.json")
        if target.exists():
            parser.error("Launcher already configured; refusing to rotate credentials or hide previous runs")
        # Preflight and install managed entries before minting a credential.
        install_launch_routes(root)
        credentials = Launcher(controller).admin_register(
            args.problem, owner_id=args.owner,
            policy=Policy(max_feedback=args.max_feedback, active_seconds=args.seconds),
            evaluation_purpose=args.purpose)
        config = {key: credentials[key] for key in ("launcher_id", "token")}
        config["endpoint"] = args.endpoint
        _atomic_write(target, (json.dumps(config, indent=2) + "\n").encode(), mode=0o600)
        print(json.dumps({"launcher_id": credentials["launcher_id"], "project_root": str(root),
                          "policy": credentials["policy"], "evaluation_purpose": credentials["evaluation_purpose"],
                          "sessions_created": 0}, ensure_ascii=False, indent=2))
        return
    if not args.problem or not args.workspace:
        parser.error("create requires --problem and --workspace")
    if Path(args.workspace).exists():
        parser.error("Workspace already exists; refusing overwrite")
    credentials = controller.admin_create(args.problem, track=args.track, owner_id=args.owner,
                                          resource_id=args.resource, baseline_path=args.baseline,
                                          evaluation_purpose=args.purpose,
                                          policy=Policy(max_feedback=args.max_feedback, active_seconds=args.seconds))
    directory = export_workspace(controller, credentials, args.workspace, args.endpoint)
    print(json.dumps({"session_id": credentials["session_id"], "workspace": str(directory),
                      "state": "PENDING", "mode": args.mode, "clock_started": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
