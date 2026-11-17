import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import launch


class LaunchClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"launcher_id": "a" * 32, "token": "launcher-secret", "endpoint": "http://127.0.0.1:8766"}
        launch._save(self.root / ".eval/launcher.json", self.config)

    def response(self, session_id="1" * 32, state="PENDING", index=1):
        files = []
        for name, content in {"kernel.asc": b"starter", "eval": b"client", "TASK.md": b"task",
                              "OP_EVAL_WORKFLOW.md": b"workflow"}.items():
            files.append({"path": name, "content_b64": base64.b64encode(content).decode(),
                          "sha256": hashlib.sha256(content).hexdigest(), "executable": name == "eval"})
        return {"session_id": session_id, "track": "from_scratch", "attempt_index": index,
                "session": {"session_id": session_id, "track": "from_scratch", "state": state,
                            "policy": {"max_feedback": 20, "active_seconds": 3600}, "feedback_used": 0,
                            "claimed_agent_metadata": {"evaluation_purpose": "agent_evaluation"}},
                "credentials": {"session_id": session_id, "token": "session-secret"}, "files": files,
                "reused": False}

    def test_new_workspace_is_private_and_stdout_metadata_has_no_credentials(self):
        with patch.object(launch, "_post", return_value=self.response()) as post:
            result = launch.open_workspace(self.root)
        workspace = Path(result["workspace"])
        self.assertEqual((workspace / "kernel.asc").read_bytes(), b"starter")
        self.assertEqual((workspace / ".eval/session.json").stat().st_mode & 0o777, 0o600)
        self.assertTrue((workspace / "eval").stat().st_mode & 0o111)
        self.assertEqual(result["next_action"], "run_workflow")
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(set(post.call_args.args[2]), {"track", "request_id"})

    def test_resume_preserves_edits_and_does_not_reset_working_tree(self):
        with patch.object(launch, "_post", return_value=self.response()):
            first = launch.open_workspace(self.root)
        kernel = Path(first["workspace"]) / "kernel.asc"
        kernel.write_text("agent's progress")
        response = self.response(state="OPEN")
        response["session"]["feedback_used"] = 19
        response["reused"] = True
        with patch.object(launch, "_post", return_value=response):
            resumed = launch.open_workspace(self.root)
        self.assertEqual(kernel.read_text(), "agent's progress")
        self.assertEqual(resumed["feedback_used"], 19)
        self.assertEqual(first["workspace"], resumed["workspace"])

    def test_lost_response_retry_keeps_request_even_if_server_session_finished(self):
        calls = []
        def fail(config, action, body, root):
            calls.append(body)
            raise launch.LaunchClientError("JUDGE_UNAVAILABLE", "lost")
        with patch.object(launch, "_post", side_effect=fail), self.assertRaises(launch.LaunchClientError):
            launch.open_workspace(self.root)
        def recover(config, action, body, root):
            calls.append(body)
            return self.response(state="FINISHED")
        with patch.object(launch, "_post", side_effect=recover):
            result = launch.open_workspace(self.root)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(result["next_action"], "read_report")
        self.assertFalse((self.root / ".eval/launch-pending-from_scratch.json").exists())

    def test_new_attempt_keeps_previous_workspace_and_report(self):
        with patch.object(launch, "_post", return_value=self.response()):
            first = launch.open_workspace(self.root)
        report = Path(first["workspace"]) / "REPORT.md"
        report.write_text("failed run preserved")
        with patch.object(launch, "_post", return_value=self.response("2" * 32, index=2)):
            second = launch.open_workspace(self.root)
        self.assertNotEqual(first["workspace"], second["workspace"])
        self.assertEqual(report.read_text(), "failed run preserved")

    def test_pending_final_routes_to_existing_operation(self):
        response = self.response(state="FINALIZING")
        response["session"]["pending_operation_id"] = "operation"
        with patch.object(launch, "_post", return_value=response):
            result = launch.open_workspace(self.root)
        self.assertEqual(result["next_action"], "resume_operation")
        self.assertEqual(result["pending_operation_id"], "operation")

    def test_bad_digest_or_path_never_publishes_workspace_and_keeps_pending(self):
        for kind in ("digest", "path", "reserved"):
            response = self.response()
            if kind == "digest": response["files"][0]["sha256"] = "0" * 64
            elif kind == "path": response["files"][0]["path"] = "../outside"
            else: response["files"][0]["path"] = ".eval/session.json"
            with self.subTest(kind=kind), patch.object(launch, "_post", return_value=response), self.assertRaises(ValueError):
                launch.open_workspace(self.root)
            self.assertFalse((self.root / "outside").exists())
            self.assertFalse((self.root / ("workspaces/from_scratch_" + "1" * 32)).exists())
            self.assertTrue((self.root / ".eval/launch-pending-from_scratch.json").exists())

    def test_symlinked_workspace_is_rejected_without_writing_target(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "workspaces").symlink_to(outside, target_is_directory=True)
        with patch.object(launch, "_post", return_value=self.response()), self.assertRaises(ValueError):
            launch.open_workspace(self.root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_track_mismatch_does_not_publish(self):
        response = self.response()
        response["session"]["track"] = "optimize"
        with patch.object(launch, "_post", return_value=response), self.assertRaises(launch.LaunchClientError):
            launch.open_workspace(self.root)
        self.assertFalse((self.root / "workspaces").exists())
