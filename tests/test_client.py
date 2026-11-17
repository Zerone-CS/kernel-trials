import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import client


class ClientReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".eval").mkdir()
        (self.root / ".eval" / "session.json").write_text(json.dumps({"session_id": "s", "token": "secret", "endpoint": "http://127.0.0.1:1"}))
        (self.root / "kernel.asc").write_text("candidate kernel")
        self.config = {"session_id": "s", "token": "secret", "endpoint": "http://127.0.0.1:1"}
        self.final = {"operation_id": "f", "request_id": "request", "action": "final", "state": "FINISHED",
                      "report": {"outcome": "passed", "score": 80}}
        self.artifact = {"report": {"session": {"session_id": "s"}, "summary": "开发评测；80/100"},
                         "markdown": "# Report\n\n开发评测；80/100\n"}

    def run_cli(self, args, replies):
        output = io.StringIO()
        with patch.object(client, "__file__", str(self.root / "eval")), patch.object(client, "request", side_effect=replies) as request, contextlib.redirect_stdout(output):
            code = client.main(args)
        return code, json.loads(output.getvalue()), request.call_args_list

    def test_final_downloads_report_without_rerunning_judgment(self):
        code, result, calls = self.run_cli(["final"], [(self.final, 200), (self.artifact, 200)])
        self.assertEqual(code, 0)
        self.assertEqual([call.args[1] for call in calls], ["final_previous", "report"])
        self.assertTrue(result["report_artifacts"]["saved"])
        self.assertEqual((self.root / "REPORT.md").read_text(), self.artifact["markdown"])
        self.assertEqual(json.loads((self.root / ".eval" / "report.json").read_text()), self.artifact["report"])
        self.assertFalse((self.root / ".eval" / "pending.json").exists())

    def test_report_download_failure_does_not_undo_final_or_retry_it(self):
        code, result, calls = self.run_cli(["final"], [(self.final, 200), OSError("connection lost")])
        self.assertEqual(code, 0)
        self.assertEqual(result["report"]["score"], 80)
        self.assertFalse(result["report_artifacts"]["saved"])
        self.assertEqual(result["report_artifacts"]["next"], "./eval report")
        self.assertEqual(len(calls), 2)
        self.assertFalse((self.root / ".eval" / "pending.json").exists())
        code, result, calls = self.run_cli(["report"], [(self.artifact, 200)])
        self.assertEqual(code, 0)
        self.assertEqual([call.args[1] for call in calls], ["report"])
        self.assertTrue(result["report_artifacts"]["saved"])

    def test_retry_of_final_keeps_original_request_and_downloads_artifact(self):
        saved = {"command": "final_previous", "payload": {"request_id": "original"}}
        (self.root / ".eval" / "pending.json").write_text(json.dumps(saved))
        _, _, calls = self.run_cli(["retry"], [(self.final, 200), (self.artifact, 200)])
        self.assertEqual(calls[0].args[2], saved["payload"])
        self.assertFalse((self.root / ".eval" / "pending.json").exists())

    def test_cross_session_artifact_is_not_written(self):
        foreign = {"report": {"session": {"session_id": "another"}}, "markdown": "wrong"}
        _, result, _ = self.run_cli(["final"], [(self.final, 200), (foreign, 200)])
        self.assertFalse(result["report_artifacts"]["saved"])
        self.assertFalse((self.root / "REPORT.md").exists())

    def test_reserved_replay_keeps_pending_and_does_not_download_report(self):
        saved = {"command": "final_previous", "payload": {"request_id": "original"}}
        (self.root / ".eval" / "pending.json").write_text(json.dumps(saved))
        _, _, calls = self.run_cli(["retry"], [({"action": "final", "state": "RESERVED"}, 200)])
        self.assertTrue((self.root / ".eval" / "pending.json").exists())
        self.assertEqual(len(calls), 1)

    def test_5xx_preserves_pending_request(self):
        _, _, calls = self.run_cli(["check"], [({"error": "JUDGE_UNAVAILABLE"}, 500)])
        pending = json.loads((self.root / ".eval" / "pending.json").read_text())
        self.assertEqual(pending["payload"], calls[0].args[2])

    def test_invalid_flags_fail_without_a_request(self):
        for argv in (["check", "--current"], ["final", "--source", "abc", "--current"]):
            with self.subTest(argv=argv), patch.object(client, "request") as request, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                client.main(argv)
            request.assert_not_called()

    def measured_report(self):
        measurement = {"case_id": 1, "median_us": 2.5, "capture_medians_us": [2.4, 2.5, 2.6],
                       "measurement_valid": True, "actual_cores": 24,
                       "captures": [{"parsed": {"samples_us": [float(n) for n in range(32)]},
                                     "csv_files": ["capture.csv"], "measurement_valid": True} for _ in range(3)]}
        return {"outcome": "passed", "stages": {"build": "passed", "performance": "passed"},
                "correctness": {"passed": 15, "total": 15, "scope": "public_full"},
                "score": 70, "scoring": {"cases": [{"case_id": 1, "case_score": 70}]},
                "baseline_identity": {"baseline_id": "baseline", "source_sha256": "a" * 64},
                "measurements": [measurement], "performance": {"metric": "msprof", "cases": [measurement]},
                "error": {"category": "example-diagnostic"}, "build_diagnostics": "compiler repair information"}

    def test_compact_cli_preserves_full_disk_evidence_and_all_decision_fields(self):
        report = self.measured_report()
        original = {"operation_id": "bench1", "action": "bench", "state": "FINISHED", "report": report,
                    "session": {"feedback_used": 2, "remaining_wall_seconds": 3300, "final_report": report}}
        before = copy.deepcopy(original)
        code, output, _ = self.run_cli(["bench"], [(original, 200)])
        self.assertEqual(code, 0)
        self.assertEqual(original, before)
        saved = json.loads((self.root / ".eval" / "last.json").read_text())
        self.assertEqual(saved, before)
        for compact_report in (output["report"], output["session"]["final_report"]):
            for row in (compact_report["measurements"][0], compact_report["performance"]["cases"][0]):
                self.assertNotIn("captures", row)
                self.assertEqual(row["captures_omitted"], 3)
                self.assertEqual(row["median_us"], 2.5)
                self.assertEqual(row["capture_medians_us"], [2.4, 2.5, 2.6])
                self.assertTrue(row["measurement_valid"])
                self.assertEqual(row["actual_cores"], 24)
            for key in ("outcome", "stages", "correctness", "score", "scoring", "baseline_identity", "error", "build_diagnostics"):
                self.assertEqual(compact_report[key], before["report"][key])
        self.assertEqual(output["session"]["feedback_used"], 2)
        self.assertEqual(output["operation_id"], "bench1")
        self.assertLess(len(json.dumps(output)), len(json.dumps(before)))

    def test_history_reports_are_compact_but_full_flag_returns_original(self):
        original = {"session": {"feedback_used": 1}, "operations": [
            {"operation_id": "history1", "report": self.measured_report()}]}
        _, output, _ = self.run_cli(["history"], [(original, 200)])
        self.assertNotIn("captures", output["operations"][0]["report"]["measurements"][0])
        self.assertEqual(json.loads((self.root / ".eval" / "last.json").read_text()), original)
        _, full, _ = self.run_cli(["history", "--full"], [(original, 200)])
        self.assertEqual(full, original)
