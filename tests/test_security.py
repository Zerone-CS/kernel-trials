"""Security gates: hostile declarations must not enable unimplemented formal mode."""
from copy import deepcopy
import json
import unittest

from engine.security import (POLICY_VERSION, REQUIRED_CHECKS, SecurityError,
                             SecurityPolicy, validate_evidence)


PROBLEM = "a" * 64


def complete_declarations(profile="device_only_broker_v1", backend="future_broker"):
    return {"schema_version": 1, "policy_version": POLICY_VERSION,
            "problem_digest": PROBLEM, "deployment_id": "deployment-one",
            "operator_profile": profile, "backend_profile": backend,
            "checks": {name: {"status": "ENFORCED", "probe_id": "probe-" + name,
                              "checked_at": 100, "expires_at": 200}
                       for name in REQUIRED_CHECKS}}


class SecurityTests(unittest.TestCase):
    def evaluate(self, evidence=None, mode="formal", **kwargs):
        return SecurityPolicy(mode).evaluate(evidence, problem_digest=PROBLEM,
                                             clock=lambda: 150, **kwargs)

    def test_formal_refuses_current_legacy_environment_without_execution(self):
        with self.assertRaises(SecurityError) as caught:
            SecurityPolicy("formal").enforce(problem_digest=PROBLEM)
        self.assertEqual(caught.exception.code, "FORMAL_UNAVAILABLE")
        report = caught.exception.report
        self.assertFalse(report["allowed"])
        self.assertFalse(report["rankable"])
        self.assertIn("formal_backend_not_implemented", report["missing_requirements"])
        self.assertIn("operator_profile_has_untrusted_host_boundary", report["missing_requirements"])

    def test_development_is_explicitly_not_cheat_resistant(self):
        report = SecurityPolicy().enforce(problem_digest=PROBLEM)
        self.assertTrue(report["allowed"])
        self.assertEqual(report["status"], "DEVELOPMENT_ONLY")
        self.assertEqual(report["security_label"], "not_cheat_resistant")
        self.assertFalse(report["rankable"])
        self.assertFalse(report["source_scanning_is_security_boundary"])

    def test_all_true_client_booleans_cannot_count_as_boundary_evidence(self):
        evidence = complete_declarations()
        evidence["checks"] = dict.fromkeys(REQUIRED_CHECKS, True)
        report = self.evaluate(evidence, operator_profile="device_only_broker_v1",
                               backend_profile="future_broker")
        self.assertFalse(report["allowed"])
        self.assertEqual(report["validated_admin_declarations"], [])
        self.assertTrue(set(REQUIRED_CHECKS).issubset(report["missing_requirements"]))

    def test_even_complete_admin_declarations_do_not_install_a_backend(self):
        report = self.evaluate(complete_declarations(), operator_profile="device_only_broker_v1",
                               backend_profile="future_broker")
        self.assertTrue(report["evidence_schema_valid"])
        self.assertFalse(report["evidence_is_runtime_attestation"])
        self.assertEqual(report["missing_requirements"], ["formal_backend_not_implemented"])
        self.assertEqual(report["status"], "FORMAL_UNAVAILABLE")

    def test_separate_translation_unit_does_not_prove_host_boundary(self):
        for profile in ("legacy_host_untrusted", "separate_harness_untrusted", "cpu_example_untrusted"):
            report = self.evaluate(complete_declarations(profile), operator_profile=profile,
                                   backend_profile="future_broker")
            self.assertIn("operator_profile_has_untrusted_host_boundary", report["missing_requirements"])

    def test_evidence_from_other_problem_profile_policy_or_deployment_cannot_match(self):
        mutations = {"problem_digest": "b" * 64, "operator_profile": "wrong",
                     "backend_profile": "wrong", "policy_version": "wrong",
                     "deployment_id": "", "schema_version": True}
        for field, value in mutations.items():
            evidence = complete_declarations()
            evidence[field] = value
            report = self.evaluate(evidence, operator_profile="device_only_broker_v1",
                                   backend_profile="future_broker")
            self.assertFalse(report["evidence_schema_valid"], field)
            self.assertGreater(len(report["missing_requirements"]), 1, field)

    def test_detected_unresolved_expired_future_and_malformed_probes_are_not_enforced(self):
        requirement = "candidate_run_isolated"
        variants = ({"status": "DETECTED"}, {"status": "UNRESOLVED"},
                    {"expires_at": 150}, {"checked_at": 151}, {"checked_at": True},
                    {"expires_at": float("inf")}, {"probe_id": ""})
        for patch in variants:
            evidence = complete_declarations()
            evidence["checks"][requirement].update(patch)
            report = self.evaluate(evidence, operator_profile="device_only_broker_v1",
                                   backend_profile="future_broker")
            self.assertIn(requirement, report["missing_requirements"], patch)
            self.assertNotIn(requirement, report["validated_admin_declarations"], patch)

    def test_candidate_safe_report_does_not_echo_private_probe_data(self):
        evidence = complete_declarations()
        evidence["private_secret"] = "DO-NOT-PUBLISH"
        evidence["checks"]["private_oracle_isolated"]["probe_id"] = "/private/DO-NOT-PUBLISH"
        original = deepcopy(evidence)
        report = self.evaluate(evidence, operator_profile="device_only_broker_v1",
                               backend_profile="future_broker")
        self.assertNotIn("DO-NOT-PUBLISH", json.dumps(report))
        self.assertEqual(evidence, original)

    def test_missing_or_malformed_evidence_does_not_raise_or_allow_formal(self):
        for evidence in (None, True, [], {}, {"checks": True}, {"checks": {"agent_identity_isolated": []}}):
            report = self.evaluate(evidence)
            self.assertFalse(report["allowed"])
            self.assertFalse(report["evidence_schema_valid"])

    def test_invalid_mode_or_clock_fails_without_silent_default(self):
        for mode in ("FORMAL", "prod", "", None):
            with self.assertRaises(ValueError):
                SecurityPolicy(mode)
        with self.assertRaises(ValueError):
            SecurityPolicy().evaluate(clock=lambda: float("nan"))


if __name__ == "__main__":
    unittest.main()
