#!/usr/bin/env python3
"""Standalone candidate client. Only kernel.asc is uploaded to the judge."""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid


def _write_atomic(path, data):
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf8") as stream:
            stream.write(data)
        temporary.chmod(0o600)
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _compact_measurements(rows):
    result = []
    for row in rows:
        if not isinstance(row, dict):
            result.append(_compact_output(row))
            continue
        summary = {key: _compact_output(value) for key, value in row.items() if key != "captures"}
        if "captures" in row:
            summary["captures_omitted"] = len(row["captures"]) if isinstance(row["captures"], list) else True
        result.append(summary)
    return result


def _compact_output(value):
    """Reduce repeated profiler evidence only; never mutate saved judge data.

    Recursion handles operation.report, session.final_report and history rows.
    Scores, source identities, budgets, correctness and repair logs stay visible.
    Every original capture remains in .eval/last.json or --full stdout.
    """
    if isinstance(value, list):
        return [_compact_output(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "measurements" and isinstance(item, list):
            result[key] = _compact_measurements(item)
        elif key == "performance" and isinstance(item, dict):
            result[key] = _compact_output(item)
            if isinstance(item.get("cases"), list):
                result[key]["cases"] = _compact_measurements(item["cases"])
        else:
            result[key] = _compact_output(item)
    return result


def request(config, command, payload):
    url = config["endpoint"].rstrip("/") + "/v1/sessions/" + config["session_id"] + "/" + command
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + config["token"]})
    try:
        with urllib.request.urlopen(req, timeout=1800) as response:
            return json.load(response), response.status
    except urllib.error.HTTPError as error:
        return json.load(error), error.code


def save_report(root, config):
    """Retrieve read-only artifacts. Failure must never resubmit the final."""
    result, status = request(config, "report", {})
    if status >= 400:
        return {"saved": False, "error": result.get("error", "REPORT_UNAVAILABLE"),
                "message": result.get("message", "Report is unavailable"), "next": "./eval report"}
    if (not isinstance(result, dict) or not isinstance(result.get("report"), dict)
            or not isinstance(result.get("markdown"), str)):
        raise ValueError("Judge returned an invalid report artifact")
    report = result["report"]
    if report.get("session", {}).get("session_id") != config["session_id"]:
        raise ValueError("Report belongs to another session")
    _write_atomic(root / ".eval" / "report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _write_atomic(root / "REPORT.md", result["markdown"])
    return {"saved": True, "json": str(root / ".eval" / "report.json"),
            "markdown": str(root / "REPORT.md"), "summary": report.get("summary")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "history", "diagnostics", "report", "check", "bench", "final", "retry"))
    parser.add_argument("--source", help="Historical source SHA for final; no source edits are uploaded")
    parser.add_argument("--current", action="store_true", help="Final current file, only while budget remains")
    parser.add_argument("--full", action="store_true", help="Print complete profiler evidence; default keeps summaries and saves full JSON in .eval/last.json")
    args = parser.parse_args(argv)
    if (args.source or args.current) and args.action != "final":
        parser.error("--source and --current are only valid for final")
    if args.source and args.current:
        parser.error("Choose either --source or --current")
    root = Path(__file__).resolve().parent
    config = json.loads((root / ".eval" / "session.json").read_text())
    pending = root / ".eval" / "pending.json"
    if args.action == "retry":
        saved = json.loads(pending.read_text())
        command, payload = saved["command"], saved["payload"]
    elif args.action in ("check", "bench", "final"):
        if pending.exists():
            parser.error("An earlier submission is unresolved; use ./eval retry to recover without spending another attempt")
        payload = {"request_id": uuid.uuid4().hex}
        if args.action == "final" and not args.current:
            command = "final_previous"
            if args.source:
                payload["source_sha256"] = args.source
        else:
            if args.source:
                parser.error("--source is only for historical final")
            command = "submit"
            payload.update(action=args.action, source=(root / "kernel.asc").read_text())
        try:
            with pending.open("x", encoding="utf8") as stream:
                stream.write(json.dumps({"command": command, "payload": payload}))
            pending.chmod(0o600)
        except FileExistsError:
            parser.error("Another submission is pending; use ./eval retry")
    else:
        command, payload = args.action, {}
    try:
        if args.action == "report":
            artifacts = save_report(root, config)
            result = {"report_artifacts": artifacts}
            http_status = 200 if artifacts["saved"] else 503
        else:
            result, http_status = request(config, command, payload)
    except (OSError, ValueError) as error:
        print(json.dumps({"error": "TRANSPORT_UNCERTAIN", "message": str(error),
                          "next": "Use ./eval retry for unresolved submissions; it keeps the same request ID"}), file=sys.stderr)
        return 2
    if pending.exists() and args.action in ("check", "bench", "final", "retry"):
        if (400 <= http_status < 500 or
                http_status < 400 and result.get("state") != "RESERVED"):
            pending.unlink()
    # Only a completed final triggers report download. Retry may recover the
    # same final response; downloading the report is a free read, never a run.
    if (http_status < 400 and result.get("action") == "final"
            and result.get("state") == "FINISHED"):
        try:
            result["report_artifacts"] = save_report(root, config)
        except (OSError, ValueError) as error:
            result["report_artifacts"] = {"saved": False, "error": "REPORT_DOWNLOAD_FAILED",
                                          "message": str(error), "next": "./eval report"}
    _write_atomic(root / ".eval" / "last.json", json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result if args.full else _compact_output(result), ensure_ascii=False, indent=2))
    return 1 if http_status >= 400 else 0


if __name__ == "__main__":
    sys.exit(main())
