"""Descriptive, paired development experiments over authoritative final reports.

Every planned task/condition/repeat remains visible. Unknown outcomes are never
zero scores; final candidate failures are. Capability labels overlap and are
not independent samples. Runner/model identities are claims, not attestation.
"""
import copy
import json
import math
import statistics

from .metrics import MSPROF_METRIC


_FAILURES = {"compile_failed", "runtime_failed", "correctness_failed", "source_rejected"}
_INFRA = {"infrastructure_error", "formal_unavailable"}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _stats(values, planned=None, geometric=False):
    values = [v for v in values if _number(v)]
    result = {"count": len(values), "mean": statistics.mean(values) if values else None,
              "std": statistics.stdev(values) if len(values) > 1 else None,
              "min": min(values) if values else None, "max": max(values) if values else None}
    if planned is not None:
        result["unavailable"] = planned - len(values)
    if geometric:
        result["geometric_mean"] = math.exp(statistics.mean(math.log(v) for v in values)) if values else None
    return result


def _object(value):
    return value if isinstance(value, dict) else {}


def _runner_claims(condition):
    runner = _object(condition.get("runner"))
    return {key: runner.get(key) if isinstance(runner.get(key), str) else None
            for key in ("configuration_digest", "provider", "model_claim")}


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _metadata(manifest, task):
    from .task_catalog import list_tasks
    catalog = next((item for item in list_tasks() if item["id"] == task["id"]), {})
    value = _object(manifest.get("task_metadata")).get(task["id"], {})
    return {**catalog, **task, **_object(value)}


def _tags(metadata):
    values = metadata.get("capabilities", metadata.get("capability_tags", metadata.get("tags", [])))
    if not isinstance(values, list):
        return []
    return sorted({v for v in values if isinstance(v, str) and v})


def _is_full(task_id, metadata):
    return (task_id in ("full", "full_operator") or metadata.get("is_full") is True
            or metadata.get("kind", metadata.get("scope")) in ("full", "full_operator"))


def _first_correct(diagnostics):
    milestone = _object(_object(diagnostics.get("first_verified")).get("correctness"))
    attempt = milestone.get("attempt")
    if type(attempt) is not int or attempt < 1:
        return None
    trace = diagnostics.get("trace") or []
    visible = [item for item in trace if isinstance(item, dict)
               and type(item.get("attempt")) is int and item["attempt"] <= attempt]
    # An operation ordinal includes the final; it is not a feedback ordinal.
    matched = (len(visible) == attempt and {item["attempt"] for item in visible} == set(range(1, attempt + 1))
               and any(item.get("attempt") == attempt and item.get("operation_id") == milestone.get("operation_id")
                       for item in visible))
    feedback = sum(item.get("action") in ("check", "bench") for item in visible) if matched else None
    elapsed = milestone.get("elapsed_seconds")
    return {"operation_attempt": attempt, "action": milestone.get("action"),
            "feedback_used": feedback, "elapsed_seconds": elapsed if _number(elapsed) and elapsed >= 0 else None,
            "scope": milestone.get("scope")}


