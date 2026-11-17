"""Authoritative lifecycle races with real SQLite and synthetic workers only."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from engine.budget import BudgetError, Policy, _ADMIN_FINALIZER
from engine.controller import Controller
from engine.identity import npu_resource_id
from engine.problems import load_problem


ROOT = Path(__file__).resolve().parents[1]
CPU = ROOT / "problems" / "vector_add_cpu_example"
NPU = ROOT / "problems" / "batch_matmul_reduce"


class Worker:
    def __init__(self):
        self.calls = []
        self.final_started = threading.Event()
        self.final_release = threading.Event()
        self.final_release.set()

    def __call__(self, package, source, action, directory, **kwargs):
        self.calls.append({"source": source, "action": action, **kwargs})
        if action == "final":
            self.final_started.set()
            if not self.final_release.wait(5):
                raise RuntimeError("test did not release final worker")
        return {"outcome": "passed", "action": action,
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "stages": {name: "passed" for name in ("build", "runtime", "correctness", "performance")},
                "correctness": {"passed": 4, "total": 4,
                                "scope": "public_and_hidden_full" if action == "final" else "public_full"},
                "score": 50, "score_status": "SYNTHETIC_TEST_RESULT",
                "security": {"mode": "development", "rankable": False, "security_label": "not_cheat_resistant"}}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.worker = Worker()
        self.controller = Controller(self.root / "judge", runner=self.worker, clock=lambda: self.now)

    def create(self, package=CPU, **kwargs):
        return self.controller.admin_create(package, track="optimize", owner_id="test", **kwargs)

    def start(self, credentials):
        return self.controller.start(credentials["session_id"], credentials["token"])

    def submit(self, credentials, request_id="check", action="check", source="// admitted kernel A\n"):
        return self.controller.submit(credentials["session_id"], credentials["token"],
                                      request_id=request_id, action=action, source=source)

    def history(self, credentials):
        return self.controller.store.history(credentials["session_id"], credentials["token"])

    def wait_final(self, credentials):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            status = self.history(credentials)["session"]
            if status["state"] == "FINISHED" and not self.controller._finalizing:
                return status
            time.sleep(0.01)
        self.fail("automatic final did not complete")

    def fake_baseline(self, package, path=None):
        if npu_resource_id(package) is None:
            return None
        if path is None:
            return {"baseline_id": self.active_baseline, "source_sha256": "f"*64}
        return json.loads(Path(path).read_text())

    def test_npu_session_without_reference_baseline_cannot_be_created(self):
        with self.assertRaisesRegex(ValueError, "calibrated"):
            self.create(NPU)
        with self.controller.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual(self.worker.calls, [])

    def test_changing_active_reference_does_not_change_existing_session(self):
        self.active_baseline = "a"*64
        with patch.object(self.controller, "_baseline", side_effect=self.fake_baseline):
            first = self.create(NPU)
            self.active_baseline = "b"*64
            second = self.create(NPU)
            first_binding = json.loads((self.controller.root / "bindings" / (first["session_id"] + ".json")).read_text())
            second_binding = json.loads((self.controller.root / "bindings" / (second["session_id"] + ".json")).read_text())
            self.assertEqual(first_binding["baseline_id"], "a"*64)
            self.assertEqual(second_binding["baseline_id"], "b"*64)
            self.assertEqual(first_binding["hidden_seed"], second_binding["hidden_seed"])
            self.start(first)
            self.submit(first)
            self.assertEqual(self.worker.calls[0]["baseline"]["baseline_id"], "a"*64)

    def test_device_lease_cannot_be_bypassed_with_caller_resource_name(self):
        self.active_baseline = "a"*64
        with patch.object(self.controller, "_baseline", side_effect=self.fake_baseline):
            with self.assertRaisesRegex(ValueError, "resource id is fixed"):
                self.create(NPU, resource_id="give-me-another-lease")
            first = self.create(NPU)
            second = self.create(NPU, resource_id=npu_resource_id(load_problem(NPU)))
            self.start(first)
            with self.assertRaises(BudgetError) as raised:
                self.start(second)
            self.assertEqual(raised.exception.code, "RESOURCE_BUSY")
            self.assertEqual(self.history(second)["session"]["state"], "PENDING")
            self.assertIsNone(self.history(second)["session"]["started_at"])

    def test_automatic_final_uses_immutable_history_and_not_current_unsubmitted_code(self):
        credentials = self.create(policy=Policy(max_feedback=1, active_seconds=100, final_grace_seconds=120))
        self.start(credentials)
        accepted = self.submit(credentials)
        self.controller.autofinalize_due()
        self.assertEqual(len(self.worker.calls), 1)  # Selection grace still open.
        self.now += 61
        self.controller.autofinalize_due()
        self.wait_final(credentials)
        history = self.history(credentials)
        self.assertEqual(history["session"]["feedback_used"], 1)
        self.assertEqual(len(history["operations"]), 2)
        final = history["operations"][-1]
        self.assertEqual(final["action"], "final")
        self.assertTrue(final["request_id"].startswith("server-auto-final-"))
        self.assertEqual(final["source_sha256"], accepted["source_sha256"])
        self.assertEqual(self.worker.calls[1]["source"], self.worker.calls[0]["source"])
        self.assertTrue((self.controller.root / "reports" / credentials["session_id"] / "REPORT.md").is_file())

    def test_manual_final_racing_automatic_final_has_exactly_one_winner(self):
        credentials = self.create(policy=Policy(max_feedback=1, active_seconds=100, final_grace_seconds=120))
        self.start(credentials)
        self.submit(credentials)
        self.now += 61
        self.worker.final_release.clear()
        self.addCleanup(self.worker.final_release.set)
        self.controller.autofinalize_due()
        self.assertTrue(self.worker.final_started.wait(3))
        with self.assertRaises(BudgetError) as raised:
            self.controller.final_previous(credentials["session_id"], credentials["token"], request_id="manual-final")
        self.assertEqual(raised.exception.code, "SESSION_CLOSED")
        self.controller.autofinalize_due()
        self.worker.final_release.set()
        self.wait_final(credentials)
        self.assertEqual(sum(call["action"] == "final" for call in self.worker.calls), 1)
        self.assertEqual(sum(op["action"] == "final" for op in self.history(credentials)["operations"]), 1)

    def test_no_submission_expires_without_fabricated_score_or_worker(self):
        credentials = self.create(policy=Policy(max_feedback=1, active_seconds=10, final_grace_seconds=10))
        self.start(credentials)
        self.now += 15
        self.controller.autofinalize_due()
        self.now += 10
        self.controller.autofinalize_due()
        history = self.history(credentials)
        self.assertEqual(history["session"]["state"], "EXPIRED")
        self.assertEqual(history["operations"], [])
        report = self.controller.report(credentials["session_id"], credentials["token"])["report"]
        self.assertEqual(report["result"]["outcome"], "no_final_result")
        self.assertIsNone(report["result"]["score"])
        self.assertEqual(self.worker.calls, [])

    def test_default_no_submission_auto_closure_writes_reports_without_fabricated_score(self):
        credentials = self.create()  # Default 20 feedback / 3600 seconds / 600 grace.
        self.start(credentials)
        self.now += 3600
        self.controller.autofinalize_due()
        self.assertEqual(self.history(credentials)["session"]["budget_stop_reason"], "time_limit")
        self.now += 600
        self.controller.autofinalize_due()
        directory = self.controller.root / "reports" / credentials["session_id"]
        json_path, markdown_path = directory / "report.json", directory / "REPORT.md"
        self.assertTrue(json_path.is_file())
        self.assertTrue(markdown_path.is_file())
        saved = json.loads(json_path.read_text())
        self.assertEqual(saved["session"]["state"], "EXPIRED")
        self.assertEqual(saved["result"]["outcome"], "no_final_result")
        self.assertIsNone(saved["result"]["score"])
        self.assertIsNone(saved["result"]["source_sha256"])
        self.assertIsNone(saved["result"]["final_operation_id"])
        self.assertEqual(saved["budget"]["feedback_used"], 0)
        self.assertFalse(saved["result"]["rankable"])
        self.assertEqual(saved["diagnostics"]["trace"], [])
        self.assertTrue(all(value is None for value in saved["diagnostics"]["first_verified"].values()))
        markdown = markdown_path.read_text()
        self.assertIn("no_final_result", markdown)
        self.assertIn("| 任务分 | 无有效分数 |", markdown)
        self.assertEqual(self.history(credentials)["operations"], [])
        self.assertEqual(self.worker.calls, [])

    def test_admin_finalizer_capability_cannot_create_feedback_or_new_final_source(self):
        credentials = self.create()
        self.start(credentials)
        for action in ("check", "bench"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.controller.submit(credentials["session_id"], _ADMIN_FINALIZER,
                                       request_id="forbidden-"+action, action=action, source="// new code")
        with self.assertRaises(BudgetError) as raised:
            self.controller.submit(credentials["session_id"], _ADMIN_FINALIZER,
                                   request_id="forbidden-new-final", action="final", source="// new code")
        self.assertEqual(raised.exception.code, "UNSUBMITTED_FINAL_SOURCE")
        self.assertEqual(self.history(credentials)["session"]["feedback_used"], 0)
        self.assertEqual(self.worker.calls, [])

    def test_string_cannot_impersonate_in_process_finalizer_capability(self):
        credentials = self.create()
        for token in ("_ADMIN_FINALIZER", str(_ADMIN_FINALIZER), "server-auto-final"):
            with self.subTest(token=token), self.assertRaises(BudgetError) as raised:
                self.controller.dispatch(credentials["session_id"], token, "status", {})
            self.assertEqual(raised.exception.code, "AUTH_FAILED")


if __name__ == "__main__":
    unittest.main()
