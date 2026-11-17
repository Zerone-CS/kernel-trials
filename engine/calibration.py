"""Immutable, environment-bound reference measurements for development scoring.

Only a trusted administrator creates or installs a baseline. Hashes detect
accidental mutation and bind sessions; they do not authenticate a root-writable
judge or substitute for the future isolated formal service.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .metrics import (MSPROF_METRIC, MeasurementError, _sha, case_weights,
                      expected_cases, reference_values, score_cases, summarize_captures)


class BaselineError(ValueError):
    code = "BASELINE_INVALID"


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                          separators=(",", ":")).encode("utf8")
    except (TypeError, ValueError) as error:
        raise BaselineError("Baseline identity must be finite JSON data") from error


def baseline_digest(baseline: Mapping[str, Any]) -> str:
    if not isinstance(baseline, Mapping):
        raise BaselineError("Baseline must be an object")
    material = {key: value for key, value in baseline.items() if key != "baseline_id"}
    return hashlib.sha256(canonical_bytes(material)).hexdigest()


def _bindings(*, package_digest, environment, reference_sha256, engine_digest,
              protocol_digest, expected_case_ids, weights):
    try:
        ids = expected_cases(expected_case_ids)
        weight_values = case_weights(ids, weights)
        bindings = {
            "package_digest": _sha(package_digest, "package digest"),
            "source_sha256": _sha(reference_sha256, "reference source digest"),
            "engine_digest": _sha(engine_digest, "measurement engine digest"),
            "protocol_digest": _sha(protocol_digest, "measurement protocol digest"),
            "case_ids": ids, "weights": weight_values,
        }
    except MeasurementError as error:
        raise BaselineError(str(error)) from error
    if not isinstance(environment, Mapping) or not environment:
        raise BaselineError("Baseline requires a nonempty measured environment identity")
    # Normalize nested container copies and reject NaN before comparison.
    environment = json.loads(canonical_bytes(dict(environment)))
    bindings["environment"] = environment
    bindings["environment_digest"] = hashlib.sha256(canonical_bytes(environment)).hexdigest()
    return bindings


def _capture_evidence(measurements):
    """Recompute reference medians from the retained profiler capture samples."""
    plans = []
    for record in measurements:
        if not isinstance(record, Mapping):
            raise BaselineError("Calibration measurement must be an object")
        captures = record.get("captures")
        if not isinstance(captures, list) or not captures:
            raise BaselineError("Calibration must retain the actual profiling capture evidence")
        parsed = []
        for capture in captures:
            if (not isinstance(capture, Mapping) or capture.get("measurement_valid") is not True
                    or not isinstance(capture.get("correctness"), Mapping)
                    or capture["correctness"].get("passed") is not True
                    or not isinstance(capture.get("parsed"), Mapping)):
                raise BaselineError("A reference capture did not pass correctness and timing validation")
            parsed.append(capture["parsed"])
        try:
            aggregate = summarize_captures(parsed, expected_captures=len(captures))
        except MeasurementError as error:
            raise BaselineError("Reference capture evidence is invalid: " + str(error)) from error
        if (record.get("median_us") != aggregate["median_us"]
                or record.get("capture_medians_us") != aggregate["capture_medians_us"]):
            raise BaselineError("Reference time vector does not match retained capture samples")
        plans.append({key: aggregate[key] for key in
                      ("capture_count", "warmups", "measured_calls", "expected_invocations", "aggregation")})
    if not plans or any(plan != plans[0] for plan in plans):
        raise BaselineError("Reference shapes used inconsistent profiling sampling plans")
    return plans[0]


def create_baseline(report: Mapping[str, Any], *, package_digest: str,
                    environment: Mapping[str, Any], reference_sha256: str,
                    engine_digest: str, protocol_digest: str,
                    expected_case_ids: Sequence[Any], weights: Optional[Mapping[Any, Any]] = None,
                    created_at: Optional[str] = None) -> dict[str, Any]:
    """Freeze a fresh full-suite reference run, never import v1 timing vectors.

    The calibration run uses action=final with baseline=None. It must execute
    the independent full correctness suite and collect complete performance
    samples; a missing score is expected until this reference is frozen.
    """
    bindings = _bindings(package_digest=package_digest, environment=environment,
                         reference_sha256=reference_sha256, engine_digest=engine_digest,
                         protocol_digest=protocol_digest, expected_case_ids=expected_case_ids,
                         weights=weights)
    if not isinstance(report, Mapping) or report.get("outcome") != "passed" or report.get("action") != "final":
        raise BaselineError("Calibration requires a passed independent final evaluation")
    if report.get("source_sha256") != reference_sha256 or report.get("problem_digest") != package_digest:
        raise BaselineError("Calibration report source or package identity differs")
    if not isinstance(report.get("stages"), Mapping) or any(report["stages"].get(stage) != "passed"
           for stage in ("build", "runtime", "correctness", "performance")):
        raise BaselineError("Calibration did not pass every execution/measurement stage")
    correctness = report.get("correctness", {})
    if (not isinstance(correctness, Mapping) or correctness.get("scope") != "public_and_hidden_full"
            or type(correctness.get("total")) is not int or correctness["total"] < 1
            or type(correctness.get("passed")) is not int
            or correctness["passed"] != correctness["total"]):
        raise BaselineError("Calibration requires the complete correctness suite")
    if "environment" in report and canonical_bytes(report["environment"]) != canonical_bytes(environment):
        raise BaselineError("Calibration report environment differs from bound environment")
    measurements = report.get("measurements")
    if not isinstance(measurements, list):
        raise BaselineError("Calibration report lacks case measurements")
    sampling_plan = _capture_evidence(measurements)
    try:
        values = reference_values({record["case_id"]: record["median_us"] for record in measurements}, bindings["case_ids"])
        # score_cases rejects duplicate/missing cases and untrusted metrics even
        # if their aggregate median happens to look plausible.
        check = score_cases(measurements, {"source_sha256": reference_sha256, "case_medians_us": values,
                                         "metric": MSPROF_METRIC},
                            source_sha256=reference_sha256, expected_case_ids=bindings["case_ids"],
                            weights=bindings["weights"])
    except (MeasurementError, KeyError, TypeError) as error:
        raise BaselineError("Calibration measurements are invalid: " + str(error)) from error
    if not check["all_correctness_passed"] or not check["all_measurements_valid"]:
        raise BaselineError("Calibration case correctness/measurement gate failed")
    stamp = created_at if created_at is not None else datetime.now(timezone.utc).isoformat()
    if not isinstance(stamp, str) or not stamp:
        raise BaselineError("created_at must be nonempty timestamp text")
    baseline = {
        "schema_version": 2, "kind": "op_eval_v2_reference_baseline", **bindings,
        "created_at": stamp, "metric": MSPROF_METRIC, "unit": "us",
        "reference_is_theoretical_optimum": False,
        "security_label": "not_cheat_resistant", "rankable": False,
        "is_official_platform_score": False,
        "case_medians_us": values,
        "sampling_plan": sampling_plan,
        "correctness": json.loads(canonical_bytes(correctness)),
        "measurements": json.loads(canonical_bytes(measurements)),
        "reference_binary_sha256": report.get("binary_sha256"),
    }
    baseline["baseline_id"] = baseline_digest(baseline)
    return baseline


def validate_baseline(baseline: Mapping[str, Any], *, package_digest: str,
                      environment: Mapping[str, Any], reference_sha256: str,
                      engine_digest: str, protocol_digest: str,
                      expected_case_ids: Sequence[Any], weights: Optional[Mapping[Any, Any]] = None) -> dict[str, Any]:
    """Reject any changed source/package/environment/engine/protocol or vector."""
    expected = _bindings(package_digest=package_digest, environment=environment,
                         reference_sha256=reference_sha256, engine_digest=engine_digest,
                         protocol_digest=protocol_digest, expected_case_ids=expected_case_ids,
                         weights=weights)
    if not isinstance(baseline, Mapping):
        raise BaselineError("Baseline must be an object")
    if baseline.get("schema_version") != 2 or baseline.get("kind") != "op_eval_v2_reference_baseline":
        raise BaselineError("Only a fresh v2 calibration is accepted; legacy times cannot be imported")
    if baseline.get("baseline_id") != baseline_digest(baseline):
        raise BaselineError("Baseline content hash does not match its immutable identity")
    for key, value in expected.items():
        if canonical_bytes(baseline.get(key)) != canonical_bytes(value):
            raise BaselineError("Baseline binding differs: " + key)
    if (baseline.get("metric") != MSPROF_METRIC or baseline.get("unit") != "us"
            or baseline.get("reference_is_theoretical_optimum") is not False):
        raise BaselineError("Baseline metric or reference semantics are invalid")
    try:
        values = reference_values(baseline, expected["case_ids"])
        report = {"outcome": "passed", "action": "final", "source_sha256": reference_sha256,
                  "problem_digest": package_digest,
                  "stages": {name: "passed" for name in ("build", "runtime", "correctness", "performance")},
                  "correctness": baseline.get("correctness"), "measurements": baseline.get("measurements")}
        rebuilt = create_baseline(report, package_digest=package_digest, environment=environment,
                                  reference_sha256=reference_sha256, engine_digest=engine_digest,
                                  protocol_digest=protocol_digest, expected_case_ids=expected_case_ids,
                                  weights=weights, created_at=baseline.get("created_at"))
        if (canonical_bytes(values) != canonical_bytes(rebuilt["case_medians_us"])
                or canonical_bytes(baseline.get("sampling_plan")) != canonical_bytes(rebuilt["sampling_plan"])):
            raise BaselineError("Baseline time vector differs from its measurement evidence")
    except MeasurementError as error:
        raise BaselineError(str(error)) from error
    return json.loads(canonical_bytes(dict(baseline)))


def save_baseline(path: Path, baseline: Mapping[str, Any]) -> None:
    """Exclusive create; never replace the baseline for an already bound trial."""
    path = Path(path)
    if baseline.get("baseline_id") != baseline_digest(baseline):
        raise BaselineError("Cannot save a baseline with mismatched content hash")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(canonical_bytes(dict(baseline)) + b"\n")


def load_baseline(path: Path, **bindings) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise BaselineError("Baseline must be a regular non-symlink file")
    try:
        baseline = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise BaselineError("Cannot read baseline JSON") from error
    return validate_baseline(baseline, **bindings)
