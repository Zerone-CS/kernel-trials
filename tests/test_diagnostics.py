import unittest

from engine.diagnostics import capability_profile, suggest_final_source


class DiagnosticsTests(unittest.TestCase):
    def session(self):
        return dict(session_id="s", problem_digest="p", track="from_scratch",
                    policy={"max_feedback": 20}, started_at=100, feedback_used=2)

    def op(self, n, outcome, stages=None, action="check", **extra):
        return dict(session_id="s", operation_id=str(n), reserved_at=100+n,
                    completed_at=102+n, source_sha256=str(n)*64, action=action,
                    state="FINISHED", report=dict(outcome=outcome, stages=stages or {}, **extra))

    def test_no_compile_success_does_not_manufacture_partial_score(self):
        ops = [self.op(1, "compile_failed", {"build": "failed"}),
               self.op(2, "compile_failed", {"build": "failed"}, "final")]
        result = capability_profile(self.session(), ops)
        self.assertEqual(result["task_result"]["task_score"], 0)
        self.assertEqual(result["highest_verified_stage"], "none")
        self.assertTrue(result["all_observed_attempts_failed_compilation"])
        self.assertFalse(result["interpretation"]["process_metrics_are_scores"])

    def test_old_success_survives_last_broken_source_but_is_not_final_grade(self):
        good = self.op(1, "passed", dict(build="passed", runtime="passed", correctness="passed"),
                       correctness=dict(passed=4,total=4,scope="public_full"))
        bad = self.op(2, "compile_failed", {"build": "failed"})
        result = capability_profile(self.session(), [good, bad])
        self.assertEqual(result["highest_verified_stage"], "correctness")
        self.assertIsNone(result["task_result"]["task_score"])
        self.assertEqual(suggest_final_source([good, bad])["source_sha256"], good["source_sha256"])
        self.assertEqual(result["first_verified"]["build"]["elapsed_seconds"], 3)

    def test_infrastructure_is_invalid_and_cannot_earn_milestone(self):
        op = self.op(1, "infrastructure_error", dict(build="passed"), "final")
        result = capability_profile(self.session(), [op])
        self.assertIsNone(result["task_result"]["task_score"])
        self.assertEqual(result["highest_verified_stage"], "none")
        self.assertFalse(result["all_observed_attempts_failed_compilation"])
        self.assertTrue(result["contains_infrastructure_failure"])

    def test_partial_cases_do_not_beat_whole_suite(self):
        old = self.op(1, "passed", dict(build="passed",runtime="passed",correctness="passed"),
                      correctness=dict(passed=4,total=4,scope="public_full"))
        subset = self.op(2, "passed", dict(build="passed",runtime="passed",correctness="passed"),
                         correctness=dict(passed=1,total=1,scope="public_subset"))
        self.assertEqual(suggest_final_source([old, subset])["source_sha256"], old["source_sha256"])

    def test_unchanged_optimize_starter_is_explicit(self):
        op = self.op(1, "passed", dict(build="passed",runtime="passed",correctness="passed"))
        result = capability_profile(self.session(), [op], starter_sha256=op["source_sha256"])
        self.assertTrue(result["trace"][0]["is_unchanged_starter"])
        self.assertFalse(result["interpretation"]["starter_success_is_agent_improvement"])

    def test_mixed_session_evidence_rejected(self):
        op = self.op(1, "compile_failed")
        op["session_id"] = "another"
        with self.assertRaises(ValueError):
            capability_profile(self.session(), [op])

    def test_equal_timestamp_uses_authoritative_history_order_not_random_uuid(self):
        first = self.op(1, "compile_failed", {"build": "failed"})
        second = self.op(2, "passed", {"build": "passed", "runtime": "passed"})
        first.update(operation_id="zzzz-first", reserved_at=100)
        second.update(operation_id="aaaa-second", reserved_at=100)
        profile = capability_profile(self.session(), [first, second])
        self.assertEqual([entry["operation_id"] for entry in profile["trace"]], ["zzzz-first", "aaaa-second"])
        self.assertEqual(profile["first_verified"]["build"]["attempt"], 2)
        second["report"] = {"outcome": "compile_failed", "stages": {"build": "failed"}}
        self.assertEqual(suggest_final_source([first, second])["source_sha256"], second["source_sha256"])

    def test_full_public_scope_outranks_later_compiled_wrong_candidate(self):
        correct = self.op(1, "passed", dict(build="passed", runtime="passed", correctness="passed"),
                          correctness=dict(passed=2, total=2, scope="public_full"))
        wrong = self.op(2, "correctness_failed", dict(build="passed", runtime="passed", correctness="failed"),
                        correctness=dict(passed=0, total=2, scope="public_full"))
        self.assertEqual(suggest_final_source([correct, wrong])["source_sha256"], correct["source_sha256"])

    def test_real_runner_error_category_is_preserved_without_turning_it_into_score(self):
        operation = self.op(1, "compile_failed", {"build": "failed"}, "final",
                            error={"stage": "build", "category": "compiler_exit_nonzero"})
        result = capability_profile(self.session(), [operation])
        self.assertEqual(result["trace"][0]["error_category"], "compiler_exit_nonzero")
        self.assertEqual(result["task_result"]["task_score"], 0)

    def test_no_final_means_unavailable_score_even_if_feedback_report_has_score(self):
        operation = self.op(1, "passed", dict(build="passed", runtime="passed", correctness="passed", performance="passed"),
                            score=95)
        result = capability_profile(self.session(), [operation])
        self.assertIsNone(result["task_result"]["task_score"])
        self.assertEqual(result["task_result"]["status"], "no_final_result")

    def test_failed_prerequisite_blocks_downstream_milestones(self):
        operation = self.op(1, "compile_failed", dict(build="failed", runtime="passed", correctness="passed", performance="passed"))
        result = capability_profile(self.session(), [operation])
        self.assertEqual(result["highest_verified_stage"], "none")
        self.assertTrue(all(value is None for value in result["first_verified"].values()))

    def test_pending_or_cancelled_work_never_establishes_milestones(self):
        pending = self.op(1, "passed", dict(build="passed", runtime="passed", correctness="passed"))
        pending["state"] = "RESERVED"
        cancelled = self.op(2, "passed", dict(build="passed", runtime="passed", correctness="passed"))
        cancelled["state"] = "CANCELLED"
        result = capability_profile(self.session(), [pending, cancelled])
        self.assertEqual(result["completed_operations"], 0)
        self.assertEqual(result["highest_verified_stage"], "none")
        self.assertFalse(result["all_observed_attempts_failed_compilation"])

    def test_failed_final_cannot_inherit_earlier_successful_feedback_score(self):
        good = self.op(1, "passed", dict(build="passed", runtime="passed", correctness="passed", performance="passed"), score=98)
        final = self.op(2, "correctness_failed", dict(build="passed", runtime="passed", correctness="failed"), "final")
        result = capability_profile(self.session(), [good, final])
        self.assertEqual(result["task_result"]["task_score"], 0)
        self.assertEqual(result["highest_verified_stage"], "performance")
        self.assertFalse(result["interpretation"]["public_correctness_is_final_correctness"])

    def test_no_operations_yields_no_unearned_failure_or_score(self):
        result = capability_profile(self.session(), [])
        self.assertIsNone(result["task_result"]["task_score"])
        self.assertFalse(result["all_observed_attempts_failed_compilation"])
        self.assertEqual(result["submitted_versions"], 0)
        self.assertIsNone(suggest_final_source([]))
