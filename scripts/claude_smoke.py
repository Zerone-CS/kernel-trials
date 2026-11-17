#!/usr/bin/env python3
"""Record one real, bare /op-eval smoke run without changing Claude settings.

Run this on the Ascend server after an administrator exports a fresh optimize
workspace with evaluation_purpose=entry_smoke. Raw model events stay private;
stdout contains only the structured, observable summary.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


CLAUDE_CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR")


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _private_open(path):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb")


def _write_json(path, value):
    with _private_open(path) as stream:
        stream.write((json.dumps(value, ensure_ascii=False, sort_keys=True,
                                 allow_nan=False, indent=2) + "\n").encode("utf8"))


def _read_judge(config, command):
    """Read-only public API; bearer is never copied into evidence or errors."""
    request = urllib.request.Request(
        config["endpoint"].rstrip("/") + "/v1/sessions/" + config["session_id"] + "/" + command,
        data=b"{}", method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + config["token"],
        })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError("Judge read failed with HTTP " + str(error.code)) from None
    except (OSError, ValueError):
        raise RuntimeError("Judge read failed: transport or invalid JSON") from None
    if not isinstance(result, dict):
        raise RuntimeError("Judge returned a non-object response")
    return result


def parse_claude_stream(path):
    """Whitelist metadata/tool names only; never return thinking or message text."""
    models, skills, slash_commands = set(), set(), set()
    counts = Counter()
    tool_ids = set()
    skill_called = False
    initialized = False
    result_metadata = {}
    malformed_lines = 0

    def names(values):
        if not isinstance(values, list):
            return []
        return [value if isinstance(value, str) else value.get("name")
                for value in values if isinstance(value, (str, dict))]

    with Path(path).open(encoding="utf8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                malformed_lines += 1
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                initialized = True
                if isinstance(event.get("model"), str):
                    models.add(event["model"])
                skills.update(name for name in names(event.get("skills")) if isinstance(name, str))
                slash_commands.update(name for name in names(event.get("slash_commands")) if isinstance(name, str))
            if event.get("type") == "assistant":
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                if isinstance(message.get("model"), str):
                    models.add(message["model"])
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name")
                    if not isinstance(name, str):
                        continue
                    identity = block.get("id")
                    if identity is not None:
                        if identity in tool_ids:
                            continue
                        tool_ids.add(identity)
                    counts[name] += 1
                    if name == "Skill" and isinstance(block.get("input"), dict):
                        called = block["input"].get("skill")
                        if isinstance(called, str) and called.split(":")[-1] == "op-eval":
                            skill_called = True
            if event.get("type") == "result":
                # Exclude the free-text result entirely; only observable scalar
                # completion metadata is safe to summarize automatically.
                for key in ("subtype", "is_error", "duration_ms", "num_turns"):
                    value = event.get(key)
                    if type(value) in (str, int, float, bool) and not (isinstance(value, float) and not math.isfinite(value)):
                        result_metadata[key] = value
    discovered = any(name.lstrip("/").split(":")[-1] == "op-eval" for name in skills | slash_commands)
    return {
        "initialized": initialized, "models_observed": sorted(models),
        "model_identity_source": "Claude init/assistant metadata; provider identity is not independently attested",
        "tool_name_counts": dict(sorted(counts.items())), "tool_calls_observed": sum(counts.values()),
        "op_eval_discovered": discovered, "op_eval_skill_tool_observed": skill_called,
        "result_metadata": result_metadata, "non_json_lines": malformed_lines,
    }


def _terminate_group(process):
    """Bounded TERM/KILL of the spawned group; not a sandbox containment claim."""
    actions = []
    try:
        os.killpg(process.pid, signal.SIGTERM)
        actions.append("SIGTERM")
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    # The parent may exit before a same-group child; a final group kill handles
    # those ordinary descendants as well. Escaped sessions require OS isolation.
    try:
        os.killpg(process.pid, signal.SIGKILL)
        actions.append("SIGKILL")
    except ProcessLookupError:
        pass
    process.wait(timeout=10)
    return actions


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--claude-config-dir", default=CLAUDE_CONFIG_DIR, help="Override CLAUDE_CONFIG_DIR; otherwise inherit or use the CLI default")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 86400:
        parser.error("--timeout must be between 1 and 86400 seconds")
    workspace = Path(args.workspace).resolve()
    output = Path(args.output).absolute()
    if output.exists() or output.is_symlink():
        parser.error("--output must be a new evidence directory")
    if not (workspace / "kernel.asc").is_file():
        parser.error("Workspace has no kernel.asc")
    config = json.loads((workspace / ".eval" / "session.json").read_text())
    before = _read_judge(config, "status")
    if before.get("claimed_agent_metadata", {}).get("evaluation_purpose") != "entry_smoke":
        parser.error("Judge session must be administrator-bound entry_smoke; refusing an ability-evaluation session")
    if before.get("state") != "PENDING":
        parser.error("Use a fresh PENDING workspace so old results cannot count as this smoke run")
    if args.claude_config_dir and not Path(args.claude_config_dir).is_dir():
        parser.error("The user-requested CLAUDE_CONFIG_DIR does not exist")
    executable = shutil.which("claude")
    if executable is None:
        parser.error("claude is not on PATH")
    output.mkdir(parents=True, mode=0o700)
    output.chmod(0o700)
    command = [executable, "-p", "/op-eval", "--output-format", "stream-json", "--verbose"]
    source_before = _sha(workspace / "kernel.asc")
    meta = {
        "started_at": _utc(), "workspace": str(workspace), "command": command,
        "claude_config_dir": args.claude_config_dir, "timeout_seconds": args.timeout,
        "session_id": config["session_id"], "evaluation_purpose": "entry_smoke",
        "kernel_sha256_before": source_before,
        "model_and_permission_overrides": False,
        "raw_stream": "claude.jsonl", "stderr": "claude.stderr.log",
    }
    _write_json(output / "meta.json", meta)
    _write_json(output / "judge_before.json", before)
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
            process = subprocess.Popen(command, cwd=str(workspace), env=environment,
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
    after, history, judge_error = None, None, None
    try:
        after = _read_judge(config, "status")
        _write_json(output / "judge_after.json", after)
        history = _read_judge(config, "history")
        _write_json(output / "judge_history.json", history)
    except RuntimeError as error:
        judge_error = str(error)
    source_after = _sha(workspace / "kernel.asc") if (workspace / "kernel.asc").is_file() else None
    final = (after or {}).get("final_report") or {}
    final_operation_id = (after or {}).get("final_operation_id")
    local_report = None
    try:
        local_report = json.loads((workspace / ".eval" / "report.json").read_text())
    except (OSError, ValueError):
        pass
    report_matches = (isinstance(local_report, dict)
                      and local_report.get("session", {}).get("session_id") == config["session_id"]
                      and local_report.get("result", {}).get("final_operation_id") == final_operation_id
                      and final_operation_id is not None)
    report_exists = (workspace / "REPORT.md").is_file()
    operations = (history or {}).get("operations", [])
    final_operations = [operation for operation in operations if operation.get("action") == "final"]
    final_source = final_operations[-1].get("source_sha256") if final_operations else None
    final_score = final.get("score")
    npu_scored = (final.get("outcome") == "passed"
                  and final.get("stages", {}).get("performance") == "passed"
                  and final.get("metric") == "msprof_device_task_duration"
                  and type(final_score) in (int, float) and math.isfinite(final_score))
    pipeline_completed = ((after or {}).get("state") == "FINISHED"
                          and final_operation_id is not None and report_exists and report_matches)
    summary = {
        "schema_version": 1, "evaluation_purpose": "entry_smoke",
        "claim": "Entry and evaluator usability smoke; not an agent implementation/optimization score",
        **observations, "process": process_result,
        "requested_entry": "/op-eval", "kernel_sha256_before": source_before,
        "kernel_sha256_after": source_after, "kernel_unchanged": source_before == source_after,
        "judge_session_id": config["session_id"], "judge_state": (after or {}).get("state"),
        "judge_started": (after or {}).get("started_at") is not None,
        "judge_feedback_used": (after or {}).get("feedback_used"),
        "judge_actions": [operation.get("action") for operation in operations],
        "judge_final_outcome": final.get("outcome"), "judge_final_score": final_score,
        "judge_final_correctness": final.get("correctness"),
        "judge_final_stages": final.get("stages"),
        "judge_final_source_matches_initial": final_source == source_before,
        "judge_read_error": judge_error, "report_markdown_exists": report_exists,
        "report_json_matches_session_and_final": report_matches,
        "pipeline_completed": pipeline_completed, "npu_performance_scored": npu_scored,
        "smoke_passed": bool(pipeline_completed and npu_scored and source_before == source_after
                             and final_source == source_before and process_result["exit_code"] == 0
                             and not timed_out and not interrupted and observations["initialized"]),
        "evidence_directory": str(output),
    }
    _write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2))
    return 0 if summary["smoke_passed"] else 130 if interrupted else 124 if timed_out else 1


if __name__ == "__main__":
    sys.exit(main())
