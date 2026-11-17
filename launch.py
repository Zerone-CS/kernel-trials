#!/usr/bin/env python3
"""Prepare or resume a public workspace through a scoped judge launcher."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from assigned import _atomic_write, _safe_path


class LaunchClientError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise LaunchClientError("LAUNCH_NOT_CONFIGURED", "The project launcher is not configured")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise LaunchClientError("INVALID_LAUNCH_CONFIG", "Expected a configuration object")
    return value


def _save(path, value):
    _atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(), mode=0o600)


def _forward(config, root):
    """Restore the administrator-configured local SSH tunnel, when needed."""
    setting = config.get("ssh_forward")
    if not isinstance(setting, dict):
        return False
    endpoint = urllib.parse.urlsplit(config["endpoint"])
    local, remote, host = setting.get("local_port"), setting.get("remote_port"), setting.get("host")
    if (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.port != local
            or type(local) is not int or type(remote) is not int
            or not 1 <= local <= 65535 or not 1 <= remote <= 65535
            or not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host)):
        raise LaunchClientError("INVALID_LAUNCH_CONFIG", "Invalid administrator SSH forwarding configuration")
    try:
        with socket.create_connection(("127.0.0.1", local), timeout=1):
            return True
    except OSError:
        pass
    log_path = _safe_path(root, ".eval/launcher-tunnel.log")
    descriptor = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as log:
        process = subprocess.Popen([
            "ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "-N", "-L", "127.0.0.1:" + str(local) + ":127.0.0.1:" + str(remote), host,
        ], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(50):
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", local), timeout=.2):
                _save(_safe_path(root, ".eval/launcher-tunnel.json"), {"pid": process.pid, "port": local})
                return True
        except OSError:
            time.sleep(.1)
    process.terminate()
    return False


def _post(config, action, body, root):
    launcher = config.get("launcher_id")
    if not isinstance(launcher, str) or not re.fullmatch(r"[0-9a-f]{32}", launcher):
        raise LaunchClientError("INVALID_LAUNCH_CONFIG", "Invalid launcher identity")
    url = config["endpoint"].rstrip("/") + "/v1/launchers/" + launcher + "/" + action
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + config["token"],
    })
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=1800 if action == 'experiment_finish' else 90) as response:
                data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise LaunchClientError("INVALID_LAUNCH_RESPONSE", "Workspace response exceeds size limit")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError("not an object")
            return result
        except urllib.error.HTTPError as error:
            try:
                result = json.loads(error.read(65536))
            except ValueError:
                result = {}
            raise LaunchClientError(result.get("error", "JUDGE_UNAVAILABLE"),
                                    result.get("message", "Judge launch request failed")) from None
        except (urllib.error.URLError, OSError):
            if attempt == 0 and _forward(config, root):
                continue
            raise LaunchClientError("JUDGE_UNAVAILABLE", "Judge is unavailable. Re-run the same launcher command to retry the saved request; no new budget is created by retry") from None
        except ValueError as error:
            if isinstance(error, LaunchClientError):
                raise
            raise LaunchClientError("INVALID_LAUNCH_RESPONSE", "Judge returned an invalid response; retry preserves the request identity") from None


def _materialize(root, config, response, track):
    session_id = response.get("session_id")
    credentials = response.get("credentials", {})
    session = response.get("session", {})
    if (not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id)
            or response.get("track") != track or session.get("track") != track
            or session.get("session_id") != session_id
            or credentials.get("session_id") != session_id
            or not isinstance(credentials.get("token"), str) or not credentials["token"]
            or type(response.get("attempt_index")) is not int or response["attempt_index"] < 1):
        raise LaunchClientError("INVALID_LAUNCH_RESPONSE", "Workspace identity does not match the requested track")
    destination = _safe_path(root, "workspaces/" + track + "_" + session_id)
    connection = dict(credentials, endpoint=config["endpoint"])
    if destination.exists():
        current = _read(_safe_path(root, destination.relative_to(root).as_posix() + "/.eval/session.json"))
        if any(current.get(key) != connection[key] for key in ("session_id", "token")):
            raise LaunchClientError("WORKSPACE_CONFLICT", "Existing workspace belongs to another session")
        # Never restore the starter over an Agent's edited kernel when resuming.
        if current.get("endpoint") != config["endpoint"]:
            _save(destination / ".eval/session.json", connection)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".launch-", dir=str(destination.parent)))
        try:
            files = response.get("files")
            if not isinstance(files, list):
                raise LaunchClientError("INVALID_LAUNCH_RESPONSE", "Missing public workspace files")
            names, total = set(), 0
            for item in files:
                name = item["path"]
                if name in names or name in (".eval/session.json", "RUN.json"):
                    raise LaunchClientError("INVALID_LAUNCH_RESPONSE", "Duplicate or reserved bundle path")
                path = _safe_path(temporary, name)
                data = base64.b64decode(item["content_b64"], validate=True)
                total += len(data)
                if total > 16 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != item["sha256"]:
                    raise LaunchClientError("INVALID_LAUNCH_RESPONSE", "Invalid public workspace file digest or size")
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(data)
                path.chmod(0o755 if item.get("executable") is True else 0o644)
                names.add(name)
            if not {"kernel.asc", "eval", "TASK.md", "OP_EVAL_WORKFLOW.md"} <= names:
                raise LaunchClientError("INVALID_LAUNCH_RESPONSE", "Incomplete public workspace bundle")
            _save(temporary / ".eval/session.json", connection)
            os.rename(str(temporary), str(destination))
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    status = session["state"]
    pending = session.get("pending_operation_id")
    next_action = "read_report" if status in ("FINISHED", "EXPIRED", "CANCELLED") else (
        "resume_operation" if pending or status == "FINALIZING" else "run_workflow")
    result = {"workspace": str(destination), "track": track, "session_id": session_id,
              "attempt_index": response["attempt_index"], "launcher_id": config["launcher_id"],
              "status": status, "reused": response.get("reused", False), "next_action": next_action,
              "pending_operation_id": pending, "report_path": str(destination / "REPORT.md"),
              "evaluation_purpose": session.get("claimed_agent_metadata", {}).get("evaluation_purpose"),
              "budget": session.get("policy"), "feedback_used": session.get("feedback_used")}
    _save(destination / "RUN.json", result)
    return result


def open_workspace(project_root, track="from_scratch", task_id="full"):
    root = Path(project_root).resolve()
    if track not in ("from_scratch", "optimize"):
        raise LaunchClientError("INVALID_TRACK", "Choose from_scratch or optimize")
    if task_id != 'full' and not re.fullmatch(r'case(?:0[1-9]|1[0-5])', task_id):
        raise LaunchClientError('INVALID_TASK', 'Choose full or case01..case15')
    config = _read(_safe_path(root, ".eval/launcher.json"))
    lock = _safe_path(root, ".eval/launch-client.lock")
    descriptor = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        suffix = track if task_id == 'full' else task_id + '-' + track
        pending = _safe_path(root, ".eval/launch-pending-" + suffix + ".json")
        if pending.exists():
            request = _read(pending)
            if (request.get("launcher_id") != config["launcher_id"] or request.get("track") != track
                    or request.get('task_id', 'full') != task_id):
                raise LaunchClientError("PENDING_LAUNCH_CONFLICT", "Unresolved launch belongs to a different configuration")
        else:
            request = {"launcher_id": config["launcher_id"], "track": track, "request_id": uuid.uuid4().hex, 'task_id': task_id}
            _save(pending, request)
        body = {key: request[key] for key in ("track", "request_id")}
        if task_id != 'full':
            body['task_id'] = task_id
        response = _post(config, 'open' if task_id == 'full' else 'open_task', body, root)
        result = _materialize(root, config, response, track)
        result['task_id'] = task_id
        _save(_safe_path(root, ".eval/launch-last-" + suffix + ".json"), result)
        pending.unlink()
        return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parent
    if argv and argv[0] == 'compare':
        from experiment import main as experiment_main
        try:
            return experiment_main(argv[1:], project_root=root)
        except (ValueError, OSError, RuntimeError, KeyError) as error:
            print(json.dumps({'error': getattr(error, 'code', 'EXPERIMENT_FAILED'), 'message': str(error)}, ensure_ascii=False), file=sys.stderr)
            return 1
    if argv and argv[0] == 'tasks':
        try:
            print(json.dumps(_post(_read(_safe_path(root, '.eval/launcher.json')), 'tasks', {}, root), ensure_ascii=False, indent=2))
            return 0
        except (ValueError, OSError) as error:
            print(json.dumps({'error': getattr(error, 'code', 'CATALOG_FAILED'), 'message': str(error)}), file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('selection', nargs='*', help='[full|case01..case15] [from_scratch|optimize]; or compare CONFIG / tasks')
    parser.add_argument("--history", action="store_true", help="Read all launcher attempts without allocating one")
    args = parser.parse_args(argv)
    tracks = [v for v in args.selection if v in ('from_scratch', 'optimize')]
    tasks = [v for v in args.selection if v not in ('from_scratch', 'optimize')]
    if len(tracks) > 1 or len(tasks) > 1:
        parser.error('Select one task and one track')
    track, task_id = (tracks or ['from_scratch'])[0], (tasks or ['full'])[0]
    try:
        result = (_post(_read(_safe_path(root, ".eval/launcher.json")), "history", {}, root)
                  if args.history else open_workspace(root, track, task_id))
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"error": getattr(error, "code", "LAUNCH_FAILED"), "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
