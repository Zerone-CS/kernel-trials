"""Real SQLite tests: races, retries, deadlines and authoritative closures."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from engine.budget import BudgetError, BudgetStore, Policy


PROBLEM = "1" * 64
SOURCE_A = "a" * 64
SOURCE_B = "b" * 64


class Clock:
    def __init__(self, value=1000):
        self.value = value

    def __call__(self):
        return self.value


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "budget.sqlite"
        self.clock = Clock()
        self.store = BudgetStore(self.db, clock=self.clock)
        self.resource_index = 0
        self.credentials = self.create()

    def create(self, policy=None, track="from_scratch", resource_id=None, start=True):
        self.resource_index += 1
        result = self.store.admin_create_session(
            problem_digest=PROBLEM, track=track, owner_id="test-owner",
            resource_id=resource_id or f"test-resource-{self.resource_index}",
            policy=policy, claimed_agent_metadata={"model": "unverified-model"})
        if start:
            result["session"] = self.store.start_session(result["session_id"], result["token"])
        return result

    def status(self, credentials=None):
        c = credentials or self.credentials
        return self.store.status(c["session_id"], c["token"])

    def reserve(self, request_id="r1", action="check", source=SOURCE_A, credentials=None):
        c = credentials or self.credentials
        return self.store.reserve(c["session_id"], c["token"], request_id=request_id,
                                  action=action, source_sha256=source)

    def denied(self, code, function, *args, **kwargs):
        with self.assertRaises(BudgetError) as context:
            function(*args, **kwargs)
        self.assertEqual(context.exception.code, code)

    def test_frozen_policy_identity_claimed_metadata_and_no_plaintext_token(self):
        c = self.credentials
        status = self.status()
        self.assertEqual(status["policy"], {"max_feedback": 20, "active_seconds": 3600,
                                             "final_grace_seconds": 600})
        self.assertEqual(status["problem_digest"], PROBLEM)
        self.assertEqual(status["track"], "from_scratch")
        self.assertFalse(status["agent_metadata_verified"])
        self.assertEqual(status["claimed_agent_metadata"], {"model": "unverified-model"})
        self.assertNotIn("token", json.dumps(status))
        with sqlite3.connect(str(self.db)) as db:
            row = db.execute("SELECT token_hash,owner_id FROM sessions").fetchone()
            dump = "\n".join(db.iterdump())
        self.assertEqual(len(row[0]), 64)
        self.assertEqual(row[1], "test-owner")
        self.assertNotIn(c["token"], dump)
        self.assertIn("no_internal_queue", status["clock_basis"])

    def test_default_twentieth_feedback_is_accepted_twenty_first_rejected_and_final_retained(self):
        # Use the unmodified default policy and real SQLite reservations. No
        # worker duration or one-hour wall-clock wait is needed to spend slots.
        self.assertEqual(self.status()["policy"], {"max_feedback": 20, "active_seconds": 3600,
                                                  "final_grace_seconds": 600})
        for attempt in range(1, 21):
            action = "check" if attempt % 2 else "bench"
            accepted = self.reserve("default-feedback-" + str(attempt), action)
            self.assertTrue(accepted["created"])
            self.assertEqual(accepted["session"]["feedback_used"], attempt)
            self.store.complete_operation(accepted["operation_id"], {
                "outcome": "compile_failed", "stages": {"build": "failed"}, "score": 0})
            status = self.status()
            self.assertEqual(status["feedback_remaining"], 20 - attempt)
            self.assertEqual(status["feedback_allowed"], attempt < 20)
        self.assertEqual(self.clock.value, 1000)
        self.assertEqual(self.status()["budget_stop_reason"], "feedback_limit")
        self.denied("FEEDBACK_EXHAUSTED", self.reserve, "default-feedback-21")
        self.denied("UNSUBMITTED_FINAL_SOURCE", self.reserve, "new-final", "final", SOURCE_B)
        final = self.reserve("default-final", "final", SOURCE_A)
        self.assertTrue(final["created"])
        self.assertEqual(final["session"]["feedback_used"], 20)
        self.store.complete_operation(final["operation_id"], {"outcome": "compile_failed", "score": 0})
        self.denied("SESSION_CLOSED", self.reserve, "second-final", "final", SOURCE_A)
        history = self.store.history(self.credentials["session_id"], self.credentials["token"])
        self.assertEqual(len(history["operations"]), 21)
        self.assertEqual(sum(op["action"] == "final" for op in history["operations"]), 1)

    def test_default_3600_second_deadline_accepts_just_before_and_rejects_exactly_at_limit(self):
        # Advance the injected server clock; deadline logic and persistence are
        # the production implementations, with the default 3600-second policy.
        started = self.status()["started_at"]
        self.assertEqual(self.status()["feedback_deadline"], started + 3600)
        self.clock.value = started + 3599.999
        before = self.reserve("just-before-default-deadline")
        self.assertTrue(before["created"])
        self.store.complete_operation(before["operation_id"], {"outcome": "compile_failed", "score": 0})
        self.clock.value = started + 3600
        self.denied("DEADLINE_REACHED", self.reserve, "exact-default-deadline")
        status = self.status()
        self.assertEqual(status["feedback_used"], 1)
        self.assertEqual(status["feedback_remaining"], 19)
        self.assertEqual(status["remaining_wall_seconds"], 0)
        self.assertEqual(status["budget_stop_reason"], "time_limit")
        self.assertEqual(status["stop_at"], started + 3600)
        self.assertEqual(status["final_deadline"], started + 4200)
        self.denied("UNSUBMITTED_FINAL_SOURCE", self.reserve, "unseen-final", "final", SOURCE_B)
        final = self.reserve("known-final-after-default-deadline", "final", SOURCE_A)
        self.assertTrue(final["created"])
        self.assertEqual(final["session"]["feedback_used"], 1)

    def test_wrong_missing_and_cross_session_tokens_cannot_spend(self):
        another = self.create()
        for token in (None, "", "wrong", another["token"]):
            self.denied("AUTH_FAILED", self.store.reserve,
                        self.credentials["session_id"], token, request_id="r1",
                        action="check", source_sha256=SOURCE_A)
        self.denied("AUTH_FAILED", self.store.status, "nonexistent", self.credentials["token"])
        self.assertEqual(self.status()["feedback_used"], 0)

    def test_invalid_policy_and_reservation_input_do_not_create_work(self):
        for values in ({"max_feedback": True}, {"max_feedback": 0}, {"active_seconds": 0},
                       {"active_seconds": 3.2}, {"final_grace_seconds": -1}):
            with self.assertRaises(ValueError):
                Policy(**values)
        for overrides in ({"request_id": ""}, {"request_id": "x/y"},
                          {"action": "renew"}, {"source": "A" * 64}):
            with self.assertRaises(ValueError):
                self.reserve(**overrides)
        self.assertEqual(self.status()["feedback_used"], 0)

    def test_compile_and_correctness_failures_both_count_and_crash_does_not_refund(self):
        c = self.create(Policy(max_feedback=3))
        first = self.reserve("compile", credentials=c)
        self.store.complete_operation(first["operation_id"], {"status": "BUILD_FAIL", "score": 0})
        second = self.reserve("precision", action="bench", credentials=c)
        self.store.complete_operation(second["operation_id"], {"status": "CORRECTNESS_FAIL", "score": 0})
        crashed = self.reserve("worker_crash", credentials=c)  # Worker disappears before reporting.
        reopened = BudgetStore(self.db, clock=self.clock)
        self.assertEqual(reopened.status(c["session_id"], c["token"])["feedback_used"], 3)
        self.denied("FEEDBACK_EXHAUSTED", self.reserve, "more", credentials=c)
        self.denied("OPERATION_PENDING", self.reserve, "final", "final", SOURCE_A, c)
        # Trusted recovery records the crash; it never refunds the reservation.
        self.store.complete_operation(crashed["operation_id"], {"status": "WORKER_CRASH", "score": 0})
        self.assertTrue(self.reserve("final", action="final", credentials=c)["created"])

    def test_identical_request_replay_is_idempotent_and_conflicting_reuse_rejected(self):
        first = self.reserve()
        replay = self.reserve()
        self.assertEqual(first["operation_id"], replay["operation_id"])
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.status()["feedback_used"], 1)
        self.denied("REQUEST_CONFLICT", self.reserve, action="bench")
        self.denied("REQUEST_CONFLICT", self.reserve, source=SOURCE_B)
        self.assertEqual(self.status()["feedback_used"], 1)

    def test_request_ids_are_scoped_to_session(self):
        one = self.reserve()
        two = self.reserve(credentials=self.create())
        self.assertNotEqual(one["operation_id"], two["operation_id"])
        self.denied("OPERATION_NOT_FOUND", self.store.operation_status,
                    self.credentials["session_id"], self.credentials["token"], two["operation_id"])

    def test_exact_deadline_stops_feedback_grace_only_allows_known_source(self):
        c = self.create(Policy(active_seconds=10, final_grace_seconds=5))
        failed = self.reserve("failed", credentials=c)
        self.store.complete_operation(failed["operation_id"], {"status": "BUILD_FAIL"})
        self.clock.value = 1010
        self.denied("DEADLINE_REACHED", self.reserve, "late_feedback", credentials=c)
        self.denied("UNSUBMITTED_FINAL_SOURCE", self.reserve, "fresh_final", "final", SOURCE_B, c)
        status = self.status(c)
        self.assertFalse(status["feedback_allowed"])
        self.assertTrue(status["final_requires_submitted_source"])
        # Incorrect code is still a submitted source; its terminal score may be zero.
        final = self.reserve("known_final", "final", SOURCE_A, c)
        self.assertEqual(final["session"]["state"], "FINALIZING")
        self.clock.value = 5000
        result = self.store.complete_operation(final["operation_id"], {"status": "BUILD_FAIL", "score": 0})
        self.assertEqual(result["session"]["state"], "FINISHED")
        self.assertEqual(result["session"]["final_report"]["score"], 0)

    def test_grace_expiry_persists_even_on_denial_and_clock_rollback_cannot_reopen(self):
        c = self.create(Policy(active_seconds=10, final_grace_seconds=5))
        first = self.reserve(credentials=c)
        self.store.complete_operation(first["operation_id"], {"status": "BUILD_FAIL"})
        self.clock.value = 1015
        self.denied("SESSION_CLOSED", self.reserve, "late", "final", SOURCE_A, c)
        self.clock.value = 1000
        self.assertEqual(self.status(c)["state"], "EXPIRED")
        self.denied("SESSION_CLOSED", self.reserve, "again", credentials=c)
        replay = self.reserve(credentials=c)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["operation_id"], first["operation_id"])

    def test_zero_grace_expires_at_normal_deadline(self):
        c = self.create(Policy(active_seconds=10, final_grace_seconds=0))
        self.clock.value = 1010
        self.assertEqual(self.status(c)["state"], "EXPIRED")
        self.denied("SESSION_CLOSED", self.reserve, "final", "final", SOURCE_A, c)

    def test_final_is_exactly_once_blocks_feedback_and_freezes_worker_report(self):
        # A novel source is permitted for final before the normal deadline.
        final = self.reserve("final", "final", SOURCE_B)
        self.denied("SESSION_CLOSED", self.reserve, "check_after_final")
        self.denied("SESSION_CLOSED", self.reserve, "another_final", "final", SOURCE_A)
        replay = self.reserve("final", "final", SOURCE_B)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["operation_id"], final["operation_id"])
        report = {"status": "SCORED", "score": 23.0, "source_sha256": SOURCE_B}
        self.store.complete_operation(final["operation_id"], report)
        self.store.complete_operation(final["operation_id"], dict(reversed(list(report.items()))))
        self.assertEqual(self.status()["final_report"], report)
        self.denied("RESULT_CONFLICT", self.store.complete_operation, final["operation_id"],
                    {"status": "SCORED", "score": 100})
        self.denied("SESSION_CLOSED", self.reserve, "retry_after_score", "final")
        self.assertEqual(self.status()["feedback_used"], 0)

    def test_cancel_final_cannot_be_undone_by_late_worker(self):
        check_session = self.create()
        check = self.reserve(credentials=check_session)
        self.store.admin_cancel_session(check_session["session_id"], reason="user stopped")
        final = self.reserve("final", "final")
        cancelled = self.store.admin_cancel_session(self.credentials["session_id"], reason="user stopped")
        self.assertEqual(cancelled["state"], "CANCELLED")
        self.assertEqual(cancelled["feedback_used"], 0)
        self.assertEqual(self.status(check_session)["feedback_used"], 1)
        for operation in (check, final):
            self.denied("OPERATION_CANCELLED", self.store.complete_operation,
                        operation["operation_id"], {"status": "SCORED", "score": 100})
        self.denied("SESSION_CLOSED", self.reserve, "new")
        self.assertEqual(self.store.admin_cancel_session(self.credentials["session_id"], reason="again")["cancel_reason"], "user stopped")
        self.assertIsNone(self.status()["final_report"])

    def test_late_feedback_completion_starts_grace_without_reopening_feedback(self):
        c = self.create(Policy(active_seconds=1, final_grace_seconds=1))
        feedback = self.reserve(credentials=c)
        self.clock.value = 1002
        self.store.complete_operation(feedback["operation_id"], {"status": "CHECK_PASS"})
        self.assertEqual(self.status(c)["state"], "OPEN")
        self.assertEqual(self.status(c)["stop_at"], 1001)
        self.assertEqual(self.status(c)["final_grace_started_at"], 1002)
        self.assertEqual(self.status(c)["final_deadline"], 1003)
        self.denied("DEADLINE_REACHED", self.reserve, "no_more_feedback", credentials=c)
        self.assertIsNone(self.status(c)["final_report"])
        self.assertIsNone(self.status(c)["pending_operation_id"])
        self.clock.value = 1003
        self.assertEqual(self.status(c)["state"], "EXPIRED")

    def test_concurrent_threads_cannot_overspend_or_duplicate_request(self):
        c = self.create(Policy(max_feedback=3))
        barrier = threading.Barrier(10)

        def request(index):
            store = BudgetStore(self.db, clock=self.clock)
            barrier.wait()
            for _ in range(1000):
                try:
                    result = store.reserve(c["session_id"], c["token"], request_id=f"thread-{index}",
                                           action="check", source_sha256=SOURCE_A)
                    store.complete_operation(result["operation_id"], {"status": "BUILD_FAIL"})
                    return result
                except BudgetError as error:
                    if error.code != "OPERATION_PENDING":
                        return error.code
                    time.sleep(.001)
            raise AssertionError("Pending operation did not complete")

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(request, range(10)))
        self.assertEqual(sum(isinstance(x, dict) for x in results), 3)
        self.assertEqual(results.count("FEEDBACK_EXHAUSTED"), 7)
        self.assertEqual(self.status(c)["feedback_used"], 3)

        c = self.create()
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda _: self.reserve("same", credentials=c), range(10)))
        self.assertEqual(sum(x["created"] for x in results), 1)
        self.assertEqual(len({x["operation_id"] for x in results}), 1)
        self.assertEqual(self.status(c)["feedback_used"], 1)

    def test_concurrent_final_reservations_have_one_winner(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            def request(index):
                try:
                    return self.reserve(f"final-{index}", "final")
                except BudgetError as error:
                    return error.code
            results = list(pool.map(request, range(8)))
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1)
        self.assertEqual(results.count("SESSION_CLOSED"), 7)

    def test_independent_processes_share_authoritative_quota(self):
        c = self.create(Policy(max_feedback=2))
        script = """import json,sys,time