def _trial(manifest, task, condition, repeat, supplied):
    metadata = _metadata(manifest, task)
    raw = _object(supplied)
    report = _object(raw.get("report"))
    if "report" in report and "result" not in report:
        report = _object(report.get("report"))  # Controller.report() envelope.
    session = _object(report.get("session"))
    result = _object(report.get("result"))
    comparison = _object(report.get("comparison"))
    baseline = _object(comparison.get("baseline_identity"))
    budget = _object(report.get("budget"))
    diagnostics = _object(report.get("diagnostics"))
    stages = _object(result.get("stages"))
    correctness = _object(result.get("correctness"))
    outcome = result.get("outcome", "no_final_result")
    has_final = bool(result.get("final_operation_id")) and outcome != "no_final_result"
    total = correctness.get("total")
    correct = None
    if has_final and outcome in _FAILURES:
        correct = False
    elif (has_final and outcome not in _INFRA
          and all(stages.get(k) == "passed" for k in ("build", "runtime", "correctness"))
          and type(total) is int and total > 0 and type(correctness.get("passed")) is int
          and correctness["passed"] == total and type(correctness.get("executed")) is int
          and correctness["executed"] == total
          and correctness.get("scope") in ("public_full", "public_and_hidden_full")):
        correct = True
    rows = comparison.get("timing_cases") or []
    timings = []
    seen = set()
    duplicate = False
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            duplicate = True
            continue
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            duplicate = True
            continue
        seen.add(case_id)
        latency = row.get("candidate_us")
        valid = row.get("measurement_valid") is True and _number(latency) and latency > 0
        timings.append({"case_id": case_id, "candidate_us": latency if valid else None,
                        "measurement_valid": valid})
    expected = metadata.get("case_ids", metadata.get("public_case_ids"))
    measurement_valid = bool(correct is True and outcome == "passed" and stages.get("performance") == "passed"
                             and result.get("metric") == MSPROF_METRIC
                             and timings and not duplicate and all(t["measurement_valid"] for t in timings)
                             and (expected is None or set(expected) == seen))
    score = 0 if has_final and outcome in _FAILURES else None
    if measurement_valid and _number(result.get("score")) and 0 <= result["score"] <= 100:
        score = result["score"]
    measured_environment = comparison.get("environment_fingerprint")
    bound_environment = (comparison.get("bound_environment_fingerprint") or baseline.get("environment_fingerprint")
                         or _object(baseline.get("environment")).get("fingerprint"))
    identity = {"problem_digest": session.get("problem_digest"), "track": session.get("track"),
                "policy": budget.get("policy"), "baseline_id": baseline.get("baseline_id"),
                "protocol_digest": comparison.get("protocol_digest") or baseline.get("protocol_digest"),
                "engine_digest": comparison.get("engine_digest") or baseline.get("engine_digest"),
                "environment_fingerprint": measured_environment or bound_environment,
                "environment_identity_source": "measured" if measured_environment else "baseline_binding" if bound_environment else None,
                "formula": comparison.get("formula")}
    issues = []
    if report:
        for field, expected_value in (("track", manifest.get("track")), ("policy", manifest.get("policy"))):
            if expected_value is not None and identity[field] != expected_value:
                issues.append(field + "_differs_from_manifest")
        for field in ("problem_digest", "protocol_digest", "baseline_id"):
            if metadata.get(field) is not None and identity[field] != metadata[field]:
                issues.append(field + "_differs_from_task_metadata")
        if baseline.get("package_digest") not in (None, identity["problem_digest"]):
            issues.append("baseline_package_differs_from_problem")
        claimed = _object(session.get("claimed_agent_metadata"))
        expected_claims = {"experiment_id": manifest.get("experiment_id"), "trial_id": raw.get("trial_id"),
                           "task_id": task["id"], "condition_id": condition["id"], "repeat_index": repeat,
                           "method_digest": condition.get("method_digest")}
        for key, value in expected_claims.items():
            if key in claimed and value is not None and claimed[key] != value:
                issues.append(key + "_differs_from_bound_session")
    if supplied is None:
        classification = "missing_trial"
    elif not has_final:
        classification = "no_final_result"
    elif outcome in _FAILURES:
        classification = "candidate_failure"
    elif outcome in _INFRA:
        classification = "infrastructure_invalid"
    elif not measurement_valid:
        classification = "measurement_invalid" if correct is True or outcome == "measurement_invalid" else "unverified_result"
    else:
        classification = "passed"
    used = budget.get("feedback_used")
    return {"trial_id": raw.get("trial_id"), "task_id": task["id"], "condition_id": condition["id"],
            "repeat_index": repeat, "status": raw.get("status", "MISSING"), "classification": classification,
            "outcome": outcome, "final_correct": correct, "measurement_valid": measurement_valid,
            "task_score": score, "timing_cases": timings, "identity": identity, "identity_issues": issues,
            "first_correct": _first_correct(diagnostics),
            "feedback_used": used if type(used) is int and used >= 0 else None,
            "session_id": session.get("session_id"), "launcher_attempt_index": session.get("attempt_index"),
            "metric": result.get("metric"),
            "report_path": raw.get("report_path"), "runner_metadata": copy.deepcopy(_object(raw.get("runner_metadata"))),
            "execution_error": copy.deepcopy(raw.get("execution_error"))}


