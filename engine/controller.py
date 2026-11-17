"""Trusted controller; exposes no candidate-side session creation or budget reset.

Development execution remains unisolated. All files under state_dir belong to
the judge. Keeping these inaccessible to an agent is a deployment requirement,
not something this Python interface can guarantee for a root agent.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import secrets
import threading

from .budget import BudgetError, BudgetStore, Policy
from .diagnostics import capability_profile, suggest_final_source
from .problems import load_problem
from .identity import measurement_engine_digest, protocol_sha256, npu_resource_id


def _write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
    path.chmod(0o600)


def _json_bytes(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2).encode()


class Controller:
    def __init__(self, state_dir, *, mode="development", clock=None, runner=None, database=None):
        self.root = Path(state_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.mode = mode
        location_file = self.root / "database.json"
        if location_file.exists():
            selected = Path(json.loads(location_file.read_text())["path"])
            if database is not None and Path(database).resolve() != selected.resolve():
                raise ValueError("Judge database is already bound to a different location")
            if not selected.is_absolute():
                raise ValueError("Judge database path must be absolute")
            if not selected.exists() and (self.root / "bindings").exists():
                raise ValueError("Authoritative database is missing; recover it instead of resetting budgets")
        else:
            selected = Path(database).resolve() if database is not None else self.root / "budget.sqlite"
            if database is not None:
                if (self.root / "budget.sqlite").exists():
                    raise ValueError("Existing database requires an explicit SQLite backup migration before rebinding")
                _write_new(location_file, _json_bytes({"schema_version": 1, "path": str(selected)}))
        self.store = BudgetStore(selected, **({"clock": clock} if clock else {}))
        if runner is None:
            from .execution import run_managed
            runner = run_managed
        self.runner = runner
        self._finalizing = set()
        self._finalizing_lock = threading.Lock()

    def _baseline(self, package, path=None):
        if npu_resource_id(package) is None:
            return None
        from .calibration import validate_baseline
        from .npu import npu_environment
        if path is None:
            pointer = self.root / "active_baselines" / (package.digest + ".json")
            if not pointer.is_file():
                raise ValueError("NPU reference must be calibrated with admin.py calibrate before creating a session")
            identity = json.loads(pointer.read_text())["baseline_id"]
            path = self.root / "baselines" / (identity + ".json")
        baseline = json.loads(Path(path).read_text())
        expected = [spec["case_id"] for spec in package.load_adapter().cases("public", 0)]
        return validate_baseline(baseline, package_digest=package.digest,
                                 environment=npu_environment(package),
                                 reference_sha256=hashlib.sha256(package.path("reference").read_bytes()).hexdigest(),
                                 engine_digest=measurement_engine_digest(), protocol_digest=protocol_sha256(package),
                                 expected_case_ids=expected)

    def admin_create(self, package_path, *, track, owner_id, resource_id="default", policy=None,
                     baseline_path=None, evaluation_purpose="agent_evaluation"):
        package = load_problem(package_path)
        if evaluation_purpose not in ("agent_evaluation", "entry_smoke"):
            raise ValueError("Unknown evaluation purpose")
        baseline = self._baseline(package, baseline_path)
        fixed_resource = npu_resource_id(package)
        if fixed_resource:
            if resource_id not in ("default", fixed_resource):
                raise ValueError("NPU resource id is fixed by the problem's device; it cannot bypass its lease")
            resource_id = fixed_resource
        registered = self.root / "packages" / package.digest
        if not registered.exists():
            registered.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(package.root, registered)
        package = load_problem(registered, expected_digest=package.digest)
        credentials = self.store.admin_create_session(
            problem_digest=package.digest, track=track, owner_id=owner_id,
            resource_id=resource_id, policy=policy or Policy(),
            claimed_agent_metadata={"evaluation_purpose": evaluation_purpose})
        starter = package.starter_path(track).read_bytes()
        seed_path = self.root / "campaign_seed.json"
        try:
            _write_new(seed_path, _json_bytes({"hidden_seed": secrets.randbits(63)}))
        except FileExistsError:
            pass
        hidden_seed = json.loads(seed_path.read_text())["hidden_seed"]
        baseline_id = baseline["baseline_id"] if baseline else None
        if baseline:
            baseline_file = self.root / "baselines" / (baseline_id + ".json")
            if not baseline_file.exists():
                _write_new(baseline_file, _json_bytes(baseline))
        _write_new(self.root / "bindings" / (credentials["session_id"] + ".json"), _json_bytes({
            "package_digest": package.digest, "mode": self.mode,
            "starter_sha256": hashlib.sha256(starter).hexdigest(),
            "baseline_id": baseline_id, "hidden_seed": hidden_seed,
            "evaluation_purpose": evaluation_purpose,
        }))
        return credentials

    def _bound(self, session_id, token):
        status = self.store.status(session_id, token)  # Authenticate before filesystem access.
        binding = json.loads((self.root / "bindings" / (session_id + ".json")).read_text())
        if binding["mode"] != self.mode:
            raise ValueError("Controller mode differs from the frozen session mode")
        package = load_problem(self.root / "packages" / status["problem_digest"],
                               expected_digest=status["problem_digest"])
        return status, binding, package

    def start(self, session_id, token):
        _, binding, package = self._bound(session_id, token)
        from .security import SecurityPolicy
        SecurityPolicy(self.mode).enforce(
            None, operator_profile=package.manifest.get("security", {}).get("operator_profile", "legacy_host_untrusted"),
            problem_digest=package.digest, backend_profile="development_subprocess")
        if binding.get("baseline_id"):
            self._baseline(package, self.root / "baselines" / (binding["baseline_id"] + ".json"))
        return self.store.start_session(session_id, token)

    def submit(self, session_id, token, *, request_id, action, source=None, source_sha256=None):
        _, binding, package = self._bound(session_id, token)
        if (source is None) == (source_sha256 is None):
            raise ValueError("Supply exactly one of source or a historical source_sha256")
        if source is None:
            if action != "final" or not re.fullmatch(r"[0-9a-f]{64}", source_sha256 or ""):
                raise ValueError("Historical hashes are accepted only for final")
            history = self.store.history(session_id, token)["operations"]
            if not any(op["source_sha256"] == source_sha256 for op in history):
                raise BudgetError("UNSUBMITTED_FINAL_SOURCE", "Source was not submitted in this session")
            data = (self.root / "sources" / session_id / (source_sha256 + ".asc")).read_bytes()
            if hashlib.sha256(data).hexdigest() != source_sha256:
                raise ValueError("Judge source snapshot integrity failure")
        else:
            if not isinstance(source, str):
                raise ValueError("source must be UTF-8 text")
            data = source.encode("utf8")
            if len(data) > package.manifest["candidate_policy"]["maximum_source_bytes"]:
                raise ValueError("Source exceeds package byte limit")
            source_sha256 = hashlib.sha256(data).hexdigest()
        operation = self.store.reserve(session_id, token, request_id=request_id, action=action,
                                       source_sha256=source_sha256)
        if not operation["created"]:
            return operation
        try:
            # Store only admitted sources. A crash between reserve and snapshot
            # is a lost worker/infrastructure failure, never another free try.
            snapshot = self.root / "sources" / session_id / (source_sha256 + ".asc")
            try:
                _write_new(snapshot, data)
            except FileExistsError:
                if snapshot.read_bytes() != data:
                    raise ValueError("Judge source snapshot integrity failure")
            kwargs = {"mode": self.mode}
            if npu_resource_id(package):
                kwargs.update(seed=binding["hidden_seed"], baseline=self._baseline(
                    package, self.root / "baselines" / (binding["baseline_id"] + ".json")))
            report = self.runner(package, data, action,
                                 self.root / "runs" / operation["operation_id"], **kwargs)
            report["evaluation_purpose"] = binding.get("evaluation_purpose", "agent_evaluation")
        except Exception as error:
            # Unexpected worker exceptions are not evidence that the kernel failed.
            # Persist the private exception locally; never leak judge internals in API reports.
            _write_new(self.root / "private_errors" / (operation["operation_id"] + ".txt"),
                       (type(error).__name__ + ": " + str(error)).encode("utf8", "replace"))
            report = {"outcome": "infrastructure_error", "error_category": "judge_worker_exception",
                      "stages": {k: "not_run" for k in ("build", "runtime", "correctness", "performance")},
                      "score": None}
        completed = self.store.complete_operation(operation["operation_id"], report)
        if action == "final":
            self._save_report(session_id, token)
        return completed

    def final_previous(self, session_id, token, *, request_id, source_sha256=None):
        if source_sha256 is None:
            choice = suggest_final_source(self.store.history(session_id, token)["operations"])
            if choice is None:
                raise BudgetError("NO_SUBMISSION", "No historical source is available for final")
            source_sha256 = choice["source_sha256"]
        return self.submit(session_id, token, request_id=request_id, action="final", source_sha256=source_sha256)

    def diagnostics(self, session_id, token):
        _, binding, _ = self._bound(session_id, token)
        history = self.store.history(session_id, token)
        return capability_profile(history["session"], history["operations"],
                                  starter_sha256=binding["starter_sha256"])

    def report(self, session_id, token):
        from .reporting import build_report, render_markdown
        history = self.store.history(session_id, token)
        report = build_report(history["session"], history["operations"], self.diagnostics(session_id, token))
        from .identity import protocol_sha256
        _, binding, package = self._bound(session_id, token)
        report['comparison']['protocol_digest'] = protocol_sha256(package)
        # A compile failure still belongs to the same frozen baseline/protocol.
        # Preserve that binding for paired failure-vs-success comparisons, while
        # leaving all unexecuted measurement fields empty.
        if binding.get('baseline_id'):
            baseline = json.loads((self.root / 'baselines' / (binding['baseline_id'] + '.json')).read_text())
            identity = report['comparison']['baseline_identity']
            for key in ('baseline_id', 'source_sha256', 'reference_sha256', 'package_digest',
                        'engine_digest', 'environment_fingerprint', 'protocol_digest'):
                if key in baseline:
                    identity.setdefault(key, baseline[key])
            report['comparison']['engine_digest'] = baseline.get('engine_digest')
            report['comparison']['bound_environment_fingerprint'] = (baseline.get('environment') or {}).get('fingerprint')
            identity.setdefault('environment_fingerprint', (baseline.get('environment') or {}).get('fingerprint'))
        report['session']['claimed_agent_metadata'] = history['session'].get('claimed_agent_metadata', {})
        report['comparison']['task_metadata'] = package.manifest.get('evaluation_task')
        return {"report": report, "markdown": render_markdown(report)}

    def _save_report(self, session_id, token):
        result = self.report(session_id, token)
        directory = self.root / "reports" / session_id
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in (("report.json", _json_bytes(result["report"])),
                              ("REPORT.md", result["markdown"].encode())):
            temporary = directory / (name + ".tmp")
            temporary.write_bytes(content)
            temporary.replace(directory / name)
        return result

    def autofinalize_due(self):
        """Schedule historical-source finals shortly before selection grace ends.

        Agent disappearance does not grant new code or another feedback attempt.
        Worker timeout remains enforced by the backend. No bearer is persisted.
        """
        from .budget import _ADMIN_FINALIZER
        for session_id in self.store.admin_open_sessions():
            # Poll small metadata, not every retained profiler sample from all
            # previous attempts. Load history only when a final is actually due.
            status = self.store.status(session_id, _ADMIN_FINALIZER)
            if status["state"] == "EXPIRED":
                self._save_report(session_id, _ADMIN_FINALIZER)
                continue
            deadline = status.get("final_deadline")
            if (status["state"] != "OPEN" or status.get("stop_at") is None
                    or status.get("pending_operation_id") or deadline is None
                    or deadline - status["server_time"] > 60):
                continue
            history = self.store.history(session_id, _ADMIN_FINALIZER)
            if not suggest_final_source(history["operations"]):
                continue
            with self._finalizing_lock:
                if session_id in self._finalizing:
                    continue
                self._finalizing.add(session_id)
            def finish(sid=session_id):
                try:
                    self.final_previous(sid, _ADMIN_FINALIZER, request_id="server-auto-final-" + secrets.token_hex(16))
                except BudgetError:
                    pass  # Another final, cancellation, or deadline won the race.
                except Exception as error:
                    path = self.root / "private_errors" / (sid + "-autofinal.txt")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(type(error).__name__ + ": " + str(error))
                finally:
                    with self._finalizing_lock:
                        self._finalizing.discard(sid)
            # A normal service shutdown must allow accepted work to finish and
            # be recorded; daemon threads could strand detached NPU processes.
            threading.Thread(target=finish, daemon=False).start()

    def dispatch(self, session_id, token, command, payload):
        if not isinstance(payload, dict):
            raise ValueError("Request must be an object")
        allowed = {
            "start": set(), "status": set(), "history": set(), "diagnostics": set(), "report": set(),
            "submit": {"request_id", "action", "source", "source_sha256"},
            "final_previous": {"request_id", "source_sha256"},
            "operation": {"operation_id"},
        }
        if command not in allowed or set(payload) - allowed[command]:
            raise ValueError("Unknown command or unsupported fields")
        if command == "start":
            return self.start(session_id, token)
        if command == "status":
            return self.store.status(session_id, token)
        if command == "history":
            return self.store.history(session_id, token)
        if command == "diagnostics":
            return self.diagnostics(session_id, token)
        if command == "report":
            return self.report(session_id, token)
        if command == "operation":
            return self.store.operation_status(session_id, token, **payload)
        if command == "final_previous":
            return self.final_previous(session_id, token, **payload)
        return self.submit(session_id, token, **payload)
