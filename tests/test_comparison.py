import copy
import json
import math
import unittest

from engine.comparison import build_comparison, render_comparison
from engine.diagnostics import capability_profile
from engine.reporting import build_report


class ComparisonTests(unittest.TestCase):
    def manifest(self, repeats=2, tasks=None):
        tasks = tasks or ["case09"]
        return {"experiment_id": "exp1", "name": "method trial", "track": "from_scratch",
                "tasks": [{"id": task} for task in tasks], "repeats": repeats, "control": "baseline",
                "conditions": [{"id": "baseline", "label": "Unassisted", "method_digest": "baseline-method"},
                               {"id": "method", "label": "Method", "method_digest": "new-method"}],
                "policy": {"max_feedback": 20, "active_seconds": 3600, "final_grace_seconds": 600},
                "task_metadata": {task: {"capabilities": ["large_gemm", "layout"],
                                         "scope": "full_operator" if task == "full" else "fixed_shape"}
                                  for task in tasks}}

    def trial(self, condition="baseline", repeat=1, task="case09", outcome="passed", latency=10, score=80):
        name = task + "-" + condition + "-" + str(repeat)
        policy = self.manifest()["policy"]
        report = {
            "schema_version": 1, "kind": "operator_evaluation_report",
            "session": {"session_id": "session-" + name, "state": "FINISHED", "problem_digest": "problem-" + task,
                        "track": "from_scratch", "attempt_index": repeat + 40},
            "budget": {"policy": policy, "feedback_used": 2},
            "result": {"outcome": outcome, "score": score, "final_operation_id": "final-" + name,
                       "stages": dict(build="passed", runtime="passed", correctness="passed", performance="passed"),
                       "correctness": dict(passed=4, total=4, executed=4, scope="public_and_hidden_full"),
                       "metric": "msprof_device_task_duration"},
            "comparison": {"baseline_identity": {"baseline_id": "baseline-" + task, "package_digest": "problem-" + task},
                           "protocol_digest": "protocol-" + task, "environment_fingerprint": "device1",
                           "formula": "100/(1+log1.5(max(1,t/tref)))",
                           "timing_cases": [dict(case_id="public_" + task, candidate_us=latency, measurement_valid=True)]},
            "diagnostics": {"first_verified": {"correctness": dict(attempt=2, operation_id="b", action="bench",
                                                                      scope="public_full", elapsed_seconds=12)},
                            "trace": [dict(attempt=1, operation_id="a", action="check"),
                                      dict(attempt=2, operation_id="b", action="bench"),
                                      dict(attempt=3, operation_id="c", action="final")]}}
        return {"trial_id": name, "task_id": task, "condition_id": condition, "repeat_index": repeat,
                "status": "FINISHED", "report": report, "report_path": "reports/" + name + ".json",
                "runner_metadata": {"provider": "test", "model_claim": "claimed-model"}, "execution_error": None}

    def condition(self, report, condition="method", task="case09"):
        return next(c for t in report["tasks"] if t["task_id"] == task for c in t["conditions"] if c["condition_id"] == condition)

    def test_all_planned_trials_remain_in_success_denominator(self):
        manifest = self.manifest(3)
        trials = [self.trial("method"), self.trial("method", 2, outcome="compile_failed", score=90)]
        report = build_comparison(manifest, trials)
        stats = self.condition(report)
        self.assertEqual(len(report["trials"]), 6)
        self.assertEqual((stats["final_correct"], stats["candidate_failed"], stats["correctness_unknown"]), (1, 1, 1))
        self.assertEqual(stats["correctness_success_rate"], 1 / 3)
        self.assertEqual(stats["missing_trials"], 1)
        self.assertEqual(stats["task_score"]["mean"], 40)
        self.assertEqual(stats["task_score"]["unavailable"], 1)

    def test_infrastructure_never_becomes_zero_score_or_candidate_failure(self):
        trial = self.trial("method", outcome="infrastructure_error", score=0)
        stats = self.condition(build_comparison(self.manifest(1), [trial]))
        self.assertEqual(stats["infrastructure_invalid"], 1)
        self.assertEqual(stats["candidate_failed"], 0)
        self.assertEqual(stats["correctness_unknown"], 1)
        self.assertIsNone(stats["task_score"]["mean"])

    def test_correct_but_invalid_measurement_keeps_correctness_without_score(self):
        trial = self.trial("method", outcome="measurement_invalid", score=100)
        report = build_comparison(self.manifest(1), [self.trial(), trial])
        stats = self.condition(report)
        self.assertEqual(stats["final_correct"], 1)
        self.assertEqual(stats["measurement_invalid"], 1)
        self.assertIsNone(stats["task_score"]["mean"])
        self.assertEqual(stats["final_latency_us"][0]["count"], 0)
        pair = report["paired_comparisons"][0]["pairs"][0]
        self.assertFalse(pair["valid_performance_pair"])
        self.assertIsNone(pair["score_delta"])

    def test_missing_or_bad_timing_cannot_make_a_passed_report_score_valid(self):
        for rows in ([], [dict(case_id="a", candidate_us=0, measurement_valid=True)],
                     [dict(case_id="a", candidate_us=10, measurement_valid=False)]):
            trial = self.trial("method")
            trial["report"]["comparison"]["timing_cases"] = rows
            with self.subTest(rows=rows):
                row = build_comparison(self.manifest(1), [trial])["trials"][1]
                self.assertTrue(row["final_correct"])
                self.assertFalse(row["measurement_valid"])
                self.assertIsNone(row["task_score"])

    def test_host_timing_cannot_substitute_for_device_latency(self):
        trial = self.trial("method")
        trial["report"]["result"]["metric"] = "acl_event_elapsed"
        row = build_comparison(self.manifest(1), [trial])["trials"][1]
        self.assertTrue(row["final_correct"])
        self.assertFalse(row["measurement_valid"])
        self.assertIsNone(row["task_score"])

    def test_pair_same_task_same_repeat_and_geometric_mean(self):
        trials = [self.trial(latency=8), self.trial("method", latency=4, score=90),
                  self.trial(repeat=2, latency=16, score=70), self.trial("method", 2, latency=4, score=95)]
        report = build_comparison(self.manifest(), list(reversed(trials)))
        comparison = report["paired_comparisons"][0]
        self.assertEqual([pair["repeat_index"] for pair in comparison["pairs"]], [1, 2])
        self.assertEqual(comparison["non_full_summary"]["valid_performance_pairs"], 2)
        self.assertAlmostEqual(comparison["non_full_summary"]["speedup"]["geometric_mean"], math.sqrt(8))
        self.assertEqual(comparison["non_full_summary"]["score_delta"]["mean"], 17.5)
        self.assertEqual(self.condition(report)["first_correct_feedback"]["mean"], 2)
        self.assertEqual(self.condition(report)["first_correct_seconds"]["mean"], 12)

    def test_failed_pair_retains_score_delta_but_not_performance(self):
        report = build_comparison(self.manifest(1), [self.trial(score=80), self.trial("method", outcome="correctness_failed")])
        pair = report["paired_comparisons"][0]["pairs"][0]
        self.assertEqual(pair["score_delta"], -80)
        self.assertFalse(pair["valid_performance_pair"])
        self.assertIn("treatment:candidate_failure", pair["reasons"])

    def test_real_compile_failure_report_without_formula_or_measured_environment_keeps_zero_delta(self):
        control, treatment = self.trial(), self.trial("method")
        session = {**treatment["report"]["session"], "policy": self.manifest()["policy"],
                   "feedback_used": 2, "started_at": 0}
        operation = {"session_id": session["session_id"], "operation_id": "failed-final", "action": "final",
                     "state": "FINISHED", "source_sha256": "a" * 64, "completed_at": 10,
                     "report": {"outcome": "compile_failed", "score": None,
                                "stages": dict(build="failed", runtime="not_run", correctness="not_run", performance="not_run")}}
        failed = build_report(session, [operation], capability_profile(session, [operation]))
        self.assertIsNone(failed["comparison"]["environment_fingerprint"])
        self.assertIsNone(failed["comparison"]["formula"])
        # Controller.report() supplements the frozen binding without pretending
        # an unsuccessful build performed an environment/timing measurement.
        failed["comparison"].update(protocol_digest="protocol-case09", bound_environment_fingerprint="device1")
        failed["comparison"]["baseline_identity"] = dict(control["report"]["comparison"]["baseline_identity"])
        treatment["report"] = failed
        report = build_comparison(self.manifest(1), [control, treatment])
        pair = report["paired_comparisons"][0]["pairs"][0]
        self.assertTrue(pair["comparable"])
        self.assertEqual(pair["score_delta"], -80)
        self.assertFalse(pair["valid_performance_pair"])
        identity = report["trials"][1]["identity"]
        self.assertEqual(identity["environment_fingerprint"], "device1")
        self.assertEqual(identity["environment_identity_source"], "baseline_binding")

    def test_environment_fallback_uses_nested_baseline_when_explicit_field_is_null(self):
        trial = self.trial("method", outcome="compile_failed")
        trial["report"]["comparison"].update(environment_fingerprint=None, formula=None)
        trial["report"]["comparison"]["baseline_identity"]["environment"] = {"fingerprint": "device1"}
        pair = build_comparison(self.manifest(1), [self.trial(), trial])["paired_comparisons"][0]["pairs"][0]
        self.assertTrue(pair["comparable"])
        self.assertEqual(pair["score_delta"], -80)

    def test_formula_difference_checked_only_when_both_have_valid_performance(self):
        trial = self.trial("method")
        trial["report"]["comparison"]["formula"] = "a different scoring rule"
        pair = build_comparison(self.manifest(1), [self.trial(), trial])["paired_comparisons"][0]["pairs"][0]
        self.assertFalse(pair["comparable"])
        self.assertIn("formula_mismatch", pair["reasons"])
        trial["report"]["result"]["outcome"] = "compile_failed"
        pair = build_comparison(self.manifest(1), [self.trial(), trial])["paired_comparisons"][0]["pairs"][0]
        self.assertTrue(pair["comparable"])
        self.assertEqual(pair["score_delta"], -80)

    def test_unmatched_successes_are_not_paired_across_repeats(self):
        report = build_comparison(self.manifest(), [self.trial(repeat=1), self.trial("method", 2)])
        comparison = report["paired_comparisons"][0]
        self.assertEqual(comparison["non_full_summary"]["planned_pairs"], 2)
        self.assertEqual(comparison["non_full_summary"]["valid_performance_pairs"], 0)
        self.assertIsNone(comparison["non_full_summary"]["speedup"]["geometric_mean"])

    def test_different_tasks_have_distinct_packages_and_never_pair_each_other(self):
        report = build_comparison(self.manifest(1, ["case09", "case10"]),
                                  [self.trial(task="case09"), self.trial("method", task="case10")])
        self.assertEqual(report["paired_comparisons"][0]["non_full_summary"]["valid_performance_pairs"], 0)
        complete = build_comparison(self.manifest(1, ["case09", "case10"]),
                                   [self.trial(condition, task=task) for task in ("case09", "case10")
                                    for condition in ("baseline", "method")])
        self.assertEqual(complete["paired_comparisons"][0]["non_full_summary"]["valid_performance_pairs"], 2)

    def test_identity_mismatches_are_explicitly_incomparable(self):
        for field in ("baseline_id", "problem_digest", "protocol_digest", "policy", "environment_fingerprint"):
            trial = self.trial("method")
            if field == "baseline_id":
                trial["report"]["comparison"]["baseline_identity"][field] = "different"
            elif field == "problem_digest":
                trial["report"]["session"][field] = "different"
            elif field == "policy":
                trial["report"]["budget"]["policy"]["max_feedback"] = 200
            else:
                trial["report"]["comparison"][field] = "different"
            with self.subTest(field=field):
                pair = build_comparison(self.manifest(1), [self.trial(), trial])["paired_comparisons"][0]["pairs"][0]
                self.assertFalse(pair["comparable"])
                self.assertFalse(pair["valid_performance_pair"])
                self.assertIsNone(pair["score_delta"])
                self.assertIn(field + "_mismatch", pair["reasons"])

    def test_expected_metadata_checks_even_when_both_reports_use_same_wrong_identity(self):
        manifest = self.manifest(1)
        manifest["task_metadata"]["case09"]["protocol_digest"] = "expected-protocol"
        report = build_comparison(manifest, [self.trial(), self.trial("method")])
        self.assertEqual(self.condition(report)["identity_incomparable"], 1)
        self.assertIsNone(self.condition(report)["task_score"]["mean"])
        self.assertFalse(report["paired_comparisons"][0]["pairs"][0]["comparable"])

    def test_method_identity_difference_is_expected_but_bound_identity_must_match_own_condition(self):
        trials = [self.trial(), self.trial("method")]
        for trial, digest in zip(trials, ("baseline-method", "new-method")):
            trial["report"]["session"]["claimed_agent_metadata"] = {"method_digest": digest, "repeat_index": 1}
        report = build_comparison(self.manifest(1), trials)
        self.assertTrue(report["paired_comparisons"][0]["pairs"][0]["comparable"])
        trials[1]["report"]["session"]["claimed_agent_metadata"]["method_digest"] = "unplanned"
        row = build_comparison(self.manifest(1), trials)["trials"][1]
        self.assertIn("method_digest_differs_from_bound_session", row["identity_issues"])

    def test_repeat_index_is_one_based_and_not_launcher_attempt(self):
        trial = self.trial("method")
        report = build_comparison(self.manifest(1), [trial])
        self.assertEqual(report["trials"][1]["repeat_index"], 1)
        self.assertEqual(report["trials"][1]["launcher_attempt_index"], 41)
        for index in (0, -1, 2, True, "1"):
            trial["repeat_index"] = index
            with self.subTest(index=index), self.assertRaises(ValueError):
                build_comparison(self.manifest(1), [trial])

    def test_duplicate_or_unknown_trials_fail_instead_of_being_hidden(self):
        for trials in ([self.trial(), self.trial()], [self.trial(task="unknown")], [self.trial("unknown")]):
            with self.subTest(trials=trials), self.assertRaises(ValueError):
                build_comparison(self.manifest(), trials)

    def test_final_first_correct_does_not_count_as_an_extra_feedback(self):
        trial = self.trial("method")
        milestone = trial["report"]["diagnostics"]["first_verified"]["correctness"]
        milestone.update(attempt=3, operation_id="c", action="final", scope="public_and_hidden_full")
        first = build_comparison(self.manifest(1), [trial])["trials"][1]["first_correct"]
        self.assertEqual(first["operation_attempt"], 3)
        self.assertEqual(first["feedback_used"], 2)
        self.assertEqual(first["action"], "final")

    def test_missing_trace_does_not_invent_feedback_count(self):
        trial = self.trial("method")
        trial["report"]["diagnostics"]["trace"] = []
        first = build_comparison(self.manifest(1), [trial])["trials"][1]["first_correct"]
        self.assertIsNone(first["feedback_used"])
        self.assertEqual(first["operation_attempt"], 2)

    def test_incomplete_trace_does_not_undercount_accepted_feedback(self):
        trial = self.trial("method")
        trial["report"]["diagnostics"]["trace"] = trial["report"]["diagnostics"]["trace"][1:]
        first = build_comparison(self.manifest(1), [trial])["trials"][1]["first_correct"]
        self.assertIsNone(first["feedback_used"])

    def test_one_session_cannot_be_replayed_as_independent_repeats(self):
        first, second = self.trial(), self.trial(repeat=2)
        second["report"]["session"]["session_id"] = first["report"]["session"]["session_id"]
        with self.assertRaisesRegex(ValueError, "session cannot be reused"):
            build_comparison(self.manifest(), [first, second])

    def test_catalog_supplies_capabilities_when_manifest_has_no_override(self):
        manifest = self.manifest(1)
        del manifest["task_metadata"]
        report = build_comparison(manifest, [])
        self.assertEqual(report["tasks"][0]["capabilities"], ["large_gemm"])

    def test_all_missing_plan_has_null_not_zero_statistics(self):
        report = build_comparison(self.manifest(), [])
        stats = self.condition(report)
        self.assertEqual(stats["planned"], 2)
        self.assertEqual(stats["correctness_unknown"], 2)
        self.assertEqual(stats["task_score"]["count"], 0)
        self.assertIsNone(stats["task_score"]["mean"])
        self.assertEqual(stats["task_score"]["unavailable"], 2)

    def test_public_success_does_not_replace_missing_final(self):
        trial = self.trial("method")
        trial["report"]["result"].update(final_operation_id=None, outcome="no_final_result", score=100)
        row = build_comparison(self.manifest(1), [trial])["trials"][1]
        self.assertIsNone(row["final_correct"])
        self.assertIsNone(row["task_score"])
        self.assertIsNotNone(row["first_correct"])

    def test_full_separate_and_overlapping_capabilities_not_new_samples(self):
        manifest = self.manifest(1, ["case09", "full"])
        report = build_comparison(manifest, [self.trial(condition, task=task)
                                            for task in ("case09", "full") for condition in ("baseline", "method")])
        self.assertEqual(report["full_tasks"], ["full"])
        self.assertEqual(len(report["capabilities"]), 2)
        for group in report["capabilities"]:
            self.assertEqual(group["task_ids"], ["case09"])
            self.assertTrue(group["overlapping_labels"])
            self.assertEqual(group["conditions"][0]["planned"], 1)
        paired = report["paired_comparisons"][0]
        self.assertEqual(paired["full_summary"]["planned_pairs"], 1)
        self.assertEqual(paired["non_full_summary"]["planned_pairs"], 1)

    def test_different_final_case_sets_cannot_pair(self):
        trial = self.trial("method")
        trial["report"]["comparison"]["timing_cases"][0]["case_id"] = "different"
        pair = build_comparison(self.manifest(1), [self.trial(), trial])["paired_comparisons"][0]["pairs"][0]
        self.assertFalse(pair["comparable"])
        self.assertIn("timing_case_set_mismatch", pair["reasons"])

    def test_controller_envelope_purity_json_and_no_single_sample_significance(self):
        manifest = self.manifest(1)
        trial = self.trial("method")
        trial["report"] = {"report": trial["report"], "markdown": "unused raw markdown"}
        original = copy.deepcopy((manifest, trial))
        report = build_comparison(manifest, [trial])
        self.assertEqual((manifest, trial), original)
        self.assertIsNone(self.condition(report)["task_score"]["std"])
        json.dumps(report, allow_nan=False)
        self.assertFalse(report["rankable"])
        self.assertEqual(report["mode"], "development")
        markdown = render_comparison(report)
        self.assertIn("不能用于正式排名", markdown)
        self.assertIn("不声称显著性", markdown)
        self.assertIn("missing_trial", markdown)
        self.assertNotIn("unused raw markdown", markdown)

    def test_entry_smoke_is_prominently_workflow_only_even_with_a_task_score(self):
        manifest = self.manifest(1)
        manifest["evaluation_purpose"] = "entry_smoke"
        report = build_comparison(manifest, [self.trial(), self.trial("method", outcome="compile_failed")])
        self.assertEqual(report["evaluation_purpose"], "entry_smoke")
        self.assertTrue(report["workflow_only"])
        self.assertIn("不评价", report["evaluation_label"])
        self.assertEqual(report["trials"][1]["task_score"], 0)
        markdown = render_comparison(report)
        self.assertTrue(markdown.startswith("# 流程验收报告（不评价方法能力）"))
        self.assertIn("仅流程验收", markdown.splitlines()[2])
        self.assertFalse(report["rankable"])

    def test_experiment_header_retains_only_public_runner_claims_and_hidden_input_policy(self):
        manifest = self.manifest(2)
        manifest["hidden_input_policy"] = "same task/repeat shares hidden seed across conditions; different repeats use different seeds"
        manifest["conditions"][0]["runner"] = {
            "provider": "claude", "model_claim": "configured-model", "configuration_digest": "config-sha",
            "env": {"API_KEY": "secret-key"}, "command": ["private-command"], "token": "private-token"}
        report = build_comparison(manifest, [])
        self.assertEqual(report["conditions"][0]["runner"], {
            "provider": "claude", "model_claim": "configured-model", "configuration_digest": "config-sha"})
        self.assertEqual(report["hidden_input_policy"], manifest["hidden_input_policy"])
        rendered = json.dumps(report) + render_comparison(report)
        for value in ("secret-key", "private-command", "private-token"):
            self.assertNotIn(value, rendered)
        markdown = render_comparison(report)
        for value in ("exp1", "from_scratch", "3600", "20", "Unassisted", "baseline-method", "configured-model", "config-sha"):
            self.assertIn(value, markdown)
        self.assertIn("公开性能输入仍由固定协议确定", markdown)


if __name__ == "__main__":
    unittest.main()