def _summary(rows):
    planned = len(rows)
    eligible = [row for row in rows if not row["identity_issues"]]
    correct = sum(row["final_correct"] is True for row in rows)
    failed = sum(row["final_correct"] is False for row in rows)
    latencies = {}
    for row in eligible:
        for timing in row["timing_cases"]:
            key = (row["task_id"], timing["case_id"])
            latencies.setdefault(key, [])
            if row["measurement_valid"]:
                latencies[key].append(timing["candidate_us"])
    task_counts = {task: sum(row["task_id"] == task for row in rows) for task in {row["task_id"] for row in rows}}
    return {"planned": planned, "final_correct": correct, "candidate_failed": failed,
            "correctness_unknown": planned - correct - failed,
            "correctness_success_rate": correct / planned if planned else None,
            "success_rate_denominator": "all_planned_trials_including_unknown",
            "missing_trials": sum(row["classification"] == "missing_trial" for row in rows),
            "no_final_result": sum(row["classification"] == "no_final_result" for row in rows),
            "infrastructure_invalid": sum(row["classification"] == "infrastructure_invalid" for row in rows),
            "measurement_invalid": sum(row["classification"] == "measurement_invalid" for row in rows),
            "identity_incomparable": sum(bool(row["identity_issues"]) for row in rows),
            "task_score": _stats([row["task_score"] for row in eligible], planned),
            "feedback_used": _stats([row["feedback_used"] for row in rows], planned),
            "first_correct_feedback": _stats([_object(row["first_correct"]).get("feedback_used") for row in rows], planned),
            "first_correct_seconds": _stats([_object(row["first_correct"]).get("elapsed_seconds") for row in rows], planned),
            "final_latency_us": [{"task_id": task, "case_id": case, **_stats(values, task_counts[task])}
                                 for (task, case), values in sorted(latencies.items())]}


def _pair(control, treatment):
    reasons = []
    reasons.extend("control:" + reason for reason in control["identity_issues"])
    reasons.extend("treatment:" + reason for reason in treatment["identity_issues"])
    left, right = control["identity"], treatment["identity"]
    for key in ("problem_digest", "track", "policy", "baseline_id"):
        if left[key] is None or right[key] is None:
            reasons.append(key + "_missing")
        elif left[key] != right[key]:
            reasons.append(key + "_mismatch")
    for key in ("protocol_digest", "engine_digest", "environment_fingerprint"):
        if left[key] != right[key]:
            reasons.append(key + "_mismatch")
    if control["measurement_valid"] and treatment["measurement_valid"] and left["formula"] != right["formula"]:
        reasons.append("formula_mismatch")
    # A matching immutable baseline binds protocol/engine even in older reports
    # that did not repeat those identity fields explicitly.
    comparable = not reasons
    score_delta = None
    if comparable and _number(control["task_score"]) and _number(treatment["task_score"]):
        score_delta = treatment["task_score"] - control["task_score"]
    cases = []
    valid = comparable and control["measurement_valid"] and treatment["measurement_valid"]
    if valid:
        a = {row["case_id"]: row["candidate_us"] for row in control["timing_cases"]}
        b = {row["case_id"]: row["candidate_us"] for row in treatment["timing_cases"]}
        if set(a) != set(b):
            reasons.append("timing_case_set_mismatch")
            comparable = valid = False
            score_delta = None
        else:
            cases = [{"case_id": key, "control_us": a[key], "treatment_us": b[key], "speedup": a[key] / b[key]}
                     for key in sorted(a)]
            if any(not _number(case["speedup"]) or case["speedup"] <= 0 for case in cases):
                reasons.append("nonfinite_speedup")
                cases = []
                valid = False
    if not valid:
        for name, row in (("control", control), ("treatment", treatment)):
            if not row["measurement_valid"]:
                reasons.append(name + ":" + row["classification"])
    return {"task_id": control["task_id"], "repeat_index": control["repeat_index"],
            "control_trial_id": control["trial_id"], "treatment_trial_id": treatment["trial_id"],
            "comparable": comparable, "reasons": reasons, "valid_performance_pair": bool(valid),
            "score_delta": score_delta, "case_speedups": cases,
            "geometric_mean_speedup": _stats([case["speedup"] for case in cases], geometric=True)["geometric_mean"]}


