"""CPU-only synthetic parser fixtures; none are NPU measurement evidence."""

import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

from engine.metrics import (
    ACL_EVENT_METRIC, MSPROF_METRIC, MeasurementError,
    parse_msprof_csv, parse_msprof_csvs, score_cases, summarize_acl_events, summarize_captures,
    summarize_samples,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
OP_HEADERS = ["Device_id", "Task ID", "Stream ID", "Task Type", "Op Name",
              "Task Start Time(us)", "Task Duration(us)", "Block Num"]
TASK_HEADERS = ["Device_id", "task_id", "stream_id", "kernel_type", "kernel_name",
                "task_start(us)", "task_time(us)", "task_stop(us)"]


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def csv(self, name="op_summary.csv", headers=None, rows=None):
        path = self.root / name
        headers = headers or OP_HEADERS
        rows = self.rows() if rows is None else rows
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        return path

    def rows(self, durations=(8, 2, 100, 4), task_type="MIX_AIC"):
        return [[0, i + 1, 3, task_type, "kernel", 1000 + 200 * i, d, 20]
                for i, d in enumerate(durations)]

    def parse(self, path=None, **kwargs):
        options = dict(expected_invocations=4, warmups=1,
                       assume_one_kernel_per_invocation=True)
        options.update(kwargs)
        return parse_msprof_csv(path or self.csv(), **options)

    def test_all_measured_samples_preserved_slow_outlier_not_selected_away(self):
        result = self.parse()
        self.assertEqual(result["samples_us"], [2, 100, 4])
        self.assertEqual(result["median_us"], 4)
        self.assertEqual(result["all_samples_us"], [8, 2, 100, 4])
        self.assertEqual(result["outliers_removed"], 0)
        self.assertEqual(result["association"], "DECLARED_NOT_INDEPENDENTLY_VERIFIED")
        self.assertEqual(result["one_kernel_rule_verdict"], "NOT_ASSESSED")
        json.dumps(result, allow_nan=False)

    def test_ai_vector_core_is_compute(self):
        result = self.parse(self.csv(rows=self.rows(task_type="AI_VECTOR_CORE")))
        self.assertEqual(result["device_task_count"], 4)

    def test_no_invocation_assumption_marks_measurement_unknown(self):
        result = self.parse(assume_one_kernel_per_invocation=False)
        self.assertFalse(result["measurement_valid"])
        self.assertFalse(result["valid_for_scoring"])
        self.assertEqual(result["per_invocation"], [])
        self.assertIsNone(result["tasks"][0]["invocation_index"])

    def test_device_clock_windows_prove_one_to_one(self):
        windows = [dict(start_us=1000 + i * 200, end_us=1150 + i * 200,
                        invocation_id=f"call_{i}") for i in range(4)]
        result = self.parse(invocation_windows=windows, assume_one_kernel_per_invocation=False)
        self.assertTrue(result["measurement_valid"])
        self.assertEqual(result["association"], "DEVICE_CLOCK_WINDOWS_ONE_TASK_EACH")
        self.assertEqual(result["per_invocation"][1]["invocation_id"], "call_1")

    def test_overlapping_or_wrong_clock_windows_rejected(self):
        for windows in [[dict(start_us=0, end_us=900)] * 4,
                        [dict(start_us=1000, end_us=4000)] * 4]:
            with self.subTest(windows=windows), self.assertRaises(MeasurementError):
                self.parse(invocation_windows=windows)

    def test_duplicate_invocation_ids_rejected(self):
        windows = [dict(start_us=1000 + i * 200, end_us=1150 + i * 200,
                        invocation_id="same") for i in range(4)]
        with self.assertRaises(MeasurementError):
            self.parse(invocation_windows=windows)

    def test_counts_missing_extra_or_warmups_without_measured_samples_rejected(self):
        for count in (3, 5):
            with self.subTest(count=count), self.assertRaises(MeasurementError):
                self.parse(expected_invocations=count)
        with self.assertRaises(MeasurementError):
            self.parse(warmups=4)

    def test_duplicate_task_identity_not_deduplicated(self):
        rows = self.rows()
        rows[1][1] = rows[0][1]
        with self.assertRaises(MeasurementError):
            self.parse(self.csv(rows=rows))

    def test_duplicate_same_table_files_not_fastest_selection(self):
        a = self.csv("a.csv")
        b = self.csv("b.csv")
        with self.assertRaises(MeasurementError):
            parse_msprof_csvs([a, b], expected_invocations=4)
        with self.assertRaises(MeasurementError):
            parse_msprof_csvs([a, a], expected_invocations=4)

    def test_task_time_and_op_summary_cross_check_same_ids(self):
        op = self.csv()
        rows = [[r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[5] + r[6]]
                for r in self.rows()]
        task = self.csv("task_time.csv", TASK_HEADERS, rows)
        result = parse_msprof_csvs([op, task], expected_invocations=4, warmups=1,
                                  assume_one_kernel_per_invocation=True)
        self.assertEqual(result["cross_checked_schemas"], ["op_summary", "task_time"])
        self.assertEqual(result["sample_count"], 3)

    def test_cross_table_missing_identity_or_duration_disagreement_rejected(self):
        op = self.csv()
        rows = [[r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[5] + r[6]]
                for r in self.rows()]
        for candidate in (rows[:-1], [[*rows[0][:6], 9, rows[0][5] + 9]] + rows[1:]):
            with self.subTest(candidate=candidate), self.assertRaises(MeasurementError):
                parse_msprof_csvs([op, self.csv("task_time.csv", TASK_HEADERS, candidate)],
                                  expected_invocations=4)

    def test_task_time_stop_duration_consistency(self):
        rows = [[0, 1, 3, "AI_CORE", "kernel", 1000, 2, 1005]]
        with self.assertRaises(MeasurementError):
            self.parse(self.csv("task.csv", TASK_HEADERS, rows), expected_invocations=1, warmups=0)

    def test_ns_unit_conversion_ignores_host_duration_and_task_wait(self):
        headers = OP_HEADERS.copy()
        headers[5] = "Task Start Time(ns)"
        headers[6] = "Task Duration(ns)"
        headers += ["Host Duration(us)", "Task Wait Time(us)"]
        rows = [[*r[:5], r[5] * 1000, r[6] * 1000, r[7], 9999999, 9999999]
                for r in self.rows()]
        self.assertEqual(self.parse(self.csv(headers=headers, rows=rows))["median_us"], 4)

    def test_ambiguous_duration_without_unit_rejected(self):
        headers = OP_HEADERS.copy()
        headers[6] = "Task Duration"
        with self.assertRaises(MeasurementError):
            self.parse(self.csv(headers=headers))

    def test_host_or_pmu_csv_rejected(self):
        for headers, rows in [(["Name", "Duration(us)"], [["aclrtLaunchKernel", 5]]),
                              (["Core ID", "aic_total_time"], [[0, 5]])]:
            with self.subTest(headers=headers), self.assertRaises(MeasurementError):
                self.parse(self.csv(headers=headers, rows=rows))

    def test_zero_negative_nonfinite_and_overflow_duration_rejected(self):
        for value in (0, -1, "nan", "inf", "1e10000", "1e-10000"):
            rows = self.rows()
            rows[2][6] = value
            with self.subTest(value=value), self.assertRaises(MeasurementError):
                self.parse(self.csv(rows=rows))

    def test_multiple_devices_streams_or_kernel_names_rejected(self):
        for column, value in [(0, 1), (2, 8), (4, "second_kernel")]:
            rows = self.rows()
            rows[1][column] = value
            with self.subTest(column=column), self.assertRaises(MeasurementError):
                self.parse(self.csv(rows=rows))

    def test_expected_kernel_or_device_mismatch_rejected(self):
        with self.assertRaises(MeasurementError):
            self.parse(expected_device_id=1)
        with self.assertRaises(MeasurementError):
            self.parse(expected_kernel="wrong")

    def test_overlapping_serial_tasks_rejected(self):
        rows = self.rows()
        rows[0][6] = 201
        with self.assertRaises(MeasurementError):
            self.parse(self.csv(rows=rows))

    def test_unknown_task_type_not_discarded(self):
        rows = self.rows()
        rows.append([0, 9, 3, "NEW_UNKNOWN_KERNEL_TYPE", "x", 4000, 3, 1])
        with self.assertRaises(MeasurementError):
            self.parse(self.csv(rows=rows))

    def test_known_memcpy_rows_not_counted_as_compute(self):
        rows = self.rows()
        rows.append([0, 9, 3, "MEMCPY_ASYNC", "copy", 4000, 3, 1])
        result = self.parse(self.csv(rows=rows))
        self.assertEqual(result["device_task_count"], 4)
        self.assertEqual(len(result["non_compute_rows"]), 1)

    def test_bad_csv_width_or_duplicate_header_rejected(self):
        with self.assertRaises(MeasurementError):
            self.parse(self.csv(rows=[self.rows()[0][:-1]]))
        with self.assertRaises(MeasurementError):
            self.parse(self.csv(headers=OP_HEADERS + ["Task ID"], rows=[self.rows()[0] + [1]]))

    def test_decimal_large_epoch_preserved_without_float_matching_loss(self):
        rows = self.rows()
        for i, row in enumerate(rows):
            row[5] = str(1770000000000000 + i * 200) + ".001"
        result = self.parse(self.csv(rows=rows))
        self.assertTrue(result["tasks"][0]["device_start_us"].endswith(".001"))

    def records(self, **changes):
        rows = [dict(case_id=cid, correctness_passed=True, measurement_valid=True,
                     median_us=1.5, metric=MSPROF_METRIC, source_sha256=SHA_A)
                for cid in ("tiny", "awkward", "large")]
        rows[0].update(changes)
        return rows

    def score(self, rows=None, **kwargs):
        return score_cases(self.records() if rows is None else rows,
                           {"source_sha256": SHA_B, "metric": MSPROF_METRIC,
                            "case_medians_us": {cid: 1 for cid in ("tiny", "awkward", "large")}},
                           source_sha256=SHA_A, expected_case_ids=["tiny", "awkward", "large"], **kwargs)

    def test_named_cases_preserve_formula_and_uncapped_speedup(self):
        result = self.score()
        self.assertAlmostEqual(result["score"], 50)
        self.assertEqual(result["case_count"], 3)
        self.assertAlmostEqual(result["cases"][0]["speedup"], 1/1.5)
        faster = self.score(self.records(median_us=0.5))
        self.assertEqual(faster["cases"][0]["case_score"], 100)
        self.assertEqual(faster["cases"][0]["speedup"], 2)
        self.assertFalse(result["reference_is_theoretical_optimum"])

    def test_package_weights_not_candidate_fastest_subset(self):
        result = self.score(self.records(median_us=1), weights={"tiny": 2, "awkward": 1, "large": 1})
        self.assertAlmostEqual(result["score"], 75)
        for weights in ({"tiny": 1}, {"tiny": 0, "awkward": 1, "large": 1},
                        {"tiny": True, "awkward": 1, "large": 1}):
            with self.subTest(weights=weights), self.assertRaises(MeasurementError):
                self.score(weights=weights)

    def test_invalid_measurement_has_no_zero_or_partial_score(self):
        for update in (dict(measurement_valid=False), dict(median_us=None),
                       dict(correctness_passed=None), dict(correctness_passed=1),
                       dict(metric=ACL_EVENT_METRIC), dict(median_us=float("nan"))):
            with self.subTest(update=update), self.assertRaises(MeasurementError):
                self.score(self.records(**update))

    def test_explicit_wrong_output_gates_score(self):
        result = self.score(self.records(correctness_passed=False))
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["score_status"], "CORRECTNESS_GATE_FAILED")

    def test_missing_duplicate_and_mixed_source_are_rejected(self):
        for records in (self.records()[:-1], self.records() + self.records()[:1], self.records(source_sha256=SHA_B)):
            with self.subTest(records=records), self.assertRaises(MeasurementError):
                self.score(records)

    def test_aliasing_integer_case_ids_are_rejected(self):
        with self.assertRaises(MeasurementError):
            score_cases([], {"source_sha256": SHA_B}, source_sha256=SHA_A, expected_case_ids=[1, "1"])

    def test_baseline_input_and_core_identity_must_match_candidate(self):
        rows = self.records()
        ref_rows = [dict(row, actual_cores=24, input_manifest_sha256="c"*64) for row in rows]
        reference = {"source_sha256": SHA_B, "case_medians_us": {row["case_id"]: 1 for row in rows},
                     "measurements": ref_rows}
        with self.assertRaisesRegex(MeasurementError, "conditions differ"):
            score_cases(rows, reference, source_sha256=SHA_A, expected_case_ids=[row["case_id"] for row in rows])
        matched = [dict(row, actual_cores=24, input_manifest_sha256="c"*64) for row in rows]
        self.assertAlmostEqual(score_cases(matched, reference, source_sha256=SHA_A,
                                          expected_case_ids=[row["case_id"] for row in rows])["score"], 50)

    def test_capture_aggregation_retains_all_samples_and_rejects_missing_or_tampered_capture(self):
        captures = [self.parse(self.csv(name=f"capture-{i}.csv", rows=self.rows(durations=(8, value, value, value))))
                    for i, value in enumerate((1, 2, 90))]
        result = summarize_captures(captures, expected_captures=3)
        self.assertEqual(result["median_us"], 2)
        self.assertEqual(result["capture_medians_us"], [1, 2, 90])
        self.assertEqual(result["outliers_removed"], 0)
        with self.assertRaises(MeasurementError):
            summarize_captures(captures[:2], expected_captures=3)
        with self.assertRaises(MeasurementError):
            summarize_captures([dict(captures[0], median_us=0.01), *captures[1:]], expected_captures=3)
        with self.assertRaises(MeasurementError):
            summarize_captures([dict(captures[0], warmups=2), *captures[1:]], expected_captures=3)

