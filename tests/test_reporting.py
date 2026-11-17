import copy
import unittest

from engine.diagnostics import capability_profile
from engine.reporting import build_report, render_markdown


class ReportingTests(unittest.TestCase):
    def session(self, purpose="agent_evaluation"):
        return {"session_id": "s", "state": "FINISHED", "problem_digest": "p" * 64,
                "track": "optimize", "policy": {"max_feedback": 20, "active_seconds": 3600},
                "feedback_used": 2, "feedback_remaining": 18, "started_at": 100,
                "claimed_agent_metadata": {"evaluation_purpose": purpose},
                "agent_metadata_verified": False}

    def op(self, n=1, action="final", outcome="passed", score=80):
        return {"session_id": "s", "operation_id": str(n), "action": action,
                "state": "FINISHED", "source_sha256": str(n) * 64,
                "reserved_at": 100+n, "completed_at": 110+n,
                "report": {"outcome": outcome, "score": score, "score_status": "COMPLETE_LOCAL_REFERENCE_SCORE",
                           "stages": dict(build="passed", runtime="passed", correctness="passed", performance="passed"),
                           "correctness": dict(passed=39, total=39, executed=39, scope="public_and_hidden_full"),
                           "security": dict(mode="development", rankable=False, security_label="not_cheat_resistant"),
                           "metric": "msprof_device_task_duration",
                           "baseline_identity": {"baseline_id": "frozen", "source_sha256": "f" * 64, "package_digest": "p" * 64},
                           "scoring": {"formula": "frozen formula", "cases": [
                               dict(case_id="shape1", median_us=8, reference_us=10, case_score=100, measurement_valid=True)]}}}

    def build(self, session, operations):
        return build_report(session, operations, capability_profile(session, operations, starter_sha256="1" * 64))

    def test_no_final_never_inherits_successful_public_score(self):
        report = self.build(self.session(), [self.op(action="bench", score=100)])
        self.assertEqual(report["result"]["outcome"], "no_final_result")
        self.assertIsNone(report["result"]["score"])
        self.assertFalse(report["result"]["rankable"])

    def test_failed_final_does_not_inherit_public_success(self):
        report = self.build(self.session(), [self.op(action="bench"), self.op(2, outcome="compile_failed", score=None)])
        self.assertEqual(report["result"]["score"], 0)

    def test_invalid_measurement_or_infrastructure_remains_unavailable(self):
        for outcome in ("measurement_invalid", "infrastructure_error", "formal_unavailable"):
            with self.subTest(outcome=outcome):
                report = self.build(self.session(), [self.op(outcome=outcome, score=0)])
                self.assertIsNone(report["result"]["score"])
                self.assertFalse(report["result"]["rankable"])

    def test_development_score_and_raw_speedup_survive_without_formal_claim(self):
        report = self.build(self.session(), [self.op()])
        self.assertEqual(report["result"]["score"], 80)
        self.assertEqual(report["comparison"]["timing_cases"][0]["speedup"], 1.25)
        self.assertFalse(report["comparison"]["reference_is_theoretical_optimum"])
        self.assertFalse(report["result"]["rankable"])
        markdown = render_markdown(report)
        self.assertIn("1.25×", markdown)
        self.assertIn("frozen", markdown)
        self.assertIn("not_cheat_resistant", markdown)
        self.assertIn("不是理论最优", markdown)
        self.assertIn("80/100", markdown)

    def test_smoke_label_comes_from_session_and_cannot_be_overridden_by_kernel_report(self):
        operation = self.op()
        operation["report"]["evaluation_purpose"] = "agent_evaluation"
        operation["report"]["security"]["rankable"] = True
        report = self.build(self.session("entry_smoke"), [operation])
        self.assertEqual(report["session"]["evaluation_purpose"], "entry_smoke")
        self.assertIn("不评价 Agent", report["summary"])
        self.assertFalse(report["result"]["rankable"])

    def test_launcher_and_attempt_are_bound_session_metadata_not_kernel_claims(self):
        session = self.session()
        session["claimed_agent_metadata"].update(launcher_id="launcher-a", attempt_index=3)
        operation = self.op()
        operation["report"].update(launcher_id="forged-launcher", attempt_index=1)
        report = self.build(session, [operation])
        self.assertEqual(report["session"]["launcher_id"], "launcher-a")
        self.assertEqual(report["session"]["attempt_index"], 3)
        self.assertFalse(report["session"]["agent_metadata_verified"])
        self.assertEqual(report["result"]["score"], 80)
        markdown = render_markdown(report)
        self.assertIn("| Launcher 标识 | launcher-a |", markdown)
        self.assertIn("| 实验轮次 | 3 |", markdown)
        self.assertNotIn("forged-launcher", markdown)

    def test_legacy_session_does_not_invent_a_launcher_or_first_attempt(self):
        report = self.build(self.session(), [self.op()])
        self.assertIsNone(report["session"]["launcher_id"])
        self.assertIsNone(report["session"]["attempt_index"])
        markdown = render_markdown(report)
        self.assertIn("| Launcher 标识 | 未提供 |", markdown)
        self.assertIn("| 实验轮次 | 未提供 |", markdown)

    def test_malformed_attempt_metadata_is_unavailable_not_a_fabricated_ordinal(self):
        for index in (None, 0, -1, True, "3", 3.5):
            session = self.session()
            session["claimed_agent_metadata"].update(launcher_id="", attempt_index=index)
            with self.subTest(index=index):
                report = self.build(session, [self.op()])
                self.assertIsNone(report["session"]["attempt_index"])
                self.assertIsNone(report["session"]["launcher_id"])

    def test_report_excludes_arbitrary_logs_and_private_oracle_fields(self):
        operation = self.op()
        operation["report"]["private_oracle"] = "secret-expected-values"
        operation["report"]["build_diagnostics"] = "large-private-log"
        report = self.build(self.session(), [operation])
        rendered = repr(report) + render_markdown(report)
        self.assertNotIn("secret-expected-values", rendered)
        self.assertNotIn("large-private-log", rendered)

    def test_cross_session_source_and_multiple_final_records_rejected(self):
        operation = self.op()
        diagnosis = capability_profile(self.session(), [operation])
        for mutation in ("session", "source", "multiple"):
            bad = copy.deepcopy(operation)
            operations = [bad]
            if mutation == "session":
                bad["session_id"] = "foreign"
            elif mutation == "source":
                bad["report"]["source_sha256"] = "different"
            else:
                operations.append(self.op(2))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                build_report(self.session(), operations, diagnosis)
