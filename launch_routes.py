"""Install project-root agent entrypoints for automatic, idempotent launching.

The outer project and v2 project invoke the same v2 launch.py. This installer
does not create a session, call a service, or execute the launch script.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import uuid


_MANIFEST = ".eval/launch_routes.manifest.json"
_LEGACY_MANIFEST = ".eval/assignment_routes.manifest.json"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ENTRIES = (
    ".agents/skills/op-eval/SKILL.md",
    ".claude/skills/op-eval/SKILL.md",
    ".gemini/commands/op-eval.toml",
    ".opencode/commands/op-eval.md",
)


class LaunchRoutesError(ValueError):
    pass


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _safe_path(root, relative):
    if (not isinstance(relative, str) or not relative or relative.startswith("/")
            or "\\" in relative or any(ord(c) < 32 for c in relative)
            or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise LaunchRoutesError("Entry paths must be canonical project-relative paths")
    path = root
    parts = relative.split("/")
    for index, part in enumerate(parts):
        path = path / part
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise LaunchRoutesError("Refusing symlinked entry path: " + relative)
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            raise LaunchRoutesError("Entry parent is not a directory: " + relative)
    return path


def _read_managed(root, relative):
    path = _safe_path(root, relative)
    if not path.exists():
        return {}
    if not path.is_file():
        raise LaunchRoutesError("Installer ownership path is not a regular file")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise LaunchRoutesError("Cannot read installer ownership manifest: " + relative) from None
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("files"), dict):
        raise LaunchRoutesError("Invalid installer ownership manifest: " + relative)
    files = value["files"]
    for name, digest in files.items():
        _safe_path(root, name)
        if not isinstance(digest, str) or not _SHA.fullmatch(digest):
            raise LaunchRoutesError("Invalid managed entry digest")
    return files


def _atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def launch_instructions(script_relative):
    command = "python3 " + shlex.quote(script_relative)
    return """Run the requested operator evaluation or configured comparison through
the project launcher. Claude Code, Gemini CLI and OpenCode invoke /op-eval;
Codex invokes $op-eval. Do not ask the user to prepare sessions or workspaces.

Choose the mode from the user's arguments, preserving their task, track and
configuration. Run commands from this project root; do not change directories
before interpreting a relative comparison configuration path.

- No arguments: run `COMMAND` ONCE, selecting full and from_scratch.
- One evaluation: run `COMMAND case06 optimize`, for example. The optional task
  is full or case01 through case15; the optional track is from_scratch or
  optimize. Either order is accepted. Omitted task means full; omitted track
  means from_scratch. Single-shape tasks allow fixed-shape specialization and
  do not measure shape generalization. Do not select a different task to obtain
  more budget within one evaluation.
- tasks: run `COMMAND tasks`, show the returned task catalog and availability,
  then stop. Listing tasks does not allocate an evaluation.
- compare CONFIG: run `COMMAND compare CONFIG` ONCE and let its experiment driver
  complete the configured trials. CONFIG is the user-supplied file path relative
  to the invocation working directory (or an absolute path); shell-quote it as
  one argument. From the outer project a configuration may be
  op_eval_v2/experiments/knowledge_ab.json; from the v2 root it may be
  experiments/knowledge_ab.json. Both entrypoints share the same v2 launcher
  state. The driver starts a fresh Agent process for each trial and writes a
  comparison REPORT.md. The current conversation must not implement the trial
  kernels itself, share solutions across trials, or create/change conditions,
  methods, task selection, runner settings or budgets to improve the results.
  The driver already emits progress. Wait synchronously for that command, or
  poll that SAME command's execution handle if the tool returns one. Do not
  start a separate Monitor, persistent `tail -f`/`tail -F`, or background log
  watcher: these can outlive completed trials and prevent the Agent CLI from
  exiting. Do not rerun the command to poll progress. If the tool starts the
  command in the background or returns a task/job handle, use THAT tool's
  wait/result mechanism on the SAME handle until it reports successful process
  completion or an explicit failure. Reading a progress/output file once is
  NOT completion. Do not send a final assistant response while the driver or
  any configured trial is still running. A progress event, existing REPORT.md,
  or trial_started message is not the command's successful result.
  On success, require the driver's final result JSON with report_path and all
  configured trials terminal; then read that final report. On explicit driver
  failure, report the failure and incomplete trials without claiming completion
  or starting another run. Once the command returns its successful result JSON,
  read the returned report_path, deliver the comparison outcome
  and report, then finish this invocation without starting any additional run
  or monitoring task. Repeating the same config in a later user invocation
  resumes the existing experiment or reads its completed report. Add --new
  only when the USER explicitly requests a separate new experiment. Never use
  --new as an error recovery step. If the driver reports an unknown process
  outcome or an active experiment, inspect/wait for that existing run and report
  the concrete condition; do not launch a duplicate Agent process.

