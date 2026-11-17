import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from admin import export_workspace
from engine.budget import Policy
from engine.controller import Controller
from service import make_server


PACKAGE = Path(__file__).resolve().parents[1] / "problems" / "vector_add_cpu_example"


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.controller = Controller(self.root / "judge", mode="development")
        self.server = make_server(self.controller, ("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.endpoint = "http://127.0.0.1:" + str(self.server.server_address[1])

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def workspace(self, name, feedback=2):
        credentials = self.controller.admin_create(PACKAGE, track="optimize", owner_id="test",
                                                   resource_id=name, policy=Policy(max_feedback=feedback))
        workspace = export_workspace(self.controller, credentials, self.root / name, self.endpoint)
        return workspace, credentials

    def cli(self, root, *args, expected_code=0):
        result = subprocess.run([sys.executable, str(root / "eval"), *args], cwd=root,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_real_client_historical_good_version_survives_latest_compile_failure(self):
        root, _ = self.workspace("old-good")
        source = (root / "kernel.asc").read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        self.assertEqual(self.cli(root, "status")["state"], "PENDING")
        self.cli(root, "start")
        good = self.cli(root, "check")
        self.assertEqual(good["report"]["outcome"], "passed")
        (root / "kernel.asc").write_text("this is invalid C++\n")
        bad = self.cli(root, "check")
        self.assertEqual(bad["report"]["outcome"], "compile_failed")
        (root / "kernel.asc").write_text("new unsubmitted version\n")
        denied = self.cli(root, "final", "--current", expected_code=1)
        self.assertEqual(denied["error"], "UNSUBMITTED_FINAL_SOURCE")
        final = self.cli(root, "final")
        self.assertEqual(final["source_sha256"], digest)
        self.assertEqual(final["report"]["outcome"], "passed")
        self.assertEqual(final["report"]["correctness"]["scope"], "public_and_hidden_full")
        self.assertEqual(final["session"]["feedback_used"], 2)
        diagnostics = self.cli(root, "diagnostics")
        self.assertEqual(diagnostics["highest_verified_stage"], "correctness")
        self.assertIsNone(diagnostics["task_result"]["task_score"])
        self.assertFalse(diagnostics["task_result"]["rankable"])
        self.assertEqual(diagnostics["first_verified"]["build"]["attempt"], 1)
        self.assertFalse((root / "reference").exists())
        self.assertFalse((root / "judge.py").exists())

    def test_every_version_fails_compilation_final_zero_has_no_fictional_progress(self):
        root, _ = self.workspace("all-broken", feedback=1)
        self.cli(root, "start")
        (root / "kernel.asc").write_text("this does not compile\n")
        self.cli(root, "check")
        final = self.cli(root, "final")
        self.assertEqual(final["report"]["outcome"], "compile_failed")
        report = self.cli(root, "diagnostics")
        self.assertEqual(report["task_result"]["task_score"], 0)
        self.assertTrue(report["all_observed_attempts_failed_compilation"])
        self.assertEqual(report["highest_verified_stage"], "none")
        self.assertEqual(report["first_verified"]["build"], None)
        self.assertTrue(report["trace"][0]["error_category"])

    def test_lost_response_retry_uses_same_operation_and_budget(self):
        root, credentials = self.workspace("retry", feedback=2)
        self.cli(root, "start")
        pending = {"command": "submit", "payload": {"request_id": "simulate-lost-response",
                    "action": "check", "source": (root / "kernel.asc").read_text()}}
        # A request completed at the server but its response was lost to client.
        first = self.controller.dispatch(credentials["session_id"], credentials["token"],
                                         pending["command"], pending["payload"])
        (root / ".eval" / "pending.json").write_text(json.dumps(pending))
        replay = self.cli(root, "retry")
        self.assertEqual(replay["operation_id"], first["operation_id"])
        self.assertEqual(replay["session"]["feedback_used"], 1)
        self.assertFalse((root / ".eval" / "pending.json").exists())

    def test_server_error_preserves_pending_request_for_retry(self):
        root, _ = self.workspace("transient")
        self.cli(root, "start")
        dispatch = self.controller.dispatch
        def fail(*args, **kwargs):
            raise RuntimeError("simulated transport failure after admission boundary")
        self.controller.dispatch = fail
        result = self.cli(root, "check", expected_code=1)
        self.assertEqual(result["error"], "JUDGE_UNAVAILABLE")
        saved = json.loads((root / ".eval" / "pending.json").read_text())
        self.controller.dispatch = dispatch
        replay = self.cli(root, "retry")
        self.assertEqual(replay["request_id"], saved["payload"]["request_id"])
        self.assertEqual(replay["session"]["feedback_used"], 1)
