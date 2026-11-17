"""Fail-closed device measurements and an explicitly local reference score.

No measurements are supplied by this module.  A profiling capture must contain
one isolated case.  Counting one device task per declared call is an assumption
unless device-clock invocation windows establish the individual associations.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import statistics
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


MSPROF_METRIC = "msprof_device_task_duration"
ACL_EVENT_METRIC = "acl_event_elapsed"
COMPUTE_TYPES = frozenset({
    "AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV", "AI_CPU",
    "AIC", "AIV", "AICORE", "MIX_AICORE",
})
# Unknown types are errors, rather than silently discarded possible kernels.
NON_COMPUTE_TYPES = frozenset({
    "MEMCPY", "MEMCPY_ASYNC", "MEMCPY_D2D", "MEMCPY_H2D", "MEMCPY_D2H",
    "MEMSET", "EVENT_RECORD", "EVENT_WAIT", "NOTIFY_RECORD", "NOTIFY_WAIT",
    "WRITE_VALUE", "WAIT_VALUE", "RUNTIME", "HOST_CPU", "ACL_API",
})
_UNITS = {"us": Decimal(1), "µs": Decimal(1), "μs": Decimal(1),
          "ns": Decimal("0.001"), "ms": Decimal(1000), "s": Decimal(1000000)}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MeasurementError(ValueError):
    """The capture cannot support the requested measurement, not a rule verdict."""

    status = "MEASUREMENT_UNSUPPORTED"


def _decimal(value: Any, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise MeasurementError(f"{label}: boolean is not a measurement")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MeasurementError(f"{label}: not a finite number: {value!r}") from exc
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise MeasurementError(f"{label}: expected {'positive' if positive else 'nonnegative'} finite number")
    return result


def _count(value: Any, label: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise MeasurementError(f"{label}: expected {'nonnegative' if allow_zero else 'positive'} integer")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise MeasurementError(f"{label}: expected lowercase SHA256")
    return value


def _float(value: Any, label: str) -> float:
    result = float(_decimal(value, label, positive=True))
    if not math.isfinite(result) or result <= 0:
        raise MeasurementError(f"{label}: outside positive finite float range")
    return result


def summarize_samples(samples_us: Sequence[float]) -> dict[str, Any]:
    """Retain every declared measured sample, including the slow ones."""
    values = [_float(v, "sample duration") for v in samples_us]
    if not values:
        raise MeasurementError("no measured samples")
    median = statistics.median(values)
    if not math.isfinite(median):
        raise MeasurementError("median outside finite float range")
    return {"samples_us": values, "sample_count": len(values),
            "median_us": median, "mean_us": statistics.mean(values),
            "min_us": min(values), "max_us": max(values),
            "mad_us": statistics.median(abs(x - median) for x in values),
            "statistic": "median_of_all_declared_nonwarmup_samples",
            "outliers_removed": 0}


def _time_column(headers: Sequence[str], stem: str) -> tuple[str, Decimal]:
    pattern = re.compile(re.escape(stem) + r"\s*\(([^)]+)\)", re.IGNORECASE)
    matches = []
    for header in headers:
        match = pattern.fullmatch(header)
        if match:
            unit = match.group(1).strip().lower()
            if unit not in _UNITS:
                raise MeasurementError(f"unsupported unit in {header!r}")
            matches.append((header, _UNITS[unit]))
    if len(matches) != 1:
        raise MeasurementError(f"need exactly one explicit {stem}(unit) column; got {matches}")
    return matches[0]


def _id(value: str, label: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"\d+", value):
        raise MeasurementError(f"{label}: expected unsigned integral identity, got {value!r}")
    return str(int(value))


def _parse_table(path: Path) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], str]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise MeasurementError(f"cannot read UTF-8 profiling CSV {path}: {exc}") from exc
    import io
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        headers = [x.strip() for x in next(reader)]
    except (StopIteration, csv.Error) as exc:
        raise MeasurementError(f"empty or invalid CSV: {path}") from exc
    if not all(headers) or len({h.casefold() for h in headers}) != len(headers):
        raise MeasurementError(f"empty or duplicate CSV header: {path}")
    if {"Task ID", "Stream ID", "Task Type", "Op Name", "Device_id"} <= set(headers):
        schema = "op_summary"
        device, stream, task, kind, name = "Device_id", "Stream ID", "Task ID", "Task Type", "Op Name"
        duration_key, duration_unit = _time_column(headers, "Task Duration")
        start_key, start_unit = _time_column(headers, "Task Start Time")
        stop = None
    elif {"task_id", "stream_id", "kernel_type", "kernel_name", "Device_id"} <= set(headers):
        schema = "task_time"
        device, stream, task, kind, name = "Device_id", "stream_id", "task_id", "kernel_type", "kernel_name"
        duration_key, duration_unit = _time_column(headers, "task_time")
        start_key, start_unit = _time_column(headers, "task_start")
        stop = _time_column(headers, "task_stop")
    else:
        raise MeasurementError(f"unsupported CSV schema (host/API/PMU tables are not task durations): {path}")
    rows, skipped = [], []
    try:
        for line, fields in enumerate(reader, 2):
            if not fields or not any(s.strip() for s in fields):
                continue
            if len(fields) != len(headers):
                raise MeasurementError(f"{path}:{line}: CSV field count differs from header")
            row = dict(zip(headers, (x.strip() for x in fields)))
            task_type = row[kind].upper().replace(" ", "_")
            if task_type in NON_COMPUTE_TYPES:
                skipped.append({"file": str(path), "line": line, "task_type": task_type})
                continue
            if task_type not in COMPUTE_TYPES:
                raise MeasurementError(f"{path}:{line}: unknown task type {row[kind]!r}; refusing to discard it")
            if not row[name]:
                raise MeasurementError(f"{path}:{line}: empty kernel name")
            start = _decimal(row[start_key], "task start") * start_unit
            duration = _decimal(row[duration_key], "device task duration", positive=True) * duration_unit
            if stop is not None:
                stop_key, stop_unit = stop
                end = _decimal(row[stop_key], "task stop") * stop_unit
                if abs(end - start - duration) > Decimal("0.02"):
                    raise MeasurementError(f"{path}:{line}: task start/stop/duration disagree")
            identity = (_id(row[device], "device"), _id(row[stream], "stream"), _id(row[task], "task"))
            if "Block Num" in row and (not row["Block Num"].isdigit() or int(row["Block Num"]) < 1):
                raise MeasurementError(f"{path}:{line}: invalid Block Num")
            rows.append({"identity": identity, "kernel": row[name], "task_type": task_type,
                         "start": start, "duration": duration, "end": start + duration,
                         "source_file": str(path), "source_line": line, "schema": schema})
    except csv.Error as exc:
        raise MeasurementError(f"invalid CSV rows in {path}: {exc}") from exc
    return schema, rows, skipped, hashlib.sha256(raw).hexdigest()


def parse_msprof_csvs(
    paths: Iterable[str | Path] | str | Path, *, expected_invocations: int,
    warmups: int = 0, expected_device_id: int | str | None = None,
    expected_kernel: str | None = None,
    invocation_windows: Sequence[Mapping[str, Any]] | None = None,
    assume_one_kernel_per_invocation: bool = False,
) -> dict[str, Any]:
    """Parse one isolated capture; failures raise MeasurementError.

    ``expected_invocations`` includes warmups.  Files from the same capture may
    include op_summary plus task_time; they must describe the same task IDs and
    agree within 0.02 us.  Repeated identities within one schema are errors.
    Device-clock windows use start_us/end_us and must establish an unambiguous
    bijection. Host-clock windows must not be passed without clock calibration.
    Without windows, timestamp order only gives a DECLARED association when the
    caller explicitly sets assume_one_kernel_per_invocation=True. It does not
    prove that each public invocation launched one kernel.
    """
    expected_invocations = _count(expected_invocations, "expected_invocations")
    warmups = _count(warmups, "warmups", allow_zero=True)
    if warmups >= expected_invocations:
        raise MeasurementError("warmups must leave at least one measured invocation")
    if type(assume_one_kernel_per_invocation) is not bool:
        raise MeasurementError("assume_one_kernel_per_invocation must be bool")
    if isinstance(paths, (str, Path)):
        paths = [paths]
    paths = [Path(p) for p in paths]
    if not paths or len({str(p.resolve()) for p in paths}) != len(paths):
        raise MeasurementError("no CSV files or duplicate CSV path")
    by_schema: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}
    skipped, inputs = [], []
    for path in paths:
        schema, rows, ignored, digest = _parse_table(path)
        target = by_schema.setdefault(schema, {})
        skipped.extend(ignored)
        inputs.append({"path": str(path), "sha256": digest, "schema": schema})
        for row in rows:
            if row["identity"] in target:
                raise MeasurementError(f"duplicate {schema} task identity {row['identity']}; no deduplication or fastest selection")
            target[row["identity"]] = row
    selected = by_schema.get("op_summary", by_schema.get("task_time", {}))
    if not selected:
        raise MeasurementError("no device compute tasks")
    for schema, table in by_schema.items():
        if set(table) != set(selected):
            raise MeasurementError(f"{schema} task identities do not match the other device table")
        for identity, row in table.items():
            primary = selected[identity]
            if row["kernel"] != primary["kernel"] or row["task_type"] != primary["task_type"]:
                raise MeasurementError(f"task name/type mismatch across tables: {identity}")
            if any(abs(row[k] - primary[k]) > Decimal("0.02") for k in ("start", "duration")):
                raise MeasurementError(f"device task timestamps/durations disagree across tables: {identity}")
    rows = sorted(selected.values(), key=lambda r: (r["start"], r["identity"]))
    devices = {r["identity"][0] for r in rows}
    streams = {(r["identity"][0], r["identity"][1]) for r in rows}
    if len(devices) != 1 or len(streams) != 1:
        raise MeasurementError("capture must have one compute device and one compute stream")
    if expected_device_id is not None and devices != {_id(str(expected_device_id), "expected_device_id")}:
        raise MeasurementError("device ID differs from requested device")
    kernels = {r["kernel"] for r in rows}
    if expected_kernel is not None and kernels != {expected_kernel}:
        raise MeasurementError(f"unexpected compute kernel names: {sorted(kernels)}")
    if len(kernels) != 1:
        raise MeasurementError("multiple compute kernels in isolated case capture")
    if len(rows) != expected_invocations:
        raise MeasurementError(f"expected {expected_invocations} compute task records, got {len(rows)}; multi-kernel/missing-call capture unsupported")
    if any(a["end"] > b["start"] for a, b in zip(rows, rows[1:])):
        raise MeasurementError("overlapping device tasks in supposed serial capture")
    if invocation_windows is not None:
        if len(invocation_windows) != expected_invocations:
            raise MeasurementError("invocation window count differs from expected_invocations")
        windows = []
        for i, window in enumerate(invocation_windows):
            lo, hi = _decimal(window.get("start_us"), "window start_us"), _decimal(window.get("end_us"), "window end_us")
            if hi <= lo:
                raise MeasurementError("invocation window has nonpositive extent")
            windows.append((lo, hi, window.get("invocation_id", i)))
        if len({str(w[2]) for w in windows}) != len(windows):
            raise MeasurementError("duplicate invocation IDs")
        if any(a[1] > b[0] for a, b in zip(windows, windows[1:])):
            raise MeasurementError("invocation windows overlap or are out of order")
        matched = set()
        for lo, hi, _ in windows:
            candidates = [i for i, row in enumerate(rows) if lo <= row["start"] and row["end"] <= hi]
            if len(candidates) != 1 or candidates[0] in matched:
                raise MeasurementError("each device-clock invocation window must contain exactly one unique complete compute task")
            matched.add(candidates[0])
        if matched != set(range(len(rows))):
            raise MeasurementError("unassociated device compute tasks")
        association = "DEVICE_CLOCK_WINDOWS_ONE_TASK_EACH"
        valid = True
    else:
        windows = [(None, None, i) for i in range(len(rows))]
        valid = assume_one_kernel_per_invocation
        association = "DECLARED_NOT_INDEPENDENTLY_VERIFIED" if valid else "UNKNOWN_WITHOUT_INVOCATION_WINDOWS_OR_DECLARATION"
    tasks = [{"invocation_index": i if valid else None,
              "invocation_id": windows[i][2] if valid else None,
              "warmup": i < warmups, "device_id": int(row["identity"][0]),
              "stream_id": row["identity"][1], "task_id": row["identity"][2],
              "kernel": row["kernel"], "task_type": row["task_type"],
              "device_start_us": str(row["start"]), "device_end_us": str(row["end"]),
              "duration_us": _float(row["duration"], "device task duration"), "compute_task_count": 1,
              "source_file": row["source_file"], "source_line": row["source_line"]}
             for i, row in enumerate(rows)]
    summary = summarize_samples([t["duration_us"] for t in tasks[warmups:]])
    return {"metric": MSPROF_METRIC, "unit": "us", "measurement_valid": valid,
            "valid_for_scoring": valid, "status": ("VALID_WINDOW_ASSOCIATED" if invocation_windows is not None else "VALID_WITH_DECLARED_ASSOCIATION") if valid else "ASSOCIATION_UNKNOWN",
            "association": association, "one_kernel_rule_verdict": "NOT_ASSESSED",
            "assumption": "one compute task per public invocation in an isolated serial single-case process",
            "expected_invocations": expected_invocations, "warmups": warmups,
            "device_task_count": len(rows), "all_samples_us": [t["duration_us"] for t in tasks],
            "tasks": tasks, "per_invocation": tasks if valid else [],
            "csv_inputs": inputs, "non_compute_rows": skipped,
            "cross_checked_schemas": sorted(by_schema), **summary}


def parse_msprof_csv(path: str | Path, **kwargs: Any) -> dict[str, Any]:
    """Convenience entry point for one op_summary or task_time CSV."""
    return parse_msprof_csvs([path], **kwargs)


def summarize_acl_events(records: Sequence[Mapping[str, Any]], *, warmups: int = 0) -> dict[str, Any]:
    """ACL event durations are a separate metric and never msprof substitutes."""
    warmups = _count(warmups, "warmups", allow_zero=True)
    if len(records) <= warmups:
        raise MeasurementError("ACL event records leave no measured samples")
    samples = []
    for record in records:
        if record.get("metric") != ACL_EVENT_METRIC or record.get("unit") not in ("us", "ms"):
            raise MeasurementError("ACL event record must identify metric and explicit us/ms unit")
        duration = _decimal(record.get("duration"), "ACL event duration", positive=True)
        samples.append(_float(duration * _UNITS[record["unit"]], "ACL event duration"))
    return {"metric": ACL_EVENT_METRIC, "unit": "us", "warmups": warmups,
            "all_samples_us": samples, **summarize_samples(samples[warmups:]),
            "comparable_to_msprof": False}


def summarize_captures(captures: Sequence[Mapping[str, Any]], *, expected_captures: int) -> dict[str, Any]:
    """Use every independently collected capture, with identical sample plans.

    No minimum or fastest selection. Failed captures invalidate measurement;
    the controller may not silently collect replacements until a good one wins.
    """
    expected_captures = _count(expected_captures, "expected_captures")
    if len(captures) != expected_captures:
        raise MeasurementError("capture count differs from the frozen protocol")
    medians, plans = [], set()
    for capture in captures:
        if (capture.get("metric") != MSPROF_METRIC
                or capture.get("measurement_valid") is not True
                or capture.get("valid_for_scoring", True) is not True):
            raise MeasurementError("one or more captures lack valid device measurements")
        summary = summarize_samples(capture.get("samples_us", []))
        median = _float(capture.get("median_us"), "capture median")
        if median != summary["median_us"]:
            raise MeasurementError("capture median does not match its retained samples")
        if capture.get("sample_count") != summary["sample_count"]:
            raise MeasurementError("capture sample count is inconsistent")
        warmups = _count(capture.get("warmups"), "capture warmups", allow_zero=True)
        invocations = _count(capture.get("expected_invocations"), "capture expected_invocations")
        if invocations != warmups + summary["sample_count"]:
            raise MeasurementError("capture invocation plan is inconsistent")
        plans.add((warmups, invocations, summary["sample_count"]))
        medians.append(median)
    if len(plans) != 1:
        raise MeasurementError("independent captures use different sampling plans")
    warmups, invocations, measured = next(iter(plans))
    return {"metric": MSPROF_METRIC, "unit": "us", "measurement_valid": True,
            "median_us": statistics.median(medians), "capture_medians_us": medians,
            "capture_count": len(captures), "warmups": warmups,
            "measured_calls": measured, "expected_invocations": invocations,
            "aggregation": "median_of_per_capture_medians", "outliers_removed": 0,
            "captures": list(captures)}


def case_identity(value: Any) -> str:
    """Canonical case IDs support operator-owned names and legacy integer IDs."""
    if type(value) is int and value >= 0:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise MeasurementError("case ID must be a canonical nonempty identifier")
    return value


def expected_cases(values: Sequence[Any]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise MeasurementError("expected case IDs must be a sequence")
    result = [case_identity(value) for value in values]
    if not result or len(result) != len(set(result)):
        raise MeasurementError("expected case IDs must be nonempty and unique")
    return result


def case_weights(ids: Sequence[str], weights: Mapping[Any, Any] | None = None) -> dict[str, float]:
    if weights is None:
        return {cid: 1.0 for cid in ids}
    if not isinstance(weights, Mapping):
        raise MeasurementError("case weights must be a mapping")
    result = {}
    for cid, value in weights.items():
        key = case_identity(cid)
        if key in result:
            raise MeasurementError("duplicate normalized weight ID")
        result[key] = _float(value, "case weight")
    if set(result) != set(ids) or not math.isfinite(sum(result.values())):
        raise MeasurementError("weights must cover exactly the frozen cases with finite total")
    return result


def _case_map(records: Iterable[Mapping[str, Any]], ids: Sequence[str]) -> dict[str, Mapping[str, Any]]:
    result = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise MeasurementError("case measurement must be an object")
        cid = case_identity(record.get("case_id"))
        if cid in result:
            raise MeasurementError("duplicate case measurement")
        result[cid] = record
    if set(result) != set(ids):
        raise MeasurementError("measurements must cover exactly the frozen performance suite")
    return result


def reference_values(reference: Mapping[Any, Any], ids: Sequence[str]) -> dict[str, float]:
    values = reference.get("case_medians_us", reference)
    if not isinstance(values, Mapping):
        raise MeasurementError("reference must map all expected case IDs to positive times")
    result = {}
    for cid, value in values.items():
        key = case_identity(cid)
        if key in result:
            raise MeasurementError("duplicate normalized reference case ID")
        result[key] = _float(value, "reference time")
    if set(result) != set(ids):
        raise MeasurementError("reference must cover exactly the frozen performance suite")
    return result


def score_cases(case_results: Iterable[Mapping[str, Any]], reference: Mapping[Any, Any], *,
                source_sha256: str, expected_case_ids: Sequence[Any],
                weights: Mapping[Any, Any] | None = None,
                baseline_source_sha256: str | None = None) -> dict[str, Any]:
    """Score operator-owned cases; never manufacture a score from invalid timing.

    The controller owns the full correctness-suite gate. Per-case failure also
    gives zero task credit; an unknown correctness state or unusable timing is
    an invalid experiment (MeasurementError), not evidence of a slow candidate.
    This is a local reference protocol, never an official-platform claim.
    """
    source_sha256 = _sha(source_sha256, "candidate source_sha256")
    if baseline_source_sha256 is not None and reference.get("source_sha256") is not None and baseline_source_sha256 != reference["source_sha256"]:
        raise MeasurementError("explicit reference SHA disagrees with the frozen baseline")
    baseline_source_sha256 = _sha(baseline_source_sha256 or reference.get("source_sha256"), "baseline source SHA")
    if reference.get("metric", MSPROF_METRIC) != MSPROF_METRIC:
        raise MeasurementError("reference metric is not device task duration")
    ids = expected_cases(expected_case_ids)
    weight_values = case_weights(ids, weights)
    cases = _case_map(case_results, ids)
    values = reference_values(reference, ids)
    if "weights" in reference and case_weights(ids, reference["weights"]) != weight_values:
        raise MeasurementError("candidate scoring weights differ from the frozen baseline")
    reference_records = (_case_map(reference["measurements"], ids)
                         if "measurements" in reference else None)
    rows, incorrect, invalid = [], [], []
    for cid in ids:
        record = cases[cid]
        if record.get("source_sha256") != source_sha256:
            raise MeasurementError("candidate source identity is missing or differs")
        if record.get("metric") not in (None, MSPROF_METRIC):
            raise MeasurementError("host or ACL event times cannot substitute for device task durations")
        if reference_records is not None:
            original = reference_records[cid]
            for identity in ("actual_cores", "input_manifest_sha256"):
                if identity in original and record.get(identity) != original[identity]:
                    raise MeasurementError("candidate/reference measurement conditions differ: " + identity)
        if "sampling_plan" in reference:
            captures = record.get("captures")
            if not isinstance(captures, list) or not captures:
                raise MeasurementError("candidate lacks retained capture evidence")
            if any(not isinstance(capture, Mapping) or capture.get("measurement_valid") is not True
                   or not isinstance(capture.get("correctness"), Mapping)
                   or capture["correctness"].get("passed") is not True for capture in captures):
                raise MeasurementError("candidate profiling capture correctness/measurement failed")
            aggregate = summarize_captures([capture.get("parsed", {}) for capture in captures],
                                          expected_captures=reference["sampling_plan"]["capture_count"])
            if (any(aggregate.get(key) != value for key, value in reference["sampling_plan"].items())
                    or aggregate["median_us"] != record.get("median_us")
                    or aggregate["capture_medians_us"] != record.get("capture_medians_us")):
                raise MeasurementError("candidate samples/aggregation differ from the frozen measurement plan")
        correctness = record.get("correctness_passed")
        correct = correctness is True
        measured = record.get("measurement_valid") is True and record.get("metric") == MSPROF_METRIC
        elapsed = _float(record["median_us"], "candidate median") if record.get("median_us") is not None else None
        ready = correct and measured and elapsed is not None
        if correctness is False:
            incorrect.append(cid)
        elif not ready:
            invalid.append(cid)
        ratio = elapsed / values[cid] if elapsed is not None else None
        speedup = values[cid] / elapsed if elapsed is not None else None
        if ratio is not None and (not math.isfinite(ratio) or ratio <= 0 or not math.isfinite(speedup) or speedup <= 0):
            raise MeasurementError("performance ratio outside finite positive range")
        clamped = max(1.0, ratio) if ratio is not None else None
        point = 100 / (1 + math.log(clamped) / math.log(1.5)) if ready else None
        rows.append({"case_id": cid, "correctness_passed": correct, "measurement_valid": measured,
                     "median_us": elapsed, "reference_us": values[cid], "ratio_to_reference": ratio,
                     "speedup": speedup, "clamped_ratio": clamped, "case_score": point,
                     "weight": weight_values[cid]})
    if incorrect:
        score, status = 0.0, "CORRECTNESS_GATE_FAILED"
    elif invalid:
        raise MeasurementError("invalid or incomplete timing/correctness evidence for cases: " + ", ".join(invalid))
    else:
        total_weight = sum(weight_values.values())
        score = sum(row["case_score"] * (row["weight"] / total_weight) for row in rows)
        status = "COMPLETE_LOCAL_REFERENCE_SCORE"
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"score": score, "score_status": status, "all_correctness_passed": not incorrect and all(row["correctness_passed"] for row in rows),
            "all_measurements_valid": all(row["measurement_valid"] and row["median_us"] is not None for row in rows),
            "incorrect_cases": incorrect, "invalid_cases": invalid, "cases": rows, "case_count": len(ids),
            "metric": MSPROF_METRIC, "unit": "us", "is_official_platform_score": False,
            "reference_is_theoretical_optimum": False,
            "aggregation": "arithmetic_mean" if weights is None else "weighted_arithmetic_mean",
            "formula": "weighted_mean(100/(1+log_1.5(max(1,t_candidate/t_reference))))",
            "source_sha256": source_sha256, "baseline_source_sha256": baseline_source_sha256,
            "reference_vector_sha256": digest, "reference_case_medians_us": values}

