"""Candidate-safe final artifacts built from authoritative judge records.

Formatting never supplies a missing score, promotes a development result to a
formal result, or uses public feedback as a substitute for the unique final.
"""
import math


_FAILURES = {"compile_failed", "runtime_failed", "correctness_failed", "source_rejected"}
_INVALID = {"infrastructure_error", "measurement_invalid", "formal_unavailable"}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _score(final):
    outcome = final.get("outcome")
    if outcome in _FAILURES:
        return 0
    value = final.get("score")
    if outcome == "passed" and _number(value) and 0 <= value <= 100:
        return value
    return None


def _timings(final):
    performance = final.get("performance") or {}
    scoring = final.get("scoring") or {}
    rows = scoring.get("cases", performance.get("cases", final.get("measurements", [])))
    if not isinstance(rows, list):
        return []
    public = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate = row.get("median_us", row.get("candidate_us"))
        reference = row.get("reference_us")
        speedup = row.get("speedup")
        if not _number(speedup) and _number(candidate) and candidate > 0 and _number(reference) and reference > 0:
            speedup = reference / candidate
        public.append({
            "case_id": row.get("case_id"),
            "candidate_us": candidate if _number(candidate) and candidate > 0 else None,
            "reference_us": reference if _number(reference) and reference > 0 else None,
            "speedup": speedup if _number(speedup) and speedup > 0 else None,
            "case_score": row.get("case_score") if _number(row.get("case_score")) else None,
            "measurement_valid": row.get("measurement_valid") is True,
        })
    return public


def build_report(session, operations, diagnostics):
    """Build an artifact using one authenticated session and ordered history."""
    session_id = session["session_id"]
    if diagnostics.get("session_id") != session_id or any(op["session_id"] != session_id for op in operations):
        raise ValueError("Report evidence mixes different sessions")
    finals = [op for op in operations if op.get("action") == "final" and op.get("state") == "FINISHED"]
    if len(finals) > 1:
        raise ValueError("Multiple final judgments in one session")
    operation = finals[0] if finals else None
    if operation and session.get("final_operation_id") not in (None, operation["operation_id"]):
        raise ValueError("Final operation differs from authoritative session")
    final = operation.get("report") or {} if operation else {}
    if not isinstance(final, dict):
        raise ValueError("Invalid final report")
    if operation and final.get("source_sha256") not in (None, operation["source_sha256"]):
        raise ValueError("Final report source differs from admitted source")
    security = final.get("security") or {}
    metadata = session.get("claimed_agent_metadata") or {}
    purpose = metadata.get("evaluation_purpose", "unspecified")
    launcher_id = metadata.get("launcher_id")
    launcher_id = launcher_id if isinstance(launcher_id, str) and launcher_id else None
    attempt_index = metadata.get("attempt_index")
    attempt_index = attempt_index if type(attempt_index) is int and attempt_index > 0 else None
    outcome = final.get("outcome", "no_final_result")
    score = _score(final)
    mode = security.get("mode", "unspecified")
    rankable = bool(security.get("rankable") is True and score is not None and outcome == "passed"
                    and purpose == "agent_evaluation" and not diagnostics.get("contains_infrastructure_failure"))
    started = session.get("started_at")
    finished = operation.get("completed_at") if operation else None
    elapsed = max(0, finished - started) if _number(started) and _number(finished) else None
    baseline = final.get("baseline_identity") or {}
    result = {
        "outcome": outcome, "score": score,
        "score_status": final.get("score_status", "unavailable"),
        "source_sha256": operation["source_sha256"] if operation else None,
        "final_operation_id": operation["operation_id"] if operation else None,
        "stages": final.get("stages", {key: "not_run" for key in ("build", "runtime", "correctness", "performance")}),
        "correctness": {key: final.get("correctness", {}).get(key)
                        for key in ("passed", "total", "executed", "scope")},
        "error_category": final.get("error_category", final.get("error", {}).get("category")),
        "mode": mode, "security_label": security.get("security_label", "not_verified"),
        "rankable": rankable,
        "metric": final.get("metric", (final.get("performance") or {}).get("metric")),
    }
    if purpose == "entry_smoke":
        label = "入口流程测试：不评价 Agent 实现或优化能力"
    elif mode == "development" or security.get("security_label") == "not_cheat_resistant":
        label = "开发评测：未部署防作弊隔离，不属于正式排行榜成绩"
    elif rankable:
        label = "正式评测"
    else:
        label = "评测记录：不具备正式排名条件"
    summary = label + "；结果 " + outcome + "；" + ("任务分 " + format(score, ".6g") + "/100" if score is not None else "暂无可比较的任务分")
    return {
        "schema_version": 1, "kind": "operator_evaluation_report", "summary": summary,
        "session": {"session_id": session_id, "state": session.get("state"),
                    "problem_digest": session.get("problem_digest"), "track": session.get("track"),
                    "launcher_id": launcher_id, "attempt_index": attempt_index,
                    "evaluation_purpose": purpose,
                    "purpose_source": "administrator_bound_session_metadata",
                    "agent_metadata_verified": session.get("agent_metadata_verified", False)},
        "budget": {"policy": session.get("policy"), "feedback_used": session.get("feedback_used"),
                   "feedback_remaining": session.get("feedback_remaining"),
                   "started_at": started, "stop_at": session.get("stop_at"),
                   "budget_stop_reason": session.get("budget_stop_reason"),
                   "completed_at": finished, "session_elapsed_seconds_including_final": elapsed},
        "result": result,
        "comparison": {"baseline_identity": baseline,
                       "reference_is_theoretical_optimum": False,
                       "timing_cases": _timings(final),
                       "formula": (final.get("scoring") or {}).get("formula"),
                       "environment_fingerprint": (final.get("environment") or {}).get("fingerprint")},
        "diagnostics": diagnostics,
    }