from engine.budget import BudgetStore,BudgetError
db,sid,token,request=sys.argv[1:]
store=BudgetStore(db,clock=lambda:1000)
for _ in range(1000):
 try:
  r=store.reserve(sid,token,request_id=request,action='bench',source_sha256='a'*64)
  store.complete_operation(r['operation_id'], {'status':'BUILD_FAIL'})
  print(json.dumps({'created':r['created']}));break
 except BudgetError as e:
  if e.code == 'OPERATION_PENDING': time.sleep(.001);continue
  print(json.dumps({'denied':e.code}));break
else: raise RuntimeError('Pending operation did not complete')
"""
        workers = [subprocess.Popen([sys.executable, "-c", script, str(self.db),
                                     c["session_id"], c["token"], f"process-{i}"],
                                    cwd=Path(__file__).resolve().parents[1],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                   for i in range(6)]
        results = []
        for process in workers:
            out, err = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, err)
            results.append(json.loads(out))
        self.assertEqual(sum(x.get("created", False) for x in results), 2)
        self.assertEqual(sum(x.get("denied") == "FEEDBACK_EXHAUSTED" for x in results), 4)
        self.assertEqual(self.status(c)["feedback_used"], 2)

    def test_pending_workspace_does_not_start_timer_and_start_is_idempotent(self):
        c = self.create(start=False)
        self.clock.value = 10000
        status = self.status(c)
        self.assertEqual(status["state"], "PENDING")
        self.assertIsNone(status["started_at"])
        self.assertIsNone(status["feedback_deadline"])
        self.assertEqual(status["remaining_wall_seconds"], 3600)
        self.denied("SESSION_NOT_STARTED", self.reserve, credentials=c)
        started = self.store.start_session(c["session_id"], c["token"])
        self.assertEqual(started["started_at"], 10000)
        self.assertEqual(started["feedback_deadline"], 13600)
        self.clock.value = 10100
        restarted = self.store.start_session(c["session_id"], c["token"])
        self.assertEqual(restarted["started_at"], 10000)
        self.assertEqual(restarted["remaining_wall_seconds"], 3500)

    def test_resource_busy_does_not_charge_time_and_expired_idle_lease_releases(self):
        self.create(Policy(active_seconds=10, final_grace_seconds=5), resource_id="npu-0")
        waiting = self.create(resource_id="npu-0", start=False)
        self.clock.value = 1014
        self.denied("RESOURCE_BUSY", self.store.start_session, waiting["session_id"], waiting["token"])
        self.assertEqual(self.status(waiting)["state"], "PENDING")
        self.assertEqual(self.status(waiting)["remaining_wall_seconds"], 3600)
        self.clock.value = 1015
        started = self.store.start_session(waiting["session_id"], waiting["token"])
        self.assertEqual(started["started_at"], 1015)
        self.assertEqual(started["feedback_deadline"], 4615)

    def test_pending_work_keeps_resource_lease_through_deferred_grace(self):
        c = self.create(Policy(active_seconds=1, final_grace_seconds=1), resource_id="npu-0")
        operation = self.reserve(credentials=c)
        waiting = self.create(resource_id="npu-0", start=False)
        self.clock.value = 1002
        self.denied("RESOURCE_BUSY", self.store.start_session, waiting["session_id"], waiting["token"])
        self.assertEqual(self.status(c)["state"], "OPEN")
        self.assertIsNone(self.status(c)["final_deadline"])
        self.store.complete_operation(operation["operation_id"], {"status": "TIMEOUT"})
        self.denied("RESOURCE_BUSY", self.store.start_session, waiting["session_id"], waiting["token"])
        self.clock.value = 1003
        self.assertEqual(self.store.start_session(waiting["session_id"], waiting["token"])["state"], "OPEN")

    def test_final_execution_keeps_lease_until_completion_even_after_grace(self):
        c = self.create(Policy(active_seconds=1, final_grace_seconds=1), resource_id="npu-0")
        final = self.reserve("final", "final", credentials=c)
        waiting = self.create(resource_id="npu-0", start=False)
        self.clock.value = 10000
        self.denied("RESOURCE_BUSY", self.store.start_session, waiting["session_id"], waiting["token"])
        self.store.complete_operation(final["operation_id"], {"status": "SCORED", "score": 12})
        self.assertEqual(self.store.start_session(waiting["session_id"], waiting["token"])["state"], "OPEN")

    def test_concurrent_starts_have_one_resource_winner(self):
        candidates = [self.create(resource_id="npu-shared", start=False) for _ in range(8)]
        def start(candidate):
            try:
                return self.store.start_session(candidate["session_id"], candidate["token"])
            except BudgetError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(start, candidates))
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1)
        self.assertEqual(results.count("RESOURCE_BUSY"), 7)
        self.assertEqual(sum(self.status(x)["started_at"] is None for x in candidates), 7)

    def test_new_operation_requires_previous_completion_and_replay_does_not_execute(self):
        def reserve(index):
            try:
                return self.reserve(f"pending-{index}")
            except BudgetError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(8)))
        winners = [x for x in results if isinstance(x, dict)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(results.count("OPERATION_PENDING"), 7)
        operation = winners[0]
        self.assertFalse(self.reserve(operation["request_id"])["created"])
        self.assertFalse(self.status()["feedback_allowed"])
        self.assertFalse(self.status()["final_allowed"])
        self.denied("OPERATION_PENDING", self.reserve, "final", "final")
        self.store.complete_operation(operation["operation_id"], {"status": "BUILD_FAIL"})
        self.assertTrue(self.status()["feedback_allowed"])
        self.assertTrue(self.reserve("next")["created"])

    def test_cancel_pending_session_cannot_later_start(self):
        c = self.create(start=False)
        self.store.admin_cancel_session(c["session_id"], reason="not needed")
        self.denied("SESSION_CLOSED", self.store.start_session, c["session_id"], c["token"])

    def test_feedback_limit_immediately_freezes_new_final_source_before_time_limit(self):
        c = self.create(Policy(max_feedback=1, active_seconds=100, final_grace_seconds=5))
        last = self.reserve(credentials=c)
        status = last["session"]
        self.assertEqual(status["stop_at"], 1000)
        self.assertEqual(status["budget_stop_reason"], "feedback_limit")
        self.assertIsNone(status["final_deadline"])
        self.assertIsNone(status["final_grace_started_at"])
        self.assertTrue(status["final_requires_submitted_source"])
        self.denied("UNSUBMITTED_FINAL_SOURCE", self.reserve, "new_final", "final", SOURCE_B, c)
        self.store.complete_operation(last["operation_id"], {"status": "BUILD_FAIL"})
        self.assertEqual(self.status(c)["final_deadline"], 1005)
        self.assertEqual(self.status(c)["feedback_deadline"], 1100)
        self.denied("UNSUBMITTED_FINAL_SOURCE", self.reserve, "new_final", "final", SOURCE_B, c)
        final = self.reserve("known_final", "final", SOURCE_A, c)
        self.assertEqual(final["source_sha256"], SOURCE_A)
        self.assertEqual(final["session"]["feedback_used"], 1)

    def test_feedback_limit_uses_short_grace_instead_of_unused_active_time(self):
        c = self.create(Policy(max_feedback=1, active_seconds=3600, final_grace_seconds=5))
        last = self.reserve(credentials=c)
        self.store.complete_operation(last["operation_id"], {"status": "CHECK_PASS"})
        self.clock.value = 1005
        self.denied("SESSION_CLOSED", self.reserve, "final", "final", SOURCE_A, c)
        status = self.status(c)
        self.assertEqual(status["state"], "EXPIRED")
        self.assertEqual(status["closed_at"], 1005)
        self.assertEqual(status["stop_at"], 1000)

    def test_long_last_feedback_defers_grace_but_cannot_add_feedback_or_new_source(self):
        c = self.create(Policy(max_feedback=1, active_seconds=10, final_grace_seconds=5))
        last = self.reserve(credentials=c)
        self.clock.value = 10000
        status = self.status(c)
        self.assertEqual(status["stop_at"], 1000)
        self.assertEqual(status["budget_stop_reason"], "feedback_limit")
        self.assertIsNone(status["final_deadline"])
        self.denied("DEADLINE_REACHED", self.reserve, "another_check", credentials=c)
        self.denied("UNSUBMITTED_FINAL_SOURCE", self.reserve, "new_final", "final", SOURCE_B, c)
        self.denied("OPERATION_PENDING", self.reserve, "known_final", "final", SOURCE_A, c)
        result = self.store.complete_operation(last["operation_id"], {"status": "WORKER_TIMEOUT"})
        self.assertEqual(result["session"]["final_grace_started_at"], 10000)
        self.assertEqual(result["session"]["final_deadline"], 10005)
        self.assertEqual(result["session"]["stop_at"], 1000)
        self.clock.value = 10004
        self.assertTrue(self.reserve("known_final", "final", SOURCE_A, c)["created"])

    def test_time_limit_defers_grace_for_pending_feedback_and_completion_replay_cannot_extend(self):
        c = self.create(Policy(active_seconds=10, final_grace_seconds=5))
        self.clock.value = 1009
        operation = self.reserve(credentials=c)
        self.clock.value = 1010
        status = self.status(c)
        self.assertEqual(status["stop_at"], 1010)
        self.assertEqual(status["budget_stop_reason"], "time_limit")
        self.assertIsNone(status["final_deadline"])
        self.clock.value = 1020
        report = {"status": "CORRECTNESS_FAIL"}
        self.store.complete_operation(operation["operation_id"], report)
        self.assertEqual(self.status(c)["final_deadline"], 1025)
        self.clock.value = 1024
        self.store.complete_operation(operation["operation_id"], report)
        self.assertEqual(self.status(c)["final_deadline"], 1025)
        self.assertEqual(self.status(c)["final_grace_started_at"], 1020)
        self.denied("UNSUBMITTED_FINAL_SOURCE", self.reserve, "fresh", "final", SOURCE_B, c)
        self.assertTrue(self.reserve("historical", "final", SOURCE_A, c)["created"])

    def test_history_is_authenticated_scoped_ordered_and_preserves_failed_pending_records(self):
        unrelated = self.create()
        self.reserve("other", credentials=unrelated)
        first = self.reserve("z-first")
        self.store.complete_operation(first["operation_id"], {"status": "BUILD_FAIL", "score": 0})
        second = self.reserve("a-second", "bench", SOURCE_B)
        history = self.store.history(self.credentials["session_id"], self.credentials["token"])
        self.assertEqual([x["request_id"] for x in history["operations"]], ["z-first", "a-second"])
        self.assertEqual(history["operations"][0]["report"]["status"], "BUILD_FAIL")
        self.assertEqual(history["operations"][1]["state"], "RESERVED")
        self.assertEqual(history["operations"][1]["operation_id"], second["operation_id"])
        serialized = json.dumps(history)
        self.assertNotIn(self.credentials["token"], serialized)
        self.assertNotIn("owner_id", serialized)
        self.assertNotIn("token_hash", serialized)
        self.assertNotIn(unrelated["session_id"], serialized)
        self.denied("AUTH_FAILED", self.store.history, self.credentials["session_id"], unrelated["token"])
        self.denied("AUTH_FAILED", self.store.history, "missing", self.credentials["token"])

    def test_cleanup_legacy_expired_pending_keeps_expired_and_releases_lease(self):
        c = self.create(resource_id="npu-cleanup")
        operation = self.reserve(credentials=c)
        waiting = self.create(resource_id="npu-cleanup", start=False)
        # Reproduce an interrupted old worker/old schema state. New deadlines
        # defer expiry while a feedback is pending, but cleanup must handle
        # pre-existing terminal sessions without reopening them.
        with sqlite3.connect(str(self.db)) as db:
            db.execute("UPDATE sessions SET state='EXPIRED',closed_at=1000 WHERE session_id=?",
                       (c["session_id"],))
        self.denied("RESOURCE_BUSY", self.store.start_session, waiting["session_id"], waiting["token"])
        cancelled = self.store.admin_cancel_session(c["session_id"], reason="worker terminated")
        self.assertEqual(cancelled["state"], "EXPIRED")
        self.assertEqual(cancelled["feedback_used"], 1)
        self.assertEqual(cancelled["closed_at"], 1000)
        self.assertIsNone(cancelled["pending_operation_id"])
        self.denied("OPERATION_CANCELLED", self.store.complete_operation,
                    operation["operation_id"], {"status": "CHECK_PASS"})
        self.denied("SESSION_CLOSED", self.store.start_session, c["session_id"], c["token"])
        self.assertEqual(self.store.start_session(waiting["session_id"], waiting["token"])["state"], "OPEN")

    def test_zero_grace_last_feedback_expires_when_result_recorded(self):
        c = self.create(Policy(max_feedback=1, final_grace_seconds=0))
        operation = self.reserve(credentials=c)
        self.assertEqual(self.status(c)["state"], "OPEN")
        self.clock.value = 1001
        result = self.store.complete_operation(operation["operation_id"], {"status": "BUILD_FAIL"})
        self.assertEqual(result["session"]["state"], "EXPIRED")
        self.assertEqual(result["session"]["final_deadline"], 1001)
        self.denied("SESSION_CLOSED", self.reserve, "final", "final", SOURCE_A, c)

    def test_cancel_unused_session_is_authenticated_idempotent_and_releases_resource(self):
        for started in (False, True):
            with self.subTest(started=started):
                resource = 'unused-' + str(started)
                credentials = self.create(resource_id=resource, start=started)
                waiting = self.create(resource_id=resource, start=False)
                self.denied('AUTH_FAILED', self.store.cancel_if_no_submission,
                            credentials['session_id'], 'wrong')
                self.assertEqual(self.status(credentials)['state'], 'OPEN' if started else 'PENDING')
                with self.assertRaises(ValueError):
                    self.store.cancel_if_no_submission(credentials['session_id'], credentials['token'], reason='')
                self.assertTrue(self.store.cancel_if_no_submission(
                    credentials['session_id'], credentials['token'], reason='driver exited'))
                self.assertEqual(self.status(credentials)['state'], 'CANCELLED')
                self.assertEqual(self.status(credentials)['feedback_used'], 0)
                self.assertTrue(self.store.cancel_if_no_submission(
                    credentials['session_id'], credentials['token'], reason='another call'))
                self.assertEqual(self.status(credentials)['cancel_reason'], 'driver exited')
                self.assertEqual(self.store.start_session(waiting['session_id'], waiting['token'])['state'], 'OPEN')

    def test_cancel_unused_does_not_touch_pending_or_completed_submission(self):
        credentials = self.create(resource_id='reserved-resource')
        waiting = self.create(resource_id='reserved-resource', start=False)
        operation = self.reserve(credentials=credentials)
        self.assertFalse(self.store.cancel_if_no_submission(credentials['session_id'], credentials['token']))
        self.assertEqual(self.status(credentials)['pending_operation_id'], operation['operation_id'])
        self.denied('RESOURCE_BUSY', self.store.start_session, waiting['session_id'], waiting['token'])
        self.store.complete_operation(operation['operation_id'], {'status': 'BUILD_FAIL'})
        self.assertFalse(self.store.cancel_if_no_submission(credentials['session_id'], credentials['token']))
        self.assertEqual(self.status(credentials)['state'], 'OPEN')
        self.assertEqual(self.status(credentials)['feedback_used'], 1)
        final = self.reserve('historical-final', 'final', credentials=credentials)
        self.assertFalse(self.store.cancel_if_no_submission(credentials['session_id'], credentials['token']))
        self.assertEqual(self.status(credentials)['state'], 'FINALIZING')
        self.store.complete_operation(final['operation_id'], {'status': 'BUILD_FAIL', 'score': 0})
        self.assertEqual(self.status(credentials)['state'], 'FINISHED')

    def test_cancel_unused_preserves_expired_state_and_legacy_pending_worker(self):
        unused = self.create(Policy(active_seconds=1, final_grace_seconds=0))
        self.clock.value = 1001
        self.assertTrue(self.store.cancel_if_no_submission(unused['session_id'], unused['token']))
        self.assertEqual(self.status(unused)['state'], 'EXPIRED')
        self.assertEqual(self.status(unused)['closed_at'], 1001)
        active = self.create(resource_id='legacy-pending')
        operation = self.reserve(credentials=active)
        with sqlite3.connect(str(self.db)) as db:
            db.execute("UPDATE sessions SET state='EXPIRED',closed_at=1001 WHERE session_id=?", (active['session_id'],))
        self.assertFalse(self.store.cancel_if_no_submission(active['session_id'], active['token']))
        self.assertEqual(self.status(active)['pending_operation_id'], operation['operation_id'])
        waiting = self.create(resource_id='legacy-pending', start=False)
        self.denied('RESOURCE_BUSY', self.store.start_session, waiting['session_id'], waiting['token'])

    def test_first_reservation_and_unused_cancellation_have_only_one_winner(self):
        for index in range(8):
            credentials = self.create()
            barrier = threading.Barrier(2)
            def submit():
                store = BudgetStore(self.db, clock=self.clock)
                barrier.wait()
                try:
                    return store.reserve(credentials['session_id'], credentials['token'],
                        request_id='first', action='check', source_sha256=SOURCE_A)
                except BudgetError as error:
                    return error.code
            def cancel():
                store = BudgetStore(self.db, clock=self.clock)
                barrier.wait()
                return store.cancel_if_no_submission(credentials['session_id'], credentials['token'])
            with ThreadPoolExecutor(max_workers=2) as pool:
                submitting, cancelling = pool.submit(submit), pool.submit(cancel)
                admitted, cancelled = submitting.result(), cancelling.result()
            history = self.store.history(credentials['session_id'], credentials['token'])
            if cancelled:
                self.assertEqual(admitted, 'SESSION_CLOSED')
                self.assertEqual(history['session']['state'], 'CANCELLED')
                self.assertEqual(history['operations'], [])
            else:
                self.assertIsInstance(admitted, dict)
                self.assertEqual(history['session']['state'], 'OPEN')
                self.assertEqual(history['session']['pending_operation_id'], admitted['operation_id'])
                self.assertEqual(len(history['operations']), 1)
                self.assertEqual(history['operations'][0]['state'], 'RESERVED')


if __name__ == "__main__":
    unittest.main()
