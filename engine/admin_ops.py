"""Trusted baseline preparation, sharing device leases with candidate sessions."""
import hashlib
from .budget import Policy
from .controller import _json_bytes, _write_new
from .identity import measurement_engine_digest, protocol_sha256, npu_resource_id
from .problems import load_problem


def calibrate(controller, package_path):
    from .calibration import create_baseline
    package = load_problem(package_path)
    resource = npu_resource_id(package)
    if resource is None:
        raise ValueError("Calibration requires an NPU performance problem")
    identity_before = measurement_engine_digest()
    source = package.path("reference").read_bytes()
    source_sha = hashlib.sha256(source).hexdigest()
    credentials = controller.store.admin_create_session(
        problem_digest=package.digest, track="optimize", owner_id="reference-calibration",
        resource_id=resource, policy=Policy(max_feedback=1, active_seconds=7200),
        claimed_agent_metadata={"evaluation_purpose": "reference_calibration"})
    sid, token = credentials["session_id"], credentials["token"]
    controller.store.start_session(sid, token)
    operation = controller.store.reserve(sid, token, request_id="reference-calibration",
                                         action="final", source_sha256=source_sha)
    try:
        report = controller.runner(package, source, "final", controller.root / "calibrations" / operation["operation_id"],
                                   mode=controller.mode, seed=0, baseline=None)
    except Exception as error:
        report = {"outcome": "infrastructure_error", "error_category": "calibration_worker_exception",
                  "score": None, "stages": {k: "not_run" for k in ("build", "runtime", "correctness", "performance")}}
        controller.store.complete_operation(operation["operation_id"], report)
        raise RuntimeError("Calibration worker failed; lease released") from error
    controller.store.complete_operation(operation["operation_id"], report)
    if report.get("outcome") != "passed" or report.get("stages", {}).get("performance") != "passed":
        raise RuntimeError("Calibration failed: " + str(report.get("outcome")) + " / "
                           + str(report.get("error_category", report.get("score_status")))
                           + "; inspect " + str(controller.root / "calibrations" / operation["operation_id"]))
    if measurement_engine_digest() != identity_before:
        raise ValueError("Measurement code changed during calibration; baseline not published")
    baseline = create_baseline(report, package_digest=package.digest, environment=report["environment"],
                               reference_sha256=source_sha, engine_digest=identity_before,
                               protocol_digest=protocol_sha256(package),
                               expected_case_ids=[spec["case_id"] for spec in package.load_adapter().cases("public", 0)])
    path = controller.root / "baselines" / (baseline["baseline_id"] + ".json")
    if not path.exists():
        _write_new(path, _json_bytes(baseline))
    pointer = controller.root / "active_baselines" / (package.digest + ".json")
    pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = pointer.with_suffix(".tmp")
    temporary.write_bytes(_json_bytes({"baseline_id": baseline["baseline_id"]}))
    temporary.replace(pointer)
    return {"baseline": str(path), "baseline_id": baseline["baseline_id"], "problem_digest": package.digest,
            "run": str(controller.root / "calibrations" / operation["operation_id"]),
            "correctness": report["correctness"], "performance_cases": len(report["measurements"]),
            "reference_is_theoretical_optimum": False}
