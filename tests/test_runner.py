"""Real CPU builds verify the worker/oracle boundary and honest failure labels."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from engine.problems import load_problem
from engine.runner import _bounded_outputs, run_candidate


FIXTURE = Path(__file__).resolve().parents[1] / "problems" / "vector_add_cpu_example"
GOOD = (FIXTURE / "starters" / "optimize" / "kernel.asc").read_bytes()
WRONG = b"void run_kernel(const float*, const float*, float* y, std::size_t n) { for (std::size_t i=0;i<n;++i) y[i]=0; }"


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = load_problem(FIXTURE)

    def run_kernel(self, source=GOOD, action="check", **kwargs):
        return run_candidate(self.package, source, action, self.root / "run", seed=123, **kwargs)

    def altered_package(self, mutate):
        target = self.root / "package"
        shutil.copytree(FIXTURE, target)
        mutate(target)
        self.package = load_problem(target)

    def test_formal_guard_runs_before_any_compile_or_directory_creation(self):
        with patch("engine.runner._execute", side_effect=AssertionError("must never execute")):
            report = self.run_kernel(mode="formal")
        self.assertEqual(report["outcome"], "formal_unavailable")
        self.assertFalse((self.root / "run").exists())
        self.assertEqual(set(report["stages"].values()), {"not_run"})

    @unittest.skipUnless(shutil.which("c++"), "C++ compiler required")
    def test_public_correct_candidate_builds_and_passes_independent_oracle(self):
        report = self.run_kernel()
        self.assertEqual(report["outcome"], "passed", report)
        self.assertEqual(report["correctness"], {"passed": 2, "total": 2, "executed": 2, "scope": "public_full"})
        self.assertTrue(all(c["correctness"]["checked_captures"] == 4 for c in report["cases"]))
        self.assertEqual(report["stages"]["performance"], "not_run")
        self.assertEqual(report["security"]["security_label"], "not_cheat_resistant")
        self.assertFalse(report["rankable"])
        self.assertIsNone(report["score"])
        self.assertEqual((self.root / "run/project/kernel.asc").read_bytes(), GOOD)
        self.assertEqual(json.loads((self.root / "run/report.json").read_text()), report)
        for directory in (self.root / "run/candidate").glob("*/inputs"):
            self.assertEqual({p.name for p in directory.iterdir()}, {"input.bin"})
            self.assertFalse((directory / "golden.bin").exists())

    @unittest.skipUnless(shutil.which("c++"), "C++ compiler required")
    def test_final_includes_hidden_cases_without_leaking_their_shapes_or_errors(self):
        report = self.run_kernel(action="final")
        self.assertEqual(report["outcome"], "passed", report)
        self.assertEqual(report["correctness"]["total"], 4)
        self.assertEqual(report["correctness"]["scope"], "public_and_hidden_full")
        hidden = [c for c in report["cases"] if c["suite"] == "hidden"]
        self.assertEqual(len(hidden), 2)
        self.assertTrue(all(set(c["correctness"]) == {"passed", "status"} for c in hidden))
        self.assertNotIn('"seed"', json.dumps(report))
        self.assertNotIn('"golden"', json.dumps(report))

    @unittest.skipUnless(shutil.which("c++"), "C++ compiler required")
    def test_real_compile_failure_has_no_runtime_or_fabricated_score(self):
        report = self.run_kernel(b"this is invalid C++ !!!\n")
        self.assertEqual(report["outcome"], "compile_failed", report)
        self.assertEqual(report["stages"]["build"], "failed")
        self.assertEqual(report["stages"]["runtime"], "not_run")
        self.assertEqual(report["error"]["category"], "compiler_exit_nonzero")
        self.assertTrue(report["build_diagnostics"])
        self.assertIsNone(report["score"])

    @unittest.skipUnless(shutil.which("c++"), "C++ compiler required")
    def test_wrong_finite_output_is_correctness_failure(self):
        report = self.run_kernel(WRONG)
        self.assertEqual(report["outcome"], "correctness_failed", report)
        self.assertEqual(report["stages"]["runtime"], "passed")
        self.assertEqual(report["correctness"]["passed"], 0)
        self.assertEqual(report["correctness"]["executed"], 2)

    @unittest.skipUnless(shutil.which("c++"), "C++ compiler required")
    def test_early_wrong_call_is_not_hidden_by_correct_final_call(self):
        source = b"void run_kernel(const float* x,const float* z,float* y,std::size_t n){static int count=0;++count;for(std::size_t i=0;i<n;++i)y[i]=count==1 ? 0 : x[i]+z[i];}"
        report = self.run_kernel(source)
        self.assertEqual(report["outcome"], "correctness_failed", report)
        self.assertEqual(report["correctness"]["passed"], 0)

    @unittest.skipUnless(shutil.which("c++"), "C++ compiler required")
    def test_runtime_timeout_is_candidate_failure_and_stage_is_explicit(self):
        def change(root):
            path = root / "protocol.json"
            protocol = json.loads(path.read_text())
            protocol["limits"]["case_seconds"] = 0.05
            path.write_text(json.dumps(protocol))
        self.altered_package(change)
        source = b"void run_kernel(const float*,const float*,float*,std::size_t){volatile unsigned x=0;for(;;){++x;}}"
        report = self.run_kernel(source)
        self.assertEqual(report["outcome"], "runtime_failed", report)
        self.assertTrue(all(c["runtime"]["timed_out"] for c in report["cases"]))
        self.assertEqual(report["correctness"]["executed"], 0)

    def test_missing_compiler_is_infrastructure_error_not_agent_failure(self):
        def change(root):
            path = root / "problem.json"
            manifest = json.loads(path.read_text())
            manifest["build"]["commands"][0][0] = "nonexistent-compiler-op-eval-v2"
            path.write_text(json.dumps(manifest))
        self.altered_package(change)
        report = self.run_kernel()
        self.assertEqual(report["outcome"], "infrastructure_error")
        self.assertEqual(report["error"]["stage"], "build")
        self.assertNotEqual(report["stages"]["build"], "failed")

    def test_non_cpu_package_is_refused_before_execution(self):
        def change(root):
            path = root / "protocol.json"
            protocol = json.loads(path.read_text())
            protocol["platform"] = "ascend"
            path.write_text(json.dumps(protocol))
        self.altered_package(change)
        with patch("engine.runner._execute", side_effect=AssertionError("must never execute")):
            report = self.run_kernel()
        self.assertEqual(report["error"]["category"], "backend_platform_unsupported")
        self.assertEqual(report["outcome"], "infrastructure_error")

    def test_forbidden_include_is_rejected_without_compiler_invocation(self):
        with patch("engine.runner._execute", side_effect=AssertionError("must never execute")):
            report = self.run_kernel(b'#include "/private/golden.bin"\n')
        self.assertEqual(report["outcome"], "compile_failed")
        self.assertEqual(report["error"]["category"], "candidate_source_rejected")

    def test_existing_run_directory_is_not_overwritten(self):
        path = self.root / "run"
        path.mkdir()
        (path / "keep.txt").write_text("existing artifact")
        report = self.run_kernel()
        self.assertEqual(report["outcome"], "infrastructure_error")
        self.assertEqual((path / "keep.txt").read_text(), "existing artifact")
        self.assertFalse((path / "report.json").exists())

    def test_oversized_and_symlink_outputs_are_rejected_without_reading_bytes(self):
        output = self.root / "output.bin"
        with output.open("wb") as stream:
            stream.truncate(1024 * 1024 * 1024)
        manifest = {"output_dtype": "float32", "output_elements": 1}
        with patch.object(Path, "read_bytes", side_effect=AssertionError("must not read oversized output")):
            result = _bounded_outputs(manifest, output, 0)
        self.assertEqual(result["status"], "OUTPUT_SIZE_MISMATCH")
        output.unlink()
        output.symlink_to(self.root / "private-golden.bin")
        self.assertEqual(_bounded_outputs(manifest, output, 0)["status"], "OUTPUT_SPECIAL_FILE_OR_SYMLINK")

    @unittest.skipUnless(shutil.which("c++"), "C++ compiler required")
    def test_public_runtime_diagnostics_and_top_level_error_category(self):
        source = b'void run_kernel(const float*,const float*,float*,std::size_t){throw std::runtime_error("not implemented");}'
        report = self.run_kernel(source)
        self.assertEqual(report["outcome"], "runtime_failed", report)
        self.assertIn("error_category", report)
        self.assertTrue(all(c["runtime_diagnostics"] for c in report["cases"]))

    @unittest.skipUnless(shutil.which("c++"), "C++ compiler required")
    def test_trusted_adapter_exception_is_invalid_and_does_not_leak_oracle_text(self):
        def change(root):
            path = root / "judge.py"
            with path.open("a") as stream:
                stream.write('\ndef prepare(spec, outdir):\n    raise AttributeError("SECRET_ORACLE_DATA")\n')
        self.altered_package(change)
        report = self.run_kernel()
        self.assertEqual(report["outcome"], "infrastructure_error", report)
        self.assertNotIn("SECRET_ORACLE_DATA", json.dumps(report))

    def test_build_timeout_uses_fixed_argv_and_is_compile_failure(self):
        def change(root):
            path = root / "problem.json"
            manifest = json.loads(path.read_text())
            manifest["build"]["commands"] = [[sys.executable, "-c", "import time; time.sleep(10)"]]
            path.write_text(json.dumps(manifest))
            path = root / "protocol.json"
            protocol = json.loads(path.read_text())
            protocol["limits"]["build_seconds"] = 0.05
            path.write_text(json.dumps(protocol))
        self.altered_package(change)
        report = self.run_kernel()
        self.assertEqual(report["outcome"], "compile_failed", report)
        self.assertEqual(report["error"]["category"], "build_timeout")


if __name__ == "__main__":
    unittest.main()
