import copy
import json
from pathlib import Path
import tempfile
import unittest

from engine.calibration import (BaselineError, baseline_digest, create_baseline,
                                load_baseline, save_baseline, validate_baseline)
from engine.metrics import MSPROF_METRIC


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.bindings = dict(package_digest="a"*64, environment={"device": 0, "sdk": "9", "fingerprint": "env"},
                             reference_sha256="b"*64, engine_digest="c"*64, protocol_digest="d"*64,
                             expected_case_ids=["small", "large"])
        self.report = {"outcome": "passed", "action": "final", "source_sha256": "b"*64,
                       "problem_digest": "a"*64, "binary_sha256": "e"*64,
                       "stages": {s: "passed" for s in ("build", "runtime", "correctness", "performance")},
                       "correctness": {"passed": 4, "total": 4, "scope": "public_and_hidden_full"},
                       "measurements": [dict(case_id=cid, median_us=time, source_sha256="b"*64,
                                             metric=MSPROF_METRIC, measurement_valid=True, correctness_passed=True)
                                        for cid,time in (("small", 1.2), ("large", 5.6))]}
        for measurement in self.report["measurements"]:
            elapsed = measurement["median_us"]
            parsed = {"metric": MSPROF_METRIC, "measurement_valid": True, "valid_for_scoring": True,
                      "samples_us": [elapsed]*3, "sample_count": 3, "median_us": elapsed,
                      "warmups": 1, "expected_invocations": 4}
            measurement["captures"] = [{"parsed": parsed, "measurement_valid": True,
                                         "correctness": {"passed": True}, "actual_cores": 24}]
            measurement["capture_medians_us"] = [elapsed]

    def create(self, report=None, **bindings):
        return create_baseline(self.report if report is None else report,
                               **dict(self.bindings, **bindings), created_at="2026-11-16T00:00:00+00:00")

    def test_baseline_roundtrip_hash_bindings_and_reference_semantics(self):
        baseline = self.create()
        self.assertEqual(validate_baseline(baseline, **self.bindings), baseline)
        self.assertEqual(baseline["baseline_id"], baseline_digest(baseline))
        self.assertEqual(baseline["case_medians_us"], {"small": 1.2, "large": 5.6})
        self.assertFalse(baseline["reference_is_theoretical_optimum"])
        self.assertFalse(baseline["rankable"])
        self.assertEqual(baseline["security_label"], "not_cheat_resistant")

    def test_changed_package_source_engine_protocol_environment_or_cases_rejected(self):
        baseline = self.create()
        for key,value in (("package_digest", "f"*64), ("reference_sha256", "f"*64),
                          ("engine_digest", "f"*64), ("protocol_digest", "f"*64),
                          ("environment", {"device": 1}), ("expected_case_ids", ["large", "small"]),
                          ("weights", {"small": 2, "large": 1})):
            with self.subTest(binding=key), self.assertRaises(BaselineError):
                validate_baseline(baseline, **dict(self.bindings, **{key:value}))

    def test_editing_vector_or_samples_breaks_identity_and_consistency(self):
        baseline = self.create()
        baseline["case_medians_us"]["small"] = 0.01
        with self.assertRaises(BaselineError):
            validate_baseline(baseline, **self.bindings)
        baseline["baseline_id"] = baseline_digest(baseline)
        with self.assertRaisesRegex(BaselineError, "evidence"):
            validate_baseline(baseline, **self.bindings)

    def test_public_only_or_partial_correctness_cannot_calibrate(self):
        for change in (dict(action="bench"), dict(outcome="correctness_failed"),
                       dict(correctness={"passed": 2,"total": 2,"scope": "public_full"}),
                       dict(correctness={"passed": 3,"total": 4,"scope": "public_and_hidden_full"}),
                       dict(stages={"build": "passed", "runtime": "passed", "correctness": "passed", "performance": "not_run"})):
            with self.subTest(change=change), self.assertRaises(BaselineError):
                self.create(dict(self.report, **change))

    def test_missing_failed_duplicate_mixed_metric_or_source_measurements_rejected(self):
        for change in (dict(measurement_valid=False), dict(correctness_passed=False),
                       dict(metric="acl_event_elapsed"), dict(source_sha256="f"*64), dict(median_us=0)):
            report = copy.deepcopy(self.report)
            report["measurements"][0].update(change)
            with self.subTest(change=change), self.assertRaises(BaselineError):
                self.create(report)
        for rows in (self.report["measurements"][:1], self.report["measurements"]*2):
            with self.assertRaises(BaselineError):
                self.create(dict(self.report, measurements=rows))

    def test_v1_baseline_rejected_even_with_matching_times(self):
        legacy = {"source_sha256": "b"*64, "case_medians_us": {"small": 1.2, "large": 5.6}}
        with self.assertRaisesRegex(BaselineError, "fresh v2"):
            validate_baseline(legacy, **self.bindings)

    def test_storage_is_exclusive_and_load_checks_all_bindings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "baseline.json"
            baseline = self.create()
            save_baseline(path, baseline)
            self.assertEqual(load_baseline(path, **self.bindings), baseline)
            with self.assertRaises(FileExistsError):
                save_baseline(path, baseline)
            path.write_text(json.dumps(dict(baseline, source_sha256="f"*64)))
            with self.assertRaises(BaselineError):
                load_baseline(path, **self.bindings)

    def test_report_environment_identity_must_match_measured_binding(self):
        with self.assertRaises(BaselineError):
            self.create(dict(self.report, environment={"device": 4}))

    def test_capture_samples_cannot_be_omitted_or_disagree_with_reference_median(self):
        for field, value in (("captures", []), ("capture_medians_us", [0.001]), ("median_us", 0.001)):
            report = copy.deepcopy(self.report)
            report["measurements"][0][field] = value
            with self.subTest(field=field), self.assertRaises(BaselineError):
                self.create(report)
        report = copy.deepcopy(self.report)
        report["measurements"][0]["captures"][0]["parsed"]["samples_us"] = [0.001]*3
        with self.assertRaises(BaselineError):
            self.create(report)

    def test_malformed_report_is_a_baseline_error(self):
        for field in ("stages", "correctness", "measurements"):
            with self.subTest(field=field), self.assertRaises(BaselineError):
                self.create(dict(self.report, **{field: None}))


if __name__ == "__main__":
    unittest.main()
