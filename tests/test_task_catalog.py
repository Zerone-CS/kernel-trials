"""Task selection/materialization checks; synthetic timing is not NPU evidence."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest

from engine.calibration import BaselineError, create_baseline, validate_baseline
from engine.identity import protocol_sha256
from engine.metrics import MSPROF_METRIC, MeasurementError, score_cases
from engine.problems import load_problem
from engine.task_catalog import (CAPABILITY_GROUPS, TaskCatalogError, list_tasks,
                                 materialize_task, resolve_tasks)


PARENT = Path(__file__).resolve().parents[1] / "problems" / "batch_matmul_reduce"


class TaskCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS's system temporary directory has a trusted /var -> /private/var
        # alias; use its canonical path while retaining explicit symlink tests.
        self.root = Path(self.temp.name).resolve()
        self.parent = load_problem(PARENT)
        self.parent_files = dict(self.parent.file_hashes)

    def child(self, task_id="case01", destination=None):
        path = materialize_task(PARENT, task_id, destination or self.root / task_id)
        return load_problem(path)

    def test_catalog_has_full_and_fifteen_fixed_shapes_with_overlapping_labels(self):
        tasks = list_tasks()
        self.assertEqual([task["id"] for task in tasks], ["full"] + [f"case{i:02d}" for i in range(1, 16)])
        parent_cases = self.parent.load_adapter().cases("public", 0)
        for task, spec in zip(tasks[1:], parent_cases):
            self.assertEqual(task["case_id"], spec["case_id"])
            self.assertEqual(task["scope"], "fixed_shape")
            self.assertEqual({key: spec[key] for key in ("b", "m", "n", "k", "dtype", "ta", "tb")},
                             {key: task["shape"][key] for key in ("b", "m", "n", "k", "dtype", "ta", "tb")})
        self.assertEqual(tasks[8]["capabilities"], ["tails", "large_gemm"])
        tasks[1]["shape"]["m"] = 999
        tasks[1]["capabilities"].clear()
        self.assertEqual(list_tasks()[1]["shape"]["m"], 1)
        self.assertEqual(list_tasks()[1]["capabilities"], ["tiny_latency"])

    def test_selectors_expand_without_group_score_or_accidental_full_run(self):
        self.assertEqual(resolve_tasks(), ["full"])
        self.assertEqual(resolve_tasks([]), ["full"])
        self.assertEqual(resolve_tasks("all"), [f"case{i:02d}" for i in range(1, 16)])
        self.assertEqual(resolve_tasks(["case08", "group:large_gemm", "full", "case08"]),
                         ["case08", "case09", "case10", "case11", "case12", "full"])
        self.assertEqual(resolve_tasks("group:tails, case01"),
                         ["case02", "case03", "case06", "case08", "case01"])
        for group, members in CAPABILITY_GROUPS.items():
            self.assertEqual(resolve_tasks("group:" + group), [f"case{i:02d}" for i in members])
        for invalid in ("case1", "case00", "case16", "group:nope", "case01,,case02", [1], 7, ""):
            with self.subTest(selector=invalid), self.assertRaises(TaskCatalogError):
                resolve_tasks(invalid)

    def test_all_children_have_distinct_identity_and_only_one_shape_in_final(self):
        digests, protocol_digests = set(), set()
        for task in list_tasks()[1:]:
            with self.subTest(task=task["id"]):
                package = self.child(task["id"])
                digests.add(package.digest)
                protocol_digests.add(protocol_sha256(package))
                self.assertEqual(package.problem_id, "batch_matmul_reduce_" + task["id"])
                self.assertEqual(package.manifest["evaluation_task"], task)
                adapter = package.load_adapter()
                public, hidden = adapter.cases("public", 0), adapter.cases("hidden", 123)
                self.assertEqual(len(public), 1)
                self.assertEqual(len(hidden), 4)
                self.assertEqual(public[0]["case_id"], task["case_id"])
                self.assertEqual({spec["pattern"] for spec in hidden},
                                 {"random", "all_negative", "zero", "cancellation"})
                # This is the exact public + hidden selection used by final.
                for spec in public + hidden:
                    for field in ("b", "m", "n", "k", "dtype", "ta", "tb"):
                        self.assertEqual(spec[field], task["shape"][field])
                    self.assertGreaterEqual(spec["repeats"], 3)
                    self.assertEqual(spec["performance"], spec["suite"] == "public")
                self.assertGreaterEqual(next(spec for spec in hidden if spec["pattern"] == "random")["repeats"], 16)
                self.assertEqual(len({spec["case_id"] for spec in public + hidden}), 5)
                self.assertNotEqual([spec["seed"] for spec in hidden],
                                    [spec["seed"] for spec in adapter.cases("hidden", 124)])
                self.assertEqual(hidden, adapter.cases("hidden", 123))
                protocol = json.loads(package.path("protocol").read_text())
                self.assertTrue(protocol["score"]["requires_fresh_calibration"])
                self.assertIn("1 case", protocol["score"]["aggregate"])
                self.assertEqual(protocol["evaluation_task"], task)
                for key in ("reference", "adapter"):
                    self.assertEqual(package.path(key).read_bytes(), self.parent.path(key).read_bytes())
                for track in ("from_scratch", "optimize"):
                    self.assertEqual(package.starter_path(track).read_bytes(), self.parent.starter_path(track).read_bytes())
                self.assertNotEqual(package.starter_path("optimize").read_bytes(), package.path("reference").read_bytes())
                provenance = json.loads((package.root / "provenance/task.json").read_text())
                self.assertEqual(provenance["parent_package_digest"], self.parent.digest)
                self.assertEqual(provenance["group_semantics"], "selection_only_no_group_score")
                task_text = package.path("task").read_text()
                self.assertIn("固定 shape 特化任务", task_text)
                self.assertIn("不代表 shape 泛化能力", task_text)
                self.assertNotIn("四种属性组合都需要支持", task_text)
                self.assertNotIn("15 个公开性能形状", task_text)
                package.assert_unchanged()
        self.assertEqual(len(digests), 15)
        self.assertEqual(len(protocol_digests), 15)
        self.assertNotIn(self.parent.digest, digests)
        self.parent.assert_unchanged()
        self.assertEqual(load_problem(PARENT).file_hashes, self.parent_files)

    def test_full_returns_original_without_writes_and_retains_39_cases(self):
        ignored = self.root / "must-not-create"
        self.assertEqual(materialize_task(PARENT, "full", ignored), PARENT.resolve())
        self.assertFalse(ignored.exists())
        self.assertEqual(len(self.parent.load_adapter().cases("all", 7)), 39)
        self.parent.assert_unchanged()

    def test_repeat_materialization_only_reuses_identical_bytes(self):
        first = self.child("case06")
        self.assertEqual(self.child("case06").digest, first.digest)
        duplicate = self.child("case06", self.root / "second-copy")
        self.assertEqual(duplicate.digest, first.digest)
        with self.assertRaisesRegex(TaskCatalogError, "differs"):
            materialize_task(PARENT, "case05", first.root)
        (first.root / "task.md").write_text("user change")
        with self.assertRaisesRegex(TaskCatalogError, "refusing overwrite"):
            materialize_task(PARENT, "case06", first.root)
        self.assertEqual((first.root / "task.md").read_text(), "user change")
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(TaskCatalogError):
            materialize_task(PARENT, "case06", empty)

    def test_parent_path_symlinks_unknown_tasks_and_changed_catalog_rejected(self):
        with self.assertRaisesRegex(TaskCatalogError, "outside"):
            materialize_task(PARENT, "case01", PARENT / "new-child")
        with self.assertRaises(TaskCatalogError):
            materialize_task(PARENT, "group:tails", self.root / "group")
        linked = self.root / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(TaskCatalogError, "symlink"):
            materialize_task(PARENT, "case01", linked / "child")
        copied = self.root / "changed-parent"
        shutil.copytree(PARENT, copied)
        cases = json.loads((copied / "cases.json").read_text())
        cases["public"][0]["k"] = 64
        (copied / "cases.json").write_text(json.dumps(cases))
        with self.assertRaisesRegex(TaskCatalogError, "shape differs"):
            materialize_task(copied, "case01", self.root / "invalid-child")
        self.assertFalse((PARENT / "new-child").exists())
        self.parent.assert_unchanged()

    def test_candidate_project_receives_fixed_harness_and_starter_without_judge_or_reference(self):
        child = self.child("case06")
        project = self.root / "candidate"
        starter = child.starter_path("optimize").read_bytes()
        child.stage_project(starter, project)
        self.assertEqual({path.name for path in project.iterdir()}, {"main.asc", "CMakeLists.txt", "kernel.asc"})
        self.assertEqual((project / "kernel.asc").read_bytes(), starter)
        self.assertNotEqual((project / "kernel.asc").read_bytes(), child.path("reference").read_bytes())
        self.assertFalse(child.manifest["public_files"])

    def test_repeated_output_verification_still_rejects_a_single_bad_capture(self):
        package = self.child()
        adapter = package.load_adapter()
        spec = next(item for item in adapter.cases("hidden", 12) if item["pattern"] == "random")
        directory = self.root / "oracle-fixture"
        directory.mkdir()
        # Explicit known-input fixture: K32 half-precision ones have dot 32.
        raw_files = {"x1": struct.pack("<H", 0x3c00) * 32,
                     "x2": struct.pack("<H", 0x3c00) * 32,
                     "golden": struct.pack("<f", 32)}
        artifacts = {}
        for name, raw in raw_files.items():
            filename = name + ".bin"
            (directory / filename).write_bytes(raw)
            artifacts[name] = {"file": filename, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        (directory / "case.json").write_text(json.dumps({
            "spec": {key: value for key, value in spec.items() if key not in ("suite", "performance")},
            "generator_sha256": hashlib.sha256((package.root / "trusted/cases.py").read_bytes()).hexdigest(),
            "artifacts": artifacts,
        }))
        output = self.root / "out.bin"
        output.write_bytes(raw_files["golden"])
        for index in range(spec["repeats"]):
            output.with_name(output.name + ".call-" + str(index)).write_bytes(raw_files["golden"])
        verified = adapter.verify(spec, directory, output, expected_calls=spec["repeats"])
        self.assertTrue(verified["passed"])
        self.assertEqual(verified["checked_captures"], 17)
        output.with_name(output.name + ".call-7").write_bytes(struct.pack("<f", 0))
        self.assertFalse(adapter.verify(spec, directory, output, expected_calls=spec["repeats"])["passed"])

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("ml_dtypes"),
                         "Real input generation requires existing numpy/ml_dtypes; no installation requested")
    def test_small_fixed_shape_generates_and_verifies_all_input_patterns(self):
        package = self.child("case02")
        adapter = package.load_adapter()
        for spec in adapter.cases("all", 17):
            directory = self.root / (spec["case_id"] + "-oracle")
            adapter.prepare(spec, directory)
            output = self.root / (spec["case_id"] + ".bin")
            raw = (directory / "golden.bin").read_bytes()
            output.write_bytes(raw)
            self.assertTrue(adapter.verify(spec, directory, output)["passed"])
            values = struct.unpack("<f", raw)
            if spec["pattern"] == "zero":
                self.assertEqual(values, (0.0,))
            elif spec["pattern"] == "all_negative":
                self.assertLess(values[0], 0)

    def test_single_task_score_is_not_divided_by_fifteen_and_rejects_other_cases(self):
        task = self.child("case06")
        ids = [spec["case_id"] for spec in task.load_adapter().cases("public", 0)]
        source, reference = "a" * 64, "b" * 64
        records = [{"case_id": ids[0], "median_us": 3.0, "source_sha256": source,
                    "metric": MSPROF_METRIC, "measurement_valid": True, "correctness_passed": True}]
        baseline = {"source_sha256": reference, "case_medians_us": {ids[0]: 2.0}}
        scored = score_cases(records, baseline, source_sha256=source, expected_case_ids=ids)
        self.assertEqual(scored["case_count"], 1)
        self.assertAlmostEqual(scored["score"], 50.0)
        self.assertEqual(scored["score"], scored["cases"][0]["case_score"])
        with self.assertRaises(MeasurementError):
            score_cases(records + [dict(records[0], case_id="public_07")], baseline,
                        source_sha256=source, expected_case_ids=ids)
        full_ids = [spec["case_id"] for spec in self.parent.load_adapter().cases("public", 0)]
        with self.assertRaises(MeasurementError):
            score_cases(records, baseline, source_sha256=source, expected_case_ids=full_ids)
        records[0]["correctness_passed"] = False
        self.assertEqual(score_cases(records, baseline, source_sha256=source, expected_case_ids=ids)["score"], 0)

    def test_parent_baseline_cannot_be_reused_for_single_shape(self):
        source = hashlib.sha256(self.parent.path("reference").read_bytes()).hexdigest()
        ids = [spec["case_id"] for spec in self.parent.load_adapter().cases("public", 0)]
        measurements = []
        for cid in ids:
            parsed = {"metric": MSPROF_METRIC, "measurement_valid": True, "valid_for_scoring": True,
                      "samples_us": [2.0] * 32, "sample_count": 32, "median_us": 2.0,
                      "warmups": 8, "expected_invocations": 40}
            measurements.append({"case_id": cid, "source_sha256": source, "median_us": 2.0,
                                 "metric": MSPROF_METRIC, "measurement_valid": True, "correctness_passed": True,
                                 "capture_medians_us": [2.0] * 3,
                                 "captures": [{"parsed": deepcopy(parsed), "measurement_valid": True,
                                               "correctness": {"passed": True}} for _ in range(3)]})
        report = {"outcome": "passed", "action": "final", "source_sha256": source,
                  "problem_digest": self.parent.digest,
                  "stages": {stage: "passed" for stage in ("build", "runtime", "correctness", "performance")},
                  "correctness": {"passed": 39, "total": 39, "scope": "public_and_hidden_full"},
                  "measurements": measurements}
        bindings = {"package_digest": self.parent.digest, "environment": {"synthetic_test": True},
                    "reference_sha256": source, "engine_digest": "e" * 64,
                    "protocol_digest": protocol_sha256(self.parent), "expected_case_ids": ids}
        baseline = create_baseline(report, **bindings)
        child = self.child()
        with self.assertRaisesRegex(BaselineError, "binding differs: package_digest"):
            validate_baseline(baseline, **dict(bindings, package_digest=child.digest,
                              protocol_digest=protocol_sha256(child), expected_case_ids=["public_01"]))


if __name__ == "__main__":
    unittest.main()