def _cell(value):
    if value is None:
        return "未提供"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").replace("`", "'")


def _numeric(value, suffix=""):
    return format(value, ".6g") + suffix if _number(value) else "未测量"


def render_markdown(report):
    """Render a standalone human-readable artifact; no candidate raw log copy."""
    session = report["session"]
    result = report["result"]
    budget = report["budget"]
    comparison = report["comparison"]
    diagnosis = report["diagnostics"]
    lines = ["# 算子评测报告", "", report["summary"], "",
             "| 项目 | 结果 |", "| --- | --- |",
             "| 会话 | " + _cell(session["session_id"]) + " |",
             "| Launcher 标识 | " + _cell(session.get("launcher_id")) + " |",
             "| 实验轮次 | " + _cell(session.get("attempt_index")) + " |",
             "| 轨道 | " + _cell(session["track"]) + " |",
             "| 用途 | " + _cell(session["evaluation_purpose"]) + " |",
             "| 终评状态 | " + _cell(result["outcome"]) + " |",
             "| 任务分 | " + (_numeric(result["score"], "/100") if result["score"] is not None else "无有效分数") + " |",
             "| 正确性 | " + _cell(result["correctness"].get("passed")) + " / " + _cell(result["correctness"].get("total")) + "；" + _cell(result["correctness"].get("scope")) + " |",
             "| 反馈使用 | " + _cell(budget["feedback_used"]) + " / " + _cell((budget.get("policy") or {}).get("max_feedback")) + " |",
             "| 任务时间上限 | " + _cell((budget.get("policy") or {}).get("active_seconds")) + " 秒 |",
             "| 会话耗时（含终评） | " + _numeric(budget["session_elapsed_seconds_including_final"], " 秒") + " |",
             "| 停止原因 | " + _cell(budget["budget_stop_reason"] or "未耗尽预算或提前终评") + " |",
             "| 安全标识 | " + _cell(result["security_label"]) + " |",
             "| 可用于正式排名 | " + ("是" if result["rankable"] else "否") + " |", "",
             "源码 SHA256：`" + _cell(result["source_sha256"]) + "`  ",
             "题目摘要：`" + _cell(session["problem_digest"]) + "`", ""]
    if result.get("error_category"):
        lines += ["失败类别：" + _cell(result["error_category"]), ""]
    lines += ["## 性能与参考基线", "", "计时口径：" + _cell(result["metric"]), "",
              "参考是同环境、同协议标定的已知实现，不是理论最优。比参考更快的收益通过加速比保留；是否计入总分取决于固定评分公式。", ""]
    baseline = comparison.get("baseline_identity") or {}
    for key in ("baseline_id", "source_sha256", "package_digest", "engine_digest", "environment_fingerprint"):
        if baseline.get(key) is not None:
            lines.append("- " + key + "：`" + _cell(baseline[key]) + "`")
    if baseline:
        lines.append("")
    if comparison.get("formula"):
        lines += ["评分公式：`" + _cell(comparison["formula"]) + "`", ""]
    timings = comparison.get("timing_cases") or []
    if timings:
        lines += ["| Shape / case | 候选 μs | 参考 μs | 加速比 | 单项分 | 测量有效 |",
                  "| --- | ---: | ---: | ---: | ---: | --- |"]
        for row in timings:
            lines.append("| " + " | ".join((_cell(row["case_id"]), _numeric(row["candidate_us"]),
                         _numeric(row["reference_us"]), _numeric(row["speedup"], "×"),
                         _numeric(row["case_score"]), "是" if row["measurement_valid"] else "否")) + " |")
    else:
        lines.append("没有可报告的逐 shape 性能结果；未测量不等于 0 耗时或 0 加速比。")
    lines += ["", "## 可核查能力记录", "", "这些过程记录不额外加分，也不把基线原有能力归功于 Agent。首次通过时间为收到该次完整反馈时的累计耗时。", "",
              "| 阶段 | 首次通过的提交序号 | 获得反馈耗时 | 测试范围 |",
              "| --- | ---: | ---: | --- |"]
    for stage in ("build", "runtime", "correctness", "performance"):
        milestone = (diagnosis.get("first_verified") or {}).get(stage)
        lines.append("| " + stage + " | " + (_cell(milestone["attempt"]) + " | " + _numeric(milestone.get("elapsed_seconds"), " 秒") + " | " + _cell(milestone.get("scope")) if milestone else "未达到 | 未达到 | 未执行或未通过") + " |")
    trace = diagnosis.get("trace") or []
    if trace:
        lines += ["", "| 提交 | 动作 | 源码 SHA（前 12 位） | 结果 | 错误类别 | 未修改起点 |",
                  "| ---: | --- | --- | --- | --- | --- |"]
        for item in trace:
            unchanged = item.get("is_unchanged_starter")
            lines.append("| " + " | ".join((_cell(item.get("attempt")), _cell(item.get("action")),
                         _cell((item.get("source_sha256") or "")[:12]), _cell(item.get("outcome")),
                         _cell(item.get("error_category")), "是" if unchanged is True else "否" if unchanged is False else "未知")) + " |")
    lines += ["", "编译/正确性失败与基础设施或测量无效分开记录。公开反馈不代替独立终评；缺失终评时不继承公开测试的分数。", ""]
    return "\n".join(lines)