For one evaluation, launcher JSON contains the absolute workspace, task_id,
track, session_id, attempt_index, status, reused, next_action,
pending_operation_id and report_path. The launcher requests preparation from
the service and owns request-id persistence. Never implement session creation
yourself or modify its connection, request cache, quotas, or existing runs.

Use the returned workspace as the working directory for all subsequent steps:

- PENDING or OPEN with no pending operation and next_action=run_workflow:
  read OP_EVAL_WORKFLOW.md there and execute it through the final report.
- If pending_operation_id is set or next_action=resume_operation: do not submit
  another check, bench, or final. Read ./eval status. If .eval/pending.json exists,
  ./eval retry recovers that same request; otherwise wait and poll status, then
  read ./eval history to obtain the completed feedback before continuing the
  existing workflow. A pending final is awaited, not replaced.
- FINALIZING: wait for the existing final with ./eval status. Once it completes,
  run ./eval report and read REPORT.md. Do not launch another evaluation.
- FINISHED or EXPIRED, or next_action=read_report: run ./eval report and read the
  existing report. Do not reset or replace that run within this invocation.
- A launcher error: report its concrete reason. Only if launch itself reports
  transport uncertainty before a confirmed result, retry the same launch command;
  its persisted request_id must be reused. Do not remove the request cache.

After a workspace has been returned, do not call launch again during this
evaluation to obtain another budget. Use the workspace client for recovery and
reporting. A later, new USER invocation may request a fresh run after the earlier
one has ended; service policy, not the Agent, decides that allocation. Keep its
attempt_index in the delivered result so separate runs are not merged.

Only kernel.asc may be edited in the assigned project. Its start response decides
whether the purpose is agent_evaluation or bounded entry_smoke. Keep the server's
budget and stop rules, and deliver the final report path and actual outcome.
""".replace("COMMAND", command)


def install_launch_routes(project_root, script_relative="launch.py"):
    """Install four command files, preserving untracked/user-modified content.

    An unchanged file owned by assigned.install_assignment_routes may be adopted
    once. After adoption only this installer's current hash authorizes an update;
    the old assignment manifest cannot override a user's later edit.
    """
    supplied = Path(project_root).absolute()
    if supplied.is_symlink() or not supplied.is_dir():
        raise LaunchRoutesError("Project root must be an existing real directory")
    root = supplied.resolve()
    script = _safe_path(root, script_relative)
    if not script.is_file():
        raise LaunchRoutesError("Launch script must already exist: " + script_relative)
    instructions = launch_instructions(script_relative)
    front = ("---\nname: op-eval\n"
             "description: Run operator evaluations or configured multi-trial comparisons through the project launcher, with automatic session preparation, server budgets, and final reports.\n")
    files = {
        _ENTRIES[0]: (front + "---\n" + instructions).encode(),
        _ENTRIES[1]: (front + "disable-model-invocation: true\nargument-hint: '[full|case01..case15] [from_scratch|optimize] | tasks | compare CONFIG [--new]'\n---\n"
                      + instructions + "\nRequested arguments: $ARGUMENTS\n").encode(),
        _ENTRIES[2]: ('description = "Run an operator evaluation or configured comparison"\nprompt = '
                      + json.dumps(instructions + "\nRequested arguments: {{args}}\n") + "\n").encode(),
        _ENTRIES[3]: ("---\ndescription: Run an operator evaluation or configured comparison\n---\n"
                      + instructions + "\nRequested arguments: $ARGUMENTS\n").encode(),
    }
    managed = _read_managed(root, _MANIFEST)
    legacy = _read_managed(root, _LEGACY_MANIFEST)
    changes = []
    adopted = []
    for relative, data in files.items():
        path = _safe_path(root, relative)
        if path.exists() and not path.is_file():
            raise LaunchRoutesError("Managed entry is not a regular file: " + relative)
        current = path.read_bytes() if path.exists() else None
        if current == data:
            continue
        if current is not None:
            expected = managed.get(relative) if relative in managed else legacy.get(relative)
            if expected != _hash(current):
                raise LaunchRoutesError("Preserved an untracked or user-edited entry: " + relative)
            if relative not in managed:
                adopted.append(relative)
        changes.append((path, data))
    manifest = _safe_path(root, _MANIFEST)
    # All four paths and both manifests have been validated before any mutation.
    for path, data in changes:
        _atomic_write(path, data)
    record = {"schema_version": 1, "managed_by": "op_eval_v2_launch_routes",
              "script_relative": script_relative,
              "files": {name: _hash(data) for name, data in files.items()}}
    _atomic_write(manifest, (json.dumps(record, sort_keys=True, indent=2) + "\n").encode(), mode=0o600)
    return {"project_root": str(root), "script": str(script), "files_changed": len(changes),
            "adopted_assignment_entries": adopted}