def _paired_summary(pairs):
    return {"planned_pairs": len(pairs), "valid_performance_pairs": sum(pair["valid_performance_pair"] for pair in pairs),
            "unavailable_performance_pairs": sum(not pair["valid_performance_pair"] for pair in pairs),
            "incomparable_pairs": sum(not pair["comparable"] for pair in pairs),
            "speedup": _stats([pair["geometric_mean_speedup"] for pair in pairs], len(pairs), geometric=True),
            "score_delta": _stats([pair["score_delta"] for pair in pairs], len(pairs))}


def build_comparison(manifest, trials):
    """Build a JSON-safe report; repeats are 1-based, never launcher ordinals.

    ``trials`` may be incomplete. Unknown/duplicate plan keys are errors rather
    than silently dropped evidence. Method digests differ by design across
    conditions; only each trial's own bound method identity is checked.
    """
    manifest = copy.deepcopy(manifest)
    _canonical(manifest)
    repeats = manifest.get("repeats")
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    tasks = [dict(id=task) if isinstance(task, str) else dict(task) for task in manifest.get("tasks", [])]
    conditions = manifest.get("conditions", [])
    for name, values in (("tasks", tasks), ("conditions", conditions)):
        ids = [value.get("id") for value in values]
        if not ids or any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError(name + " must contain unique nonempty ids")
    control = manifest.get("control", "baseline")
    if control not in {condition["id"] for condition in conditions}:
        raise ValueError("control must identify a condition")
    plan = {(task["id"], condition["id"], repeat)
            for task in tasks for condition in conditions for repeat in range(1, repeats + 1)}
    supplied = {}
    trial_ids = set()
    for trial in trials:
        repeat = trial.get("repeat_index")
        key = (trial.get("task_id"), trial.get("condition_id"), repeat)
        if type(repeat) is not int or key not in plan:
            raise ValueError("Trial is outside the 1-based experiment plan")
        if key in supplied:
            raise ValueError("Duplicate task/condition/repeat trial")
        trial_id = trial.get("trial_id")
        if not isinstance(trial_id, str) or not trial_id or trial_id in trial_ids:
            raise ValueError("Trial ids must be nonempty and unique")
        supplied[key] = trial
        trial_ids.add(trial_id)
    rows = [_trial(manifest, task, condition, repeat, supplied.get((task["id"], condition["id"], repeat)))
            for task in tasks for condition in conditions for repeat in range(1, repeats + 1)]
    session_ids = [row["session_id"] for row in rows if row["session_id"]]
    if len(session_ids) != len(set(session_ids)):
        raise ValueError("A judge session cannot be reused as independent experiment trials")
    lookup = {(row["task_id"], row["condition_id"], row["repeat_index"]): row for row in rows}
    task_summaries = []
    full_ids = set()
    groups = {}
    for task in tasks:
        metadata = _metadata(manifest, task)
        full = _is_full(task["id"], metadata)
        if full:
            full_ids.add(task["id"])
        else:
            for tag in _tags(metadata):
                groups.setdefault(tag, []).append(task["id"])
        task_summaries.append({"task_id": task["id"], "is_full": full, "capabilities": _tags(metadata),
                               "conditions": [{"condition_id": condition["id"], **_summary([
                                   row for row in rows if row["task_id"] == task["id"] and row["condition_id"] == condition["id"]])}
                                              for condition in conditions]})
    paired = []
    for condition in conditions:
        if condition["id"] == control:
            continue
        pairs = [_pair(lookup[(task["id"], control, repeat)], lookup[(task["id"], condition["id"], repeat)])
                 for task in tasks for repeat in range(1, repeats + 1)]
        paired.append({"condition_id": condition["id"], "control_id": control,
                       "by_task": [{"task_id": task["id"], **_paired_summary([pair for pair in pairs if pair["task_id"] == task["id"]])}
                                   for task in tasks],
                       "capabilities": [{"capability": tag, "task_ids": ids, **_paired_summary([pair for pair in pairs if pair["task_id"] in ids])}
                                        for tag, ids in sorted(groups.items())],
                       # The full operator is not another independent shape.
                       "non_full_summary": _paired_summary([pair for pair in pairs if pair["task_id"] not in full_ids]),
                       "full_summary": _paired_summary([pair for pair in pairs if pair["task_id"] in full_ids]),
                       "pairs": pairs})
    purpose = manifest.get("evaluation_purpose", "unspecified")
    workflow_only = purpose == "entry_smoke"
    label = "仅流程验收：不评价 Agent 写算子或方法优化能力" if workflow_only else "研发方法对照：不属于正式排名"
    hidden_input_policy = manifest.get("hidden_input_policy")
    return {"schema_version": 1, "kind": "operator_experiment_comparison", "mode": "development", "rankable": False,
            "evaluation_purpose": purpose, "workflow_only": workflow_only, "evaluation_label": label,
            "hidden_input_policy": hidden_input_policy if isinstance(hidden_input_policy, str) else None,
            "experiment_id": manifest.get("experiment_id"), "name": manifest.get("name"), "track": manifest.get("track"),
            "control": control, "repeats": repeats, "policy": manifest.get("policy"),
            "conditions": [{**{key: condition.get(key) for key in ("id", "label", "method_digest")},
                            "runner": _runner_claims(condition)} for condition in conditions],
            "trials": rows, "tasks": task_summaries,
            "capabilities": [{"capability": tag, "task_ids": ids, "overlapping_labels": True,
                              "conditions": [{"condition_id": condition["id"], **_summary([
                                  row for row in rows if row["task_id"] in ids and row["condition_id"] == condition["id"]])}
                                             for condition in conditions]} for tag, ids in sorted(groups.items())],
            "full_tasks": sorted(full_ids), "paired_comparisons": paired,
            "interpretation": {"success_denominator": "all_planned_trials; unknown is shown separately, not a proven failure",
                               "timing_unit": "microseconds, final per-case measurement only",
                               "speedup": "control_latency / treatment_latency; geometric mean over matching cases, then matching task/repeat pairs",
                               "score_delta": "treatment - control; valid candidate failures retain zero scores",
                               "std": "sample standard deviation; null for fewer than two observations",
                               "significance": "descriptive only; no significance or population-generalization claim",
                               "capabilities": "overlapping descriptive labels, not independent samples; full operator listed separately",
                               "security": "development results; runner/model claims are not authenticated; no isolated formal ranking"}}


