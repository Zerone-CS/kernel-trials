#!/usr/bin/env python3
"""Observe bare /op-eval creating its first workspace and finishing a CPU smoke.

This script never pre-allocates a session or invokes the launcher open endpoint.
An administrator must supply a fresh, CPU-only entry_smoke launcher project.
It does not change Claude configuration, model, or permission settings. Raw
events can contain private reasoning and credentials; retain them privately and
summarize only whitelisted metadata and independent judge observations.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from scripts.claude_smoke import (CLAUDE_CONFIG_DIR, _private_open, _read_judge,
                                  _sha, _terminate_group, _utc, _write_json,
                                  parse_claude_stream)


CPU_PROBLEM = "vector_add_cpu_example"
EXPECTED_TRACK = "from_scratch"


def _launcher_history(config):
    """Read the launcher inventory without allocating work or exposing tokens."""
    launcher_id = config.get("launcher_id")
    if not isinstance(launcher_id, str) or not re.fullmatch(r"[a-f0-9]{32}", launcher_id):
        raise RuntimeError("Invalid launcher identity")
    request = urllib.request.Request(
        config["endpoint"].rstrip("/") + "/v1/launchers/" + launcher_id + "/history",
        data=b"{}", method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + config["token"],
        })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError("Launcher history failed with HTTP " + str(error.code)) from None
    except (OSError, ValueError):
        raise RuntimeError("Launcher history failed: transport or invalid JSON") from None
    if (not isinstance(result, dict) or result.get("launcher_id") != launcher_id
            or not isinstance(result.get("attempts"), list)):
        raise RuntimeError("Launcher history identity or schema mismatch")
    return result


def _workspace_entries(project):
    directory = project / "workspaces"
    if directory.is_symlink() or directory.exists() and not directory.is_dir():
        raise RuntimeError("workspaces must be a real directory when present")
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def _find_workspace(project, session_id):
    """Accept exactly one materialized workspace, never follow arbitrary paths."""
    entries = _workspace_entries(project)
    if len(entries) != 1:
        raise RuntimeError("Expected exactly one newly created workspace")
    workspace = project / "workspaces" / entries[0]
    if workspace.is_symlink() or not workspace.is_dir() or not workspace.resolve().is_relative_to(project):
        raise RuntimeError("Workspace path is invalid")
    connection = workspace / ".eval" / "session.json"
    if connection.is_symlink() or not connection.is_file():
        raise RuntimeError("Workspace session configuration is unavailable")
    config = json.loads(connection.read_text())
    if not isinstance(config, dict) or config.get("session_id") != session_id:
        raise RuntimeError("Workspace belongs to another session")
    return workspace, config


def _copy_private(source, destination):
    if source.is_symlink() or not source.is_file():
        return False
    with _private_open(destination) as stream:
        stream.write(source.read_bytes())
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--claude-config-dir", default=CLAUDE_CONFIG_DIR, help="Override CLAUDE_CONFIG_DIR; otherwise inherit or use the CLI default")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 86400:
        parser.error("--timeout must be between 1 and 86400 seconds")
    project = Path(args.project).resolve()
    output = Path(args.output).absolute()
    if not project.is_dir():
        parser.error("--project must be an existing configured project")
    if output.exists() or output.is_symlink():
        parser.error("--output must be a new evidence directory")
    if output.resolve().is_relative_to(project / "workspaces"):
        parser.error("Evidence output must not occupy the project's workspace directory")
    try:
        config_path = project / ".eval" / "launcher.json"
        if config_path.is_symlink() or not config_path.is_file():
            raise RuntimeError("Project launcher configuration is missing")
        launcher = json.loads(config_path.read_text())
        if (not isinstance(launcher, dict) or launcher.get("evaluation_purpose") != "entry_smoke"
                or launcher.get("problem_id") != CPU_PROBLEM):
            raise RuntimeError("Administrator launcher configuration must identify CPU vector-add entry_smoke")
        entries_before = _workspace_entries(project)
        if entries_before or (project / "kernel.asc").exists() or (project / ".eval" / "session.json").exists():
            raise RuntimeError("Use a fresh project with no pre-created workspace or session")
        history_before = _launcher_history(launcher)
        if history_before["attempts"]:
            raise RuntimeError("Launcher history must be empty before the real command")
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        # Do not include configuration values or request headers in failures.
        parser.error(str(error) if isinstance(error, RuntimeError) else "Invalid launcher configuration or history")
    starter = REPOSITORY / "problems" / CPU_PROBLEM / "starters" / EXPECTED_TRACK / "kernel.asc"
    if not starter.is_file():
        parser.error("Repository CPU from_scratch starter is missing")
    if args.claude_config_dir and not Path(args.claude_config_dir).is_dir():
        parser.error("The user-requested CLAUDE_CONFIG_DIR does not exist")
    executable = shutil.which("claude")
    if executable is None:
        parser.error("claude is not on PATH")

    output.mkdir(parents=True, mode=0o700)
    output.chmod(0o700)
    expected_source = _sha(starter)
    command = [executable, "-p", "/op-eval", "--output-format", "stream-json", "--verbose"]
    _write_json(output / "meta.json", {
        "started_at": _utc(), "project": str(project), "command": command,
        "claude_config_dir": args.claude_config_dir, "timeout_seconds": args.timeout,
        "launcher_id": launcher["launcher_id"], "evaluation_purpose": "entry_smoke",
        "problem_id": CPU_PROBLEM, "requested_track": EXPECTED_TRACK,
        "expected_starter_sha256": expected_source, "workspace_entries_before": entries_before,
        "no_precreated_workspace": True, "model_and_permission_overrides": False,
        "raw_stream": "claude.jsonl", "stderr": "claude.stderr.log",
    })
    _write_json(output / "launcher_before.json", history_before)
    environment = dict(os.environ)
    if args.claude_config_dir:
        environment["CLAUDE_CONFIG_DIR"] = args.claude_config_dir
    process = None
    timed_out = interrupted = False
    error_type = None
    termination = []
    start = time.monotonic()
    with _private_open(output / "claude.jsonl") as stdout, _private_open(output / "claude.stderr.log") as stderr:
        try:
            process = subprocess.Popen(command, cwd=str(project), env=environment,
                                       stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            termination = _terminate_group(process)
        except KeyboardInterrupt:
            interrupted = True
            if process is not None:
                termination = _terminate_group(process)
        except OSError as error:
            error_type = type(error).__name__
            if process is not None and process.poll() is None:
                termination = _terminate_group(process)
    process_result = {
        "finished_at": _utc(), "exit_code": process.returncode if process else None,
        "timed_out": timed_out, "interrupted": interrupted,
        "elapsed_seconds": time.monotonic() - start,
        "termination_signals": termination, "launch_error_type": error_type,
    }
    _write_json(output / "process_result.json", process_result)
    observations = parse_claude_stream(output / "claude.jsonl")

    history_after = status = history = local_report = None
    workspace = None
    session_id = None
    judge_error = None
    report_exists = report_copied = json_copied = False
    source_after = None
    attempt = {}
    try:
        history_after = _launcher_history(launcher)
        _write_json(output / "launcher_after.json", history_after)
        attempts = history_after["attempts"]
        if len(attempts) != 1 or not isinstance(attempts[0], dict):
            raise RuntimeError("The command must allocate exactly one fresh session")
        attempt = attempts[0]
        session_id = attempt.get("session_id")
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise RuntimeError("Allocated session identity is invalid")
        workspace, connection = _find_workspace(project, session_id)
        status = _read_judge(connection, "status")
        history = _read_judge(connection, "history")
        _write_json(output / "judge_after.json", status)
        _write_json(output / "judge_history.json", history)
        source_path = workspace / "kernel.asc"
        if source_path.is_file() and not source_path.is_symlink():
            source_after = _sha(source_path)
        report_file = workspace / ".eval" / "report.json"
        markdown_file = workspace / "REPORT.md"
        if report_file.is_file() and not report_file.is_symlink():
            local_report = json.loads(report_file.read_text())
            json_copied = _copy_private(report_file, output / "report.json")
        report_exists = markdown_file.is_file() and not markdown_file.is_symlink()
        report_copied = _copy_private(markdown_file, output / "REPORT.md")
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        judge_error = str(error) if isinstance(error, RuntimeError) else type(error).__name__

    operations = (history or {}).get("operations", [])
    valid_operations = isinstance(operations, list) and all(isinstance(op, dict) for op in operations)
    operations = operations if valid_operations else []
    actions = [op.get("action") for op in operations]
    final_operations = [op for op in operations if op.get("action") == "final"]
    final_operation = final_operations[0] if len(final_operations) == 1 else {}
    final = final_operation.get("report") or {}
    final_id = (status or {}).get("final_operation_id")
    metadata = (status or {}).get("claimed_agent_metadata") or {}
    report_session = local_report.get("session", {}) if isinstance(local_report, dict) else {}
    report_result = local_report.get("result", {}) if isinstance(local_report, dict) else {}
    checks = {
        "no_precreated_workspace": not entries_before and not history_before["attempts"],
        "exactly_one_launcher_attempt": len((history_after or {}).get("attempts", [])) == 1,
        "first_attempt_from_scratch": attempt.get("attempt_index") == 1 and attempt.get("track") == EXPECTED_TRACK,
        "judge_identity_matches": bool(session_id and (status or {}).get("session_id") == session_id
                                       and (history or {}).get("session", {}).get("session_id") == session_id
                                       and all(op.get("session_id") == session_id for op in operations)),
        "judge_bound_entry_smoke": metadata.get("evaluation_purpose") == "entry_smoke",
        "judge_bound_launcher_attempt": metadata.get("launcher_id") == launcher["launcher_id"] and metadata.get("attempt_index") == 1,
        "session_finished": (status or {}).get("state") == "FINISHED" and attempt.get("state") == "FINISHED",
        "check_bench_final_once": actions == ["check", "bench", "final"] and Counter(actions) == {"check": 1, "bench": 1, "final": 1},
        "all_operations_finished": len(operations) == 3 and all(op.get("state") == "FINISHED" for op in operations),
        "only_two_feedback_attempts": (status or {}).get("feedback_used") == 2,
        "cpu_problem_verified": len(operations) == 3 and all((op.get("report") or {}).get("problem_id") == CPU_PROBLEM for op in operations),
        "starter_unchanged": source_after == expected_source and len(operations) == 3
                             and all(op.get("source_sha256") == expected_source for op in operations),
        "report_markdown_exists": report_exists and report_copied,
        "report_json_matches_session_and_final": bool(json_copied and final_id
            and final_operation.get("operation_id") == final_id
            and report_session.get("session_id") == session_id
            and report_session.get("launcher_id") == launcher["launcher_id"]
            and report_session.get("attempt_index") == 1
            and report_session.get("evaluation_purpose") == "entry_smoke"
            and report_result.get("final_operation_id") == final_id
            and report_result.get("source_sha256") == expected_source
            and report_result.get("outcome") == final.get("outcome")),
        "model_exit_zero": process_result["exit_code"] == 0 and not timed_out and not interrupted,
        "claude_initialized": observations["initialized"],
        "op_eval_discovered": observations["op_eval_discovered"],
        "model_result_not_error": observations["result_metadata"].get("is_error") is not True,
        "judge_reads_succeeded": judge_error is None,
    }
    summary = {
        "schema_version": 1, "evaluation_purpose": "entry_smoke", "problem_id": CPU_PROBLEM,
        "claim": "Real bare slash command allocated its first workspace and completed the CPU evaluator entry flow; not an agent ability or NPU performance result",
        "requested_entry": "/op-eval", **observations, "process": process_result,
        "project": str(project), "workspace": str(workspace) if workspace else None,
        "launcher_id": launcher["launcher_id"], "judge_session_id": session_id,
        "judge_state": (status or {}).get("state"), "judge_actions": actions,
        "judge_feedback_used": (status or {}).get("feedback_used"),
        "judge_final_outcome": final.get("outcome"), "judge_final_score": final.get("score"),
        "reported_task_score": report_result.get("score"),
        "expected_starter_sha256": expected_source, "kernel_sha256_after": source_after,
        "judge_read_error": judge_error, "checks": checks,
        "smoke_passed": all(checks.values()), "evidence_directory": str(output),
    }
    _write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2))
    return 0 if summary["smoke_passed"] else 130 if interrupted else 124 if timed_out else 1


if __name__ == "__main__":
    sys.exit(main())
