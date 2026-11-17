"""Trust/budget integration with deterministic workers, no GPU/model execution."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import client
from admin import export_workspace
from engine.budget import BudgetError, Policy
from engine.controller import Controller
from engine.security import SecurityError
from service import make_server


FIXTURE = Path(__file__).resolve().parents[1] / "problems" / "vector_add_cpu_example"
SOURCE_A = "// submitted version A\n"
SOURCE_B = "// newer version B\n"


def passing(scope="public_full"):
    return {"outcome": "passed", "stages": {"build": "passed", "runtime": "passed",
            "correctness": "passed", "performance": "not_run"},
            "correctness": {"passed": 2, "total": 2, "scope": scope}, "score": None,
            "security": {"rankable": False, "security_label": "not_cheat_resistant"}}


def compile_failure():
    return {"outcome": "compile_failed", "stages": {"build": "failed", "runtime": "not_run",
            "correctness": "not_run", "performance": "not_run"}, "score": None,
            "error": {"category": "compiler_exit_nonzero"}}


class StubRunner:
    def __init__(self):
        self.calls = []
        self.replies = []

    def __call__(self, package, data, action, run_dir, *, mode):
        self.calls.append({"source": data, "action": action, "mode": mode,
                           "digest": package.digest, "run_dir": run_dir,
                           "harness": (package.root / "harness" / "main.cpp").read_bytes()})
        reply = self.replies.pop(0) if self.replies else passing()
        if isinstance(reply, Exception):
            raise reply
        return copy.deepcopy(reply)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.worker = StubRunner()
        self.controller = Controller(self.root / "state", runner=self.worker, clock=lambda: self.now)
        self.credentials = self.create()

    def create(self, policy=None, start=True):
        credentials = self.controller.admin_create(
            FIXTURE, track="from_scratch", owner_id="test-admin",
            resource_id="resource-" + str(len(list((self.root / "state" / "bindings").glob("*.json")))),
            policy=policy)
        if start:
            self.controller.start(credentials["session_id"], credentials["token"])
        return credentials

    def dispatch(self, command, payload=None, credentials=None):
        c = credentials or self.credentials
        return self.controller.dispatch(c["session_id"], c["token"], command, payload or {})

    def submit(self, request="r1", action="check", source=SOURCE_A, credentials=None):
        return self.dispatch("submit", {"request_id": request, "action": action, "source": source}, credentials)

    def denied(self, code, call, *args, **kwargs):
        with self.assertRaises(BudgetError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def test_one_feedback_limit_rejects_new_final_hash_and_retests_failed_historical_snapshot(self):
        c = self.create(Policy(max_feedback=1))
        self.worker.replies = [compile_failure(), compile_failure()]
        first = self.submit(credentials=c)
        self.assertEqual(first["session"]["feedback_used"], 1)
        self.assertEqual(first["session"]["budget_stop_reason"], "feedback_limit")
        self.denied("UNSUBMITTED_FINAL_SOURCE", self.submit, "new-final", "final", SOURCE_B, c)
        self.assertEqual(len(self.worker.calls), 1)
        rejected_snapshot = self.root / "state" / "sources" / c["session_id"] / (hashlib.sha256(SOURCE_B.encode()).hexdigest() + ".asc")
        self.assertFalse(rejected_snapshot.exists())
        final = self.dispatch("final_previous", {"request_id": "final", "source_sha256": first["source_sha256"]}, c)
        self.assertEqual([call["source"] for call in self.worker.calls], [SOURCE_A.encode(), SOURCE_A.encode()])
        self.assertEqual(self.worker.calls[-1]["action"], "final")
        self.assertNotEqual(self.worker.calls[0]["run_dir"], self.worker.calls[1]["run_dir"])
        self.assertEqual(final["session"]["state"], "FINISHED")
        self.assertEqual(final["session"]["feedback_used"], 1)
        self.assertEqual(self.dispatch("diagnostics", credentials=c)["task_result"]["task_score"], 0)

    def test_final_is_new_independent_judgment_not_cached_public_success(self):
        self.worker.replies = [passing(), compile_failure()]
        checked = self.submit()
        final = self.dispatch("final_previous", {"request_id": "final", "source_sha256": checked["source_sha256"]})
        self.assertEqual(final["report"]["outcome"], "compile_failed")
        self.assertEqual(self.dispatch("diagnostics")["task_result"]["task_score"], 0)
        self.assertEqual(len(self.worker.calls), 2)

    def test_request_replay_and_final_replay_never_reexecute(self):
        first = self.submit()
        replay = self.submit()
        self.assertFalse(replay["created"])
        self.assertEqual(replay["operation_id"], first["operation_id"])
        self.assertEqual(len(self.worker.calls), 1)
        self.denied("REQUEST_CONFLICT", self.submit, "r1", "bench")
        final = self.dispatch("final_previous", {"request_id": "final"})
        retry = self.dispatch("final_previous", {"request_id": "final"})
        self.assertEqual(retry["operation_id"], final["operation_id"])
        self.assertFalse(retry["created"])
        self.denied("SESSION_CLOSED", self.dispatch, "final_previous", {"request_id": "another-final"})
        self.denied("SESSION_CLOSED", self.submit, "after-final")
        self.assertEqual(len(self.worker.calls), 2)
        self.assertEqual(len(self.dispatch("history")["operations"]), 2)

    def test_mode_policy_hidden_selection_and_claimed_scores_cannot_override(self):
        base = {"request_id": "r1", "action": "check", "source": SOURCE_A}
        for field, value in (("mode", "formal"), ("policy", {"max_feedback": 10000}),
                             ("hidden", False), ("suite", "public"), ("repeat", 1),
                             ("score", 100), ("tbest", 0.001), ("problem_digest", "0" * 64),
                             ("resource_id", "another"), ("admin_evidence", {})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.dispatch("submit", dict(base, **{field: value}))
        self.assertEqual(self.dispatch("status")["feedback_used"], 0)
        self.assertEqual(len(self.worker.calls), 0)
        self.submit()
        self.assertEqual(self.worker.calls[0]["mode"], "development")

    def test_candidate_cannot_create_reset_cancel_or_clear_authoritative_history(self):
        with sqlite3.connect(str(self.root / "state" / "budget.sqlite")) as db:
            before = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        for command in ("admin_create", "create", "reset", "cancel", "admin_cancel_session", "complete_operation"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.dispatch(command, {})
        with sqlite3.connect(str(self.root / "state" / "budget.sqlite")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], before)

    def test_wrong_token_cannot_start_submit_read_history_or_mint_work(self):
        c = dict(self.credentials, token="wrong-token")
        for command, payload in (("start", {}), ("status", {}), ("history", {}), ("diagnostics", {}),
                                 ("submit", {"request_id": "r1", "action": "check", "source": SOURCE_A}),
                                 ("final_previous", {"request_id": "final"})):
            with self.subTest(command=command):
                self.denied("AUTH_FAILED", self.dispatch, command, payload, c)
        self.assertEqual(len(self.worker.calls), 0)

    def test_worker_exception_consumes_budget_but_is_invalid_not_candidate_zero(self):
        c = self.create(Policy(max_feedback=1))
        self.worker.replies = [RuntimeError("private-oracle-secret"), RuntimeError("private-final-secret")]
        first = self.submit(credentials=c)
        self.assertEqual(first["report"]["outcome"], "infrastructure_error")
        self.assertEqual(first["session"]["feedback_used"], 1)
        self.assertIsNone(first["session"]["pending_operation_id"])
        self.assertNotIn("private-oracle-secret", json.dumps(first))
        self.denied("FEEDBACK_EXHAUSTED", self.submit, "again", credentials=c)
        final = self.dispatch("final_previous", {"request_id": "final"}, c)
        diagnosis = self.dispatch("diagnostics", credentials=c)
        self.assertEqual(final["session"]["state"], "FINISHED")
        self.assertIsNone(diagnosis["task_result"]["task_score"])
        self.assertEqual(diagnosis["highest_verified_stage"], "none")
        self.assertTrue(diagnosis["contains_infrastructure_failure"])
        self.assertNotIn("private-final-secret", json.dumps(diagnosis))

    def test_snapshot_integrity_failure_never_executes_modified_judge_source(self):
        checked = self.submit()
        snapshot = self.root / "state" / "sources" / self.credentials["session_id"] / (checked["source_sha256"] + ".asc")
        snapshot.write_text(SOURCE_B)
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.dispatch("final_previous", {"request_id": "final", "source_sha256": checked["source_sha256"]})
        self.assertEqual(len(self.worker.calls), 1)
        self.assertEqual(len(self.dispatch("history")["operations"]), 1)

    def test_new_hash_cannot_select_another_sessions_snapshot(self):
        other = self.create()
        foreign = self.submit(source=SOURCE_B, credentials=other)
        self.denied("UNSUBMITTED_FINAL_SOURCE", self.dispatch, "final_previous",
                    {"request_id": "final", "source_sha256": foreign["source_sha256"]})
        self.assertEqual(len(self.worker.calls), 1)

    def test_export_keeps_pending_and_fixed_engineering_authoritative(self):
        c = self.create(start=False)
        directory = export_workspace(self.controller, c, self.root / "workspace", "http://127.0.0.1:8765")
        self.now = 50000
        status = self.dispatch("status", credentials=c)
        self.assertEqual(status["state"], "PENDING")
        self.assertIsNone(status["started_at"])
        self.assertEqual(status["remaining_wall_seconds"], 3600)
        self.assertFalse((directory / "reference").exists())
        self.assertFalse((directory / "judge.py").exists())
        self.assertFalse((directory / "cases.json").exists())
        (directory / "main.cpp").write_text("tampered candidate harness")
        (directory / "kernel.asc").write_text(SOURCE_A)
        self.dispatch("start", credentials=c)
        self.submit(source=(directory / "kernel.asc").read_text(), credentials=c)
        self.assertNotIn(b"tampered candidate harness", self.worker.calls[0]["harness"])
        self.assertEqual(self.worker.calls[0]["source"], SOURCE_A.encode())

    def test_formal_unavailable_cannot_downgrade_or_start_timer(self):
        formal = Controller(self.root / "formal", runner=self.worker, mode="formal", clock=lambda: self.now)
        c = formal.admin_create(FIXTURE, track="from_scratch", owner_id="admin")
        with self.assertRaises(SecurityError):
            formal.start(c["session_id"], c["token"])
        status = formal.store.status(c["session_id"], c["token"])
        self.assertEqual(status["state"], "PENDING")
        self.assertIsNone(status["started_at"])
        self.assertEqual(len(self.worker.calls), 0)

    def test_http_auth_and_command_allowlist_preserve_budget(self):
        server = make_server(self.controller, ("127.0.0.1", 0))
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        try:
            endpoint = "http://127.0.0.1:" + str(server.server_address[1])
            config = dict(self.credentials, endpoint=endpoint)
            result, status = client.request(dict(config, token="wrong"), "status", {})
            self.assertEqual(status, 401)
            self.assertEqual(result["error"], "AUTH_FAILED")
            for command in ("admin_create", "reset", "complete_operation"):
                result, status = client.request(config, command, {})
                self.assertEqual(status, 400)
                self.assertEqual(result["error"], "INVALID_REQUEST")
            result, status = client.request(config, "submit", {
                "request_id": "http", "action": "check", "source": SOURCE_A, "hidden": False})
            self.assertEqual(status, 400)
            self.assertEqual(result["error"], "INVALID_REQUEST")
            result, status = client.request(config, "status", {})
            self.assertEqual(status, 200)
            self.assertEqual(result["feedback_used"], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ClientRetryTests(unittest.TestCase):
    def test_server_failure_preserves_request_id_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / ".eval").mkdir()
            (workspace / ".eval/session.json").write_text(json.dumps({
                "endpoint": "http://127.0.0.1:1", "session_id": "a" * 32, "token": "test-token"}))
            pending = workspace / ".eval/pending.json"
            saved = {"command": "submit", "payload": {"request_id": "stable-id", "action": "check", "source": SOURCE_A}}
            pending.write_text(json.dumps(saved))
            failure = urllib.error.HTTPError("http://127.0.0.1:1", 500, "Unavailable", {},
                                             io.BytesIO(b'{"error":"JUDGE_UNAVAILABLE"}'))
            with patch.object(client, "__file__", str(workspace / "eval")), \
                    patch("client.urllib.request.urlopen", side_effect=failure), \
                    patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                self.assertNotEqual(client.main(["retry"]), 0)
            self.assertTrue(pending.exists())
            self.assertEqual(json.loads(pending.read_text()), saved)
            response = io.BytesIO(b'{"state":"FINISHED","operation_id":"same-server-operation"}')
            response.status = 200
            with patch.object(client, "__file__", str(workspace / "eval")), \
                    patch("client.urllib.request.urlopen", return_value=response) as opened, \
                    patch("sys.stdout", io.StringIO()):
                self.assertEqual(client.main(["retry"]), 0)
                self.assertEqual(json.loads(opened.call_args.args[0].data)["request_id"], "stable-id")
            self.assertFalse(pending.exists())


if __name__ == "__main__":
    unittest.main()
