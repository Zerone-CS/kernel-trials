#!/usr/bin/env python3
"""Route project-root agent commands to administrator-assigned sessions.

This module never creates, starts, renews or resets a judge session. Installation
is a trusted administrator operation; the CLI performs a free status read and
prints the absolute workspace for one already assigned track.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import urllib.error
import urllib.request
import uuid


_TRACKS = ("from_scratch", "optimize")
_ASSIGNMENTS = ".eval/assignments.json"
_MANIFEST = ".eval/assignment_routes.manifest.json"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_SESSION = re.compile(r"[a-f0-9]{32}\Z")


class AssignmentError(ValueError):
    def __init__(self, message, *, code="ASSIGNMENT_INVALID", workspace=None, track=None):
        super().__init__(message)
        self.code, self.workspace, self.track = code, workspace, track


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf8")


def _safe_path(root, relative):
    if (not isinstance(relative, str) or not relative or relative.startswith("/") or "\\" in relative
            or any(ord(character) < 32 for character in relative)
            or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise AssignmentError("Assignment file paths must be canonical project-relative paths")
    path = root
    for index, part in enumerate(relative.split("/")):
        path = path / part
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise AssignmentError("Refusing symlinked assignment path: " + relative)
        if index < len(relative.split("/")) - 1 and not stat.S_ISDIR(mode):
            raise AssignmentError("Assignment path parent is not a directory: " + relative)
    return path


def _read_json_file(path):
    if not path.is_file() or path.is_symlink():
        raise AssignmentError("Required assignment file is missing or not a regular file: " + str(path))
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise AssignmentError("Cannot parse assignment metadata: " + str(path)) from None
    if not isinstance(value, dict):
        raise AssignmentError("Assignment metadata must be a JSON object")
    return value


def _root(path):
    candidate = Path(path).absolute()
    if candidate.is_symlink() or not candidate.is_dir():
        raise AssignmentError("Project root must be an existing real directory")
    return candidate.resolve()


def _workspace(root, value):
    if not isinstance(value, (str, Path)):
        raise AssignmentError("Assigned workspace must be a path")
    supplied = Path(value)
    if supplied.is_absolute():
        try:
            relative = supplied.relative_to(root).as_posix()
        except ValueError:
            raise AssignmentError("Assigned workspace must be inside the project") from None
    else:
        relative = str(value)
    path = _safe_path(root, relative)
    if not path.is_dir() or path == root:
        raise AssignmentError("Assigned workspace directory does not exist")
    session_path = _safe_path(root, relative + "/.eval/session.json")
    session = _read_json_file(session_path)
    if not isinstance(session.get("session_id"), str) or not _SESSION.fullmatch(session["session_id"]):
        raise AssignmentError("Workspace lacks a valid judge session identity")
    if not isinstance(session.get("endpoint"), str) or not isinstance(session.get("token"), str):
        raise AssignmentError("Workspace lacks its judge connection configuration")
    return path, relative, session


def _managed(root):
    path = _safe_path(root, _MANIFEST)
    if not path.exists():
        return {}
    value = _read_json_file(path)
    files = value.get("files")
    if value.get("schema_version") != 1 or not isinstance(files, dict):
        raise AssignmentError("Invalid assignment installer ownership manifest")
    for name, digest in files.items():
        _safe_path(root, name)
        if not isinstance(digest, str) or not _SHA.fullmatch(digest):
            raise AssignmentError("Invalid managed assignment digest")
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


ROOT_ENTRY = """Route this invocation to an administrator-assigned evaluation workspace.

No argument selects from_scratch. The argument optimize selects optimize;
from_scratch explicitly selects the default track. These are the two supported
project-root selections. Do not create a session or workspace yourself.

From the project root containing assigned.py, run `python3 assigned.py from_scratch`
(default) or `python3 assigned.py optimize`. The read-only resolver
prints JSON with the absolute workspace and track. If it reports a completed or
expired assignment, explain the reason and existing report path; a fresh
assignment must come from the administrator. If final is still running, wait
for that existing result. For a missing or unavailable assignment, report the
returned reason without inventing a new session.
Do not reset budgets, edit assignment metadata, or run admin.py to bypass it.

