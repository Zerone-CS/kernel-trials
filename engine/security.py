"""Fail-closed formal-evaluation policy, not an OS sandbox implementation.

Only trusted service/admin code may provide deployment evidence. Never forward
candidate request fields into ``admin_evidence``, ``operator_profile`` or
``backend_profile``. A JSON claim cannot authenticate its own provenance.

This release intentionally registers NO formal execution backends. Even perfect
looking administrator declarations cannot promote a development worker into a
verified sandbox. Future backends must implement/probe the boundaries below,
then be explicitly registered in trusted code and reviewed with adversarial
fixtures. Separate C++ translation units alone are not a trusted Host boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from typing import Any, Callable, Dict, Mapping, Optional, Tuple


POLICY_VERSION = "formal-boundaries-v1"
REQUIRED_CHECKS: Tuple[str, ...] = (
    "agent_identity_isolated",
    "authoritative_service_state",
    "candidate_build_isolated",
    "candidate_run_isolated",
    "private_oracle_isolated",
    "trusted_correctness_boundary",
    "trusted_timing_boundary",
    "host_device_boundary_enforced",
    "device_exclusive_access",
    "resource_limits_enforced",
    "fresh_inputs_verified",
)

# Empty on purpose. An operator manifest/config may not add entries here.
# Registration must accompany a real trusted backend, not an "enabled" flag.
_FORMAL_BACKENDS: Mapping[str, Tuple[str, ...]] = {}
_UNTRUSTED_OPERATOR_PROFILES = frozenset((
    "legacy_host_untrusted", "cpu_example_untrusted",
    "separate_harness_untrusted", "unspecified",
))
_SHA = re.compile(r"[a-f0-9]{64}\Z")


class SecurityError(ValueError):
    """Stable service denial; ``report`` is candidate-safe diagnostic metadata."""

    code = "FORMAL_UNAVAILABLE"

    def __init__(self, report: Mapping[str, Any]):
        super().__init__("Formal evaluation unavailable: " + ", ".join(report["missing_requirements"]))
        self.report = dict(report)


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def validate_evidence(admin_evidence: Optional[Mapping[str, Any]], *,
                      problem_digest: Optional[str], operator_profile: str,
                      backend_profile: str, now: float) -> Dict[str, Any]:
    """Validate evidence SHAPE/binding; caller must establish admin provenance.

    Accepted evidence is scoped to an immutable problem and deployment profile,
    with explicit per-boundary probe IDs, timestamps, expiry and ENFORCED status.
    Neither a client's boolean nor a source scanner result qualifies. This
    routine does not run probes and does not attest that their claims are true.
    Raw evidence/probe paths are never copied into the public result.
    """
    missing = []
    verified = []
    if not isinstance(admin_evidence, Mapping):
        return {"evidence_valid": False, "validated_declarations": [],
                "missing_requirements": ["administrator_evidence_missing", *REQUIRED_CHECKS]}
    evidence = admin_evidence
    if type(evidence.get("schema_version")) is not int or evidence.get("schema_version") != 1:
        missing.append("administrator_evidence_schema")
    if evidence.get("policy_version") != POLICY_VERSION:
        missing.append("evidence_policy_binding")
    if (not isinstance(problem_digest, str) or not _SHA.fullmatch(problem_digest)
            or evidence.get("problem_digest") != problem_digest):
        missing.append("evidence_problem_binding")
    if evidence.get("operator_profile") != operator_profile:
        missing.append("evidence_operator_binding")
    if evidence.get("backend_profile") != backend_profile:
        missing.append("evidence_backend_binding")
    if not isinstance(evidence.get("deployment_id"), str) or not evidence["deployment_id"].strip():
        missing.append("evidence_deployment_identity")
    checks = evidence.get("checks")
    if not isinstance(checks, Mapping):
        checks = {}
    for requirement in REQUIRED_CHECKS:
        probe = checks.get(requirement)
        accepted = isinstance(probe, Mapping)
        if accepted:
            observed = probe.get("checked_at")
            expires = probe.get("expires_at")
            accepted = (
                probe.get("status") == "ENFORCED"
                and isinstance(probe.get("probe_id"), str)
                and bool(probe["probe_id"].strip())
                and _number(observed) and _number(expires)
                and observed <= now < expires
                and observed < expires
            )
        if accepted:
            verified.append(requirement)
        else:
            missing.append(requirement)
    return {"evidence_valid": not missing,
            "validated_declarations": verified,
            "missing_requirements": missing}


@dataclass(frozen=True)
class SecurityPolicy:
    mode: str = "development"

    def __post_init__(self):
        if self.mode not in ("development", "formal"):
            raise ValueError("mode must be development or formal")

    def evaluate(self, admin_evidence: Optional[Mapping[str, Any]] = None, *,
                 operator_profile: str = "legacy_host_untrusted",
                 problem_digest: Optional[str] = None,
                 backend_profile: str = "development_subprocess",
                 clock: Callable[[], float] = time.time) -> Dict[str, Any]:
        """Return a public guard report; no processes, probes or writes occur."""
        now = clock()
        if not _number(now):
            raise ValueError("security evidence clock must return finite seconds")
        if not isinstance(operator_profile, str) or not isinstance(backend_profile, str):
            raise ValueError("Profiles must be trusted server-side strings")
        evidence = validate_evidence(admin_evidence, problem_digest=problem_digest,
                                     operator_profile=operator_profile,
                                     backend_profile=backend_profile, now=now)
        missing = list(evidence["missing_requirements"])
        allowed_profiles = _FORMAL_BACKENDS.get(backend_profile)
        if allowed_profiles is None:
            missing.append("formal_backend_not_implemented")
        if operator_profile in _UNTRUSTED_OPERATOR_PROFILES:
            missing.append("operator_profile_has_untrusted_host_boundary")
        elif allowed_profiles is not None and operator_profile not in allowed_profiles:
            missing.append("operator_profile_not_supported_by_backend")
        formal = not missing
        allowed = self.mode == "development" or formal
        return {
            "policy_version": POLICY_VERSION,
            "mode": self.mode,
            "status": ("FORMAL_READY" if formal and self.mode == "formal" else
                       "DEVELOPMENT_ONLY" if self.mode == "development" else
                       "FORMAL_UNAVAILABLE"),
            "allowed": allowed,
            "formal_available": formal,
            "rankable": formal and self.mode == "formal",
            "security_label": ("verified_formal_boundaries" if formal and self.mode == "formal"
                               else "not_cheat_resistant"),
            "not_cheat_resistant": not (formal and self.mode == "formal"),
            "operator_profile": operator_profile,
            "backend_profile": backend_profile,
            "missing_requirements": list(dict.fromkeys(missing)),
            "evidence_schema_valid": evidence["evidence_valid"],
            "validated_admin_declarations": evidence["validated_declarations"],
            "evidence_is_runtime_attestation": False,
            "source_scanning_is_security_boundary": False,
        }

    def enforce(self, admin_evidence: Optional[Mapping[str, Any]] = None,
                **kwargs: Any) -> Dict[str, Any]:
        """Guard BEFORE candidate execution; formal failure must not downgrade."""
        report = self.evaluate(admin_evidence, **kwargs)
        if not report["allowed"]:
            raise SecurityError(report)
        return report