def _cell(value):
    return str(value if value is not None else "未提供").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _value(value):
    return format(value, ".6g") if _number(value) else "无有效数据"


def _range(stats):
    return (_value(stats["mean"]) + " [" + _value(stats["min"]) + ", " + _value(stats["max"]) + "]"
            + "; std=" + _value(stats["std"]) + "; n=" + str(stats["count"]))


def render_comparison(report):
    """Render all planned outcomes and pair exclusions without private raw logs."""
    title = "# 流程验收报告（不评价方法能力）" if report.get("workflow_only") else "# 方法对照与重复实验报告"
    policy = _object(report.get("policy"))
    lines = [title, "", "**" + _cell(report.get("evaluation_label", "研发方法对照")) + "**", "", _cell(report.get("name")), "",
             "| 实验配置 | 值 |", "| --- | --- |",
             "| experiment_id | " + _cell(report.get("experiment_id")) + " |",
             "| 轨道 | " + _cell(report.get("track")) + " |",
             "| 每任务每条件重复次数 | " + _cell(report.get("repeats")) + " |",
             "| 每 trial 预算 | check/bench 合计 " + _cell(policy.get("max_feedback")) + " 次；任务 "
             + _cell(policy.get("active_seconds")) + " 秒；终评选择宽限 " + _cell(policy.get("final_grace_seconds")) + " 秒 |", "",
             "| 条件 | 标签 | method_digest | Runner 公开声明（未经身份认证） |",
             "| --- | --- | --- | --- |"]
    for condition in report["conditions"]:
        runner = _object(condition.get("runner"))
        claim = "; ".join(key + "=" + _cell(runner.get(key)) for key in ("provider", "model_claim", "configuration_digest"))
        lines.append("| " + " | ".join((_cell(condition["id"]), _cell(condition.get("label")),
                     _cell(condition.get("method_digest")), claim)) + " |")
    if report.get("hidden_input_policy"):
        lines += ["", "隐藏输入策略：" + _cell(report["hidden_input_policy"]) + "。公开性能输入仍由固定协议确定。"]
    lines += ["",
             "开发实验，未认证模型身份或部署正式防作弊隔离，不能用于正式排名。", "",
             "成功率分母包含全部计划实验；未知结果单列，不补成 0 分。性能仅配对同任务、同轮次的有效终评；统计为描述性均值、范围和样本标准差，不声称显著性。", "",
             "| 任务 | 条件 | 最终正确 / 计划 | 候选失败 | 未知 | 分数均值 [最小,最大]; std; n | 已用反馈 | 首次正确反馈 | 首次正确秒 |",
             "| --- | --- | ---: | ---: | ---: | --- | --- | --- | --- |"]
    for task in report["tasks"]:
        for condition in task["conditions"]:
            lines.append("| " + " | ".join((_cell(task["task_id"]) + ("（完整算子）" if task["is_full"] else ""),
                         _cell(condition["condition_id"]), str(condition["final_correct"]) + "/" + str(condition["planned"]),
                         str(condition["candidate_failed"]), str(condition["correctness_unknown"]), _range(condition["task_score"]),
                         _range(condition["feedback_used"]), _range(condition["first_correct_feedback"]), _range(condition["first_correct_seconds"]))) + " |")
    lines += ["", "## 能力标签", "", "标签可以重叠，同一实验不能因此当作多个独立样本；完整算子单列。", "",
              "| 标签 | 任务 | 条件 | 正确 / 计划 | 分数统计 |", "| --- | --- | --- | ---: | --- |"]
    for group in report["capabilities"]:
        for condition in group["conditions"]:
            lines.append("| " + " | ".join((_cell(group["capability"]), _cell(", ".join(group["task_ids"])),
                         _cell(condition["condition_id"]), str(condition["final_correct"]) + "/" + str(condition["planned"]), _range(condition["task_score"]))) + " |")
    lines += ["", "## 最终逐 case 延迟（μs）", "", "| 任务 | 条件 | Case | 均值 [最小,最大]; std; n | 未获得有效测量 |",
              "| --- | --- | --- | --- | ---: |"]
    for task in report["tasks"]:
        for condition in task["conditions"]:
            for case in condition["final_latency_us"]:
                lines.append("| " + " | ".join((_cell(task["task_id"]), _cell(condition["condition_id"]),
                             _cell(case["case_id"]), _range(case), str(case["unavailable"]))) + " |")
    lines += ["", "## 同轮次配对", "", "加速比 = 对照延迟 / 方法延迟。失败分数可参与分差，失败或无效计时不参与性能加速比。", "",
              "| 方法 | 任务 | 有效性能对 / 计划 | 不可比 | 配对加速比几何均值 | 分差均值 [最小,最大]; std; n |",
              "| --- | --- | ---: | ---: | ---: | --- |"]
    for treatment in report["paired_comparisons"]:
        for task in treatment["by_task"]:
            lines.append("| " + " | ".join((_cell(treatment["condition_id"]), _cell(task["task_id"]),
                         str(task["valid_performance_pairs"]) + "/" + str(task["planned_pairs"]), str(task["incomparable_pairs"]),
                         _value(task["speedup"]["geometric_mean"]), _range(task["score_delta"]))) + " |")
    lines.append("")
    for treatment in report["paired_comparisons"]:
        for pair in treatment["pairs"]:
            if pair["reasons"]:
                lines.append("- " + _cell(treatment["condition_id"]) + " / " + _cell(pair["task_id"]) + " / 第 "
                             + str(pair["repeat_index"]) + " 轮：" + _cell(", ".join(pair["reasons"])))
    lines += ["", "## 全部计划实验", "", "| 任务 | 条件 | 重复轮次 | Launcher轮次 | 状态 | 终评分类 | 分数 | 报告 |",
              "| --- | --- | ---: | ---: | --- | --- | ---: | --- |"]
    for row in report["trials"]:
        lines.append("| " + " | ".join((_cell(row["task_id"]), _cell(row["condition_id"]), str(row["repeat_index"]),
                     _cell(row["launcher_attempt_index"]), _cell(row["status"]), _cell(row["classification"]),
                     _value(row["task_score"]), _cell(row["report_path"]))) + " |")
    lines.append("")
    for row in report["trials"]:
        if row["identity_issues"]:
            lines.append("\n身份不匹配（" + _cell(row["trial_id"]) + "）：" + _cell(", ".join(row["identity_issues"])) + "\n")
    lines += ["", "首次正确指标来自诊断记录，可能属于公开测试；它不替代最终正确性。首次正确的提交序号与已用 check/bench 反馈数分别记录，终评不伪装成第 21 次反馈。", ""]
    return "\n".join(lines)