On success, use the returned directory as the working directory for subsequent
commands, read its OP_EVAL_WORKFLOW.md, and execute that workflow to final report.
The workspace's start response determines whether its administrator purpose is
agent_evaluation or entry_smoke. Only its kernel.asc may be edited. Preserve the
server's budget and stop rules. The project-root resolver does not start the clock.
"""


def install_assignment_routes(project_root, mapping):
    """Install managed root entry files for two existing assigned workspaces.

    All paths/content are preflighted before writes. Previously installed files
    can be updated only while their bytes match the installer's recorded hash.
    Existing identical files are safe to adopt; user-edited files are preserved.
    No bearer tokens are written to the root assignment map or ownership file.
    """
    root = _root(project_root)
    if not isinstance(mapping, dict) or set(mapping) != set(_TRACKS):
        raise AssignmentError("Assign exactly from_scratch and optimize workspaces")
    records = {}
    for track in _TRACKS:
        _, relative, session = _workspace(root, mapping[track])
        records[track] = {"workspace": relative, "session_id": session["session_id"]}
    if len({record["workspace"] for record in records.values()}) != len(_TRACKS) or len({record["session_id"] for record in records.values()}) != len(_TRACKS):
        raise AssignmentError("Each track requires a distinct assigned session/workspace")
    skill_front = "---\nname: op-eval\ndescription: Run the administrator-assigned operator evaluation from this project root; choose from_scratch or optimize without creating or resetting a session.\n"
    files = {
        _ASSIGNMENTS: _json_bytes({"schema_version": 1, "assignments": records}),
        ".agents/skills/op-eval/SKILL.md": (skill_front + "---\n" + ROOT_ENTRY).encode(),
        ".claude/skills/op-eval/SKILL.md": (skill_front + "disable-model-invocation: true\nargument-hint: '[from_scratch|optimize]'\n---\n" + ROOT_ENTRY + "\nRequested selection: $ARGUMENTS\n").encode(),
        ".gemini/commands/op-eval.toml": ('description = "Run the assigned operator evaluation"\nprompt = ' + json.dumps(ROOT_ENTRY + "\nRequested selection: {{args}}\n") + "\n").encode(),
        ".opencode/commands/op-eval.md": ("---\ndescription: Run the assigned operator evaluation\n---\n" + ROOT_ENTRY + "\nRequested selection: $ARGUMENTS\n").encode(),
    }
    old = _managed(root)
    changes = []
    for relative, data in files.items():
        path = _safe_path(root, relative)
        if path.exists() and not path.is_file():
            raise AssignmentError("Managed entry path is not a regular file: " + relative)
        current = path.read_bytes() if path.exists() else None
        if current == data:
            continue
        if current is not None and old.get(relative) != _hash(current):
            raise AssignmentError("Preserved an untracked or user-edited entry file: " + relative)
        changes.append((path, data))
    manifest_path = _safe_path(root, _MANIFEST)
    if manifest_path.exists() and not manifest_path.is_file():
        raise AssignmentError("Installer ownership path is not a regular file")
    for path, data in changes:
        _atomic_write(path, data, mode=0o600 if path.name == "assignments.json" else 0o644)
    _atomic_write(manifest_path, _json_bytes({"schema_version": 1,
                                           "files": {name: _hash(data) for name, data in files.items()}}), mode=0o600)
    return {"project_root": str(root), "assignments": records, "files_changed": len(changes)}


def _session_status(config):
    request = urllib.request.Request(
        config["endpoint"].rstrip("/") + "/v1/sessions/" + config["session_id"] + "/status",
        data=b"{}", method="POST", headers={"Content-Type": "application/json",
                                               "Authorization": "Bearer " + config["token"]})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.load(response)
    except urllib.error.HTTPError as error:
        raise AssignmentError("Judge status read failed with HTTP " + str(error.code), code="JUDGE_UNAVAILABLE") from None
    except (OSError, ValueError):
        raise AssignmentError("Judge status is unavailable; no session was started or reset", code="JUDGE_UNAVAILABLE") from None
    if not isinstance(value, dict):
        raise AssignmentError("Judge returned an invalid status", code="JUDGE_UNAVAILABLE")
    return value


def resolve_assignment(project_root, track="from_scratch"):
    root = _root(project_root)
    if track not in _TRACKS:
        raise AssignmentError("Track must be from_scratch or optimize")
    path = _safe_path(root, _ASSIGNMENTS)
    managed = _managed(root)
    if not path.is_file() or managed.get(_ASSIGNMENTS) != _hash(path.read_bytes()):
        raise AssignmentError("Assignments are missing or changed; ask the administrator to install a fresh assignment")
    value = _read_json_file(path)
    if value.get("schema_version") != 1 or not isinstance(value.get("assignments"), dict):
        raise AssignmentError("Invalid root assignment map")
    record = value["assignments"].get(track)
    if not isinstance(record, dict):
        raise AssignmentError("This track has no administrator assignment")
    workspace, _, config = _workspace(root, record.get("workspace"))
    if config["session_id"] != record.get("session_id"):
        raise AssignmentError("Workspace session differs from the administrator assignment")
    status = _session_status(config)
    if status.get("session_id") != config["session_id"] or status.get("track") != track:
        raise AssignmentError("Judge session identity or track differs from this assignment")
    if status.get("state") == "FINALIZING":
        raise AssignmentError("The assigned final is still running; wait for that result instead of creating or resetting a session",
                              code="ASSIGNMENT_FINALIZING", workspace=str(workspace), track=track)
    if status.get("state") not in ("PENDING", "OPEN"):
        raise AssignmentError("Assigned session is " + str(status.get("state"))
                              + "; ask the administrator for a fresh assignment. Existing report: "
                              + str(workspace / "REPORT.md"), code="ASSIGNMENT_CLOSED",
                              workspace=str(workspace), track=track)
    return {"workspace": str(workspace), "track": track}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("track", nargs="?", choices=_TRACKS, default="from_scratch")
    args = parser.parse_args(argv)
    try:
        result = resolve_assignment(Path(__file__).resolve().parent, args.track)
    except AssignmentError as error:
        result = {"error": error.code, "message": str(error)}
        if error.workspace:
            result.update(workspace=error.workspace, track=error.track)
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
