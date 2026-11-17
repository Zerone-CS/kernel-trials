"""Evidence-only capability profiles; these are not a second scoring rubric.

Accept only records read from the trusted judge/budget store. A candidate's
claims, compiler error count, code length, and model-written review never earn
points here. Public milestones do not substitute for independent final tests.
"""
from collections import Counter


STAGES = ("build", "runtime", "correctness", "performance")
INVALID = {"infrastructure_error", "formal_unavailable"}
FAILURES = {"compile_failed", "runtime_failed", "correctness_failed", "source_rejected"}


def capability_profile(session, operations, *, starter_sha256=None):
    # BudgetStore.history supplies acceptance order, including timestamp ties.
    ordered = list(operations)
    if any(op["session_id"] != session["session_id"] for op in ordered):
        raise ValueError("Cannot combine evidence from different sessions")
    started = session.get("started_at")
    first = {stage: None for stage in STAGES}
    outcomes = Counter()
    completed = []
    trace = []
    for index, operation in enumerate(ordered, 1):
        report = operation.get("report")
        if operation["state"] != "FINISHED" or not isinstance(report, dict):
            continue
        completed.append(operation)
        outcome = report.get("outcome", "unknown")
        outcomes[outcome] += 1
        stages = report.get("stages", {})
        verified = []
        if outcome not in INVALID:
            for stage in STAGES:
                if stages.get(stage) != "passed":
                    break  # Downstream claims cannot skip prerequisite stages.
                verified.append(stage)
                if first[stage] is None:
                    finished_at = operation.get("completed_at")
                    first[stage] = {
                        "attempt": index, "operation_id": operation["operation_id"],
                        "source_sha256": operation["source_sha256"],
                        "elapsed_seconds": (max(0, finished_at - started)
                                            if started is not None and finished_at is not None else None),
                        "scope": report.get("correctness", {}).get("scope") if stage == "correctness" else None,
                        "action": operation["action"],
                    }
        trace.append({
            "attempt": index, "operation_id": operation["operation_id"],
            "source_sha256": operation["source_sha256"], "action": operation["action"],
            "outcome": outcome, "highest_verified_stage": verified[-1] if verified else "none",
            "error_category": report.get("error_category", report.get("error", {}).get("category")),
            "correctness": {k: report.get("correctness", {}).get(k)
                            for k in ("passed", "total", "scope")},
            "is_unchanged_starter": (operation["source_sha256"] == starter_sha256
                                     if starter_sha256 else None),
        })

    finals = [op for op in completed if op["action"] == "final"]
    final = finals[-1] if finals else None
    if final is None:
        result = {"status": "no_final_result", "task_score": None}
    else:
        report = final["report"]
        outcome = report.get("outcome", "unknown")
        result = {"status": outcome, "task_score": None,
                  "source_sha256": final["source_sha256"],
                  "security_label": report.get("security", {}).get("security_label"),
                  "rankable": report.get("security", {}).get("rankable", False)}
        if outcome in FAILURES:
            result["task_score"] = 0
        elif outcome == "passed" and report.get("score") is not None:
            result["task_score"] = report["score"]
        # Invalid timing/infrastructure/unsupported scoring remains null, never 0.

    candidate_results = [op for op in completed if op["report"].get("outcome") not in INVALID]
    highest = next((stage for stage in reversed(STAGES) if first[stage]), "none")
    return {
        "schema_version": 1,
        "session_id": session["session_id"], "problem_digest": session["problem_digest"],
        "track": session["track"], "policy": session["policy"],
        "task_result": result, "highest_verified_stage": highest,
        "first_verified": first,
        "feedback_used": session["feedback_used"],
        "submitted_versions": len({op["source_sha256"] for op in ordered}),
        "completed_operations": len(completed),
        "outcomes": dict(sorted(outcomes.items())),
        "all_observed_attempts_failed_compilation": bool(candidate_results) and all(
            op["report"].get("outcome") == "compile_failed" for op in candidate_results),
        "contains_infrastructure_failure": any(outcomes[k] for k in INVALID),
        "trace": trace,
        "interpretation": {
            "process_metrics_are_scores": False,
            "public_correctness_is_final_correctness": False,
            "error_count_is_progress": False,
            "starter_success_is_agent_improvement": False,
            "unobserved_work_is_evaluated": False,
            "source_review": "optional_qualitative_separate_from_verified_results",
        },
    }


def suggest_final_source(operations):
    """Deterministic fallback, not a claim to choose the fastest implementation.

    Prefer latest wholly public-correct submission, then latest compiled, then
    latest submitted. No comparison of incomparable timing/subset results.
    The selected bytes must still be retrieved from immutable judge snapshots
    and independently re-evaluated. Caller may supply an explicit historical SHA.
    """
    candidates = [op for op in reversed(list(operations)) if op["action"] in ("check", "bench")]
    eligible = [op for op in candidates if op["state"] == "FINISHED"
                and op.get("report", {}).get("outcome") not in INVALID]
    for op in eligible:
        report = op.get("report") or {}
        correct = report.get("correctness", {})
        if (report.get("stages", {}).get("correctness") == "passed"
                and correct.get("scope") == "public_full"
                and isinstance(correct.get("total"), int) and correct["total"] > 0
                and correct.get("passed") == correct["total"]):
            return {"source_sha256": op["source_sha256"], "reason": "latest_public_correct"}
    for op in eligible:
        if op.get("report", {}).get("stages", {}).get("build") == "passed":
            return {"source_sha256": op["source_sha256"], "reason": "latest_compiled"}
    if candidates:
        return {"source_sha256": candidates[0]["source_sha256"], "reason": "latest_submitted"}
    return None
