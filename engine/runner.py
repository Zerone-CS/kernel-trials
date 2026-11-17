"""Local DEVELOPMENT worker for trusted problem packages and untrusted kernels.

There is deliberately no OS isolation and no NPU performance implementation.
Formal mode is checked before creating a run directory or compiling anything.
Oracle/inputs occupy different directories, but development subprocesses still
share the service user's permissions; separation here is NOT confidentiality.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import secrets
import signal
import stat
import subprocess
import threading
import time
from typing import Any, Dict, Mapping, Optional, Union

from .problems import ProblemError, ProblemPackage, load_problem, validate_candidate_source
from .security import SecurityError, SecurityPolicy


_LOG_LIMIT = 256 * 1024
_PUBLIC_VERIFY_KEYS = frozenset(("passed", "status", "checked_elements", "failed_elements",
                                "max_abs_error", "checked_captures", "expected_calls", "observed_calls"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _positive_number(value: Any, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(label + " must be positive finite seconds")
    return float(value)


def _safe_relative(value: Any) -> str:
    if (not isinstance(value, str) or not value or "\\" in value
            or any(ord(c) < 32 for c in value) or value.startswith("/")
            or any(p in ("", ".", "..") for p in value.split("/"))):
        raise ValueError("Adapter artifact name must be a canonical relative path")
    return str(PurePosixPath(value))


def _execute(argv, *, cwd: Path, log: Path, timeout: float, env: Mapping[str, str]) -> Dict[str, Any]:
    """No shell, bounded captured log, process-group timeout on POSIX hosts.

    A process group cannot contain a deliberately escaped setsid child. This is
    development cleanup, not the cgroup/namespace boundary required for formal.
    """
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or "\x00" in a for a in argv):
        raise ValueError("Adapter/build command must be an argv list of strings")
    started = time.monotonic()
    try:
        process = subprocess.Popen(argv, cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    except OSError as error:
        return {"started": False, "returncode": None, "timed_out": False,
                "launch_error": type(error).__name__, "elapsed_seconds": time.monotonic() - started,
                "log": log.name, "log_truncated": False}
    result = {"started": True, "returncode": None, "timed_out": False,
              "log": log.name, "log_truncated": False}
    read_errors = []

    def drain():
        try:
            kept = 0
            with log.open("wb") as output:
                while True:
                    block = process.stdout.read(8192)
                    if not block:
                        break
                    available = max(0, _LOG_LIMIT - kept)
                    output.write(block[:available])
                    kept += min(len(block), available)
                    if len(block) > available:
                        result["log_truncated"] = True
        except OSError as error:
            read_errors.append(type(error).__name__)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
    finally:
        # Also remove ordinary forked descendants after a normal parent exit.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        reader.join(timeout=2)
        if not reader.is_alive():
            process.stdout.close()
    result["returncode"] = process.returncode
    result["elapsed_seconds"] = time.monotonic() - started
    if read_errors or reader.is_alive():
        result["capture_error"] = read_errors[0] if read_errors else "unclosed_descendant_pipe"
    return result


def _inputs_only(manifest: Mapping[str, Any], oracle_dir: Path, input_dir: Path) -> None:
    inputs, oracles = manifest.get("inputs"), manifest.get("oracle_files")
    if not isinstance(inputs, list) or not inputs or not isinstance(oracles, list) or not oracles:
        raise ValueError("Adapter must declare input and oracle artifact lists")
    names = [_safe_relative(name) for name in inputs]
    private = {_safe_relative(name) for name in oracles}
    if len(names) != len(set(names)) or private.intersection(names):
        raise ValueError("Input/oracle artifact identities overlap or repeat")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("Adapter must provide artifact identities")
    for name in names:
        source = oracle_dir / name
        if source.is_symlink() or not source.is_file() or source.resolve().is_relative_to(oracle_dir.resolve()) is False:
            raise ValueError("Input artifact is missing, symlinked or outside oracle directory")
        data = source.read_bytes()
        identity = artifacts.get(name)
        if not isinstance(identity, Mapping) or identity.get("bytes") != len(data) or identity.get("sha256") != _sha(data):
            raise ValueError("Input artifact identity mismatch")
        destination = input_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def _bounded_outputs(manifest: Mapping[str, Any], output: Path, repeats: int) -> Optional[Dict[str, Any]]:
    """Reject missing/special/oversized output before the adapter reads bytes."""
    sizes = {"float16": 2, "bfloat16": 2, "float32": 4, "float64": 8,
             "int8": 1, "uint8": 1, "int32": 4, "int64": 8}
    count, dtype = manifest.get("output_elements"), manifest.get("output_dtype")
    if type(count) is not int or count <= 0 or dtype not in sizes:
        raise ValueError("Adapter must declare bounded output elements and dtype")
    expected_bytes = count * sizes[dtype]
    if expected_bytes > 64 * 1024 * 1024:
        raise ValueError("Declared output exceeds this development backend's 64 MiB limit")
    expected = {output.name, *(output.name + ".call-" + str(i) for i in range(repeats))}
    with os.scandir(str(output.parent)) as entries:
        for entry in entries:
            if entry.name.startswith(output.name + ".call-") and entry.name not in expected:
                return {"passed": False, "status": "CAPTURE_COUNT_MISMATCH"}
    for name in expected:
        try:
            identity = (output.parent / name).lstat()
        except FileNotFoundError:
            return {"passed": False, "status": "OUTPUT_OR_CAPTURE_MISSING"}
        if not stat.S_ISREG(identity.st_mode):
            return {"passed": False, "status": "OUTPUT_SPECIAL_FILE_OR_SYMLINK"}
        if identity.st_size != expected_bytes:
            return {"passed": False, "status": "OUTPUT_SIZE_MISMATCH"}
    return None


def run_candidate(package: Union[ProblemPackage, Path, str], source: bytes,
                  action: str, run_dir: Union[Path, str], *, mode: str = "development",
                  seed: Optional[int] = None,
                  admin_evidence: Optional[Mapping[str, Any]] = None,
                  baseline: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Execute one admitted source snapshot; caller owns auth and budget reserve.

    ``package``, ``run_dir``, ``mode``, ``seed`` and ``admin_evidence`` are trusted
    service arguments, not candidate-controlled request options. Only ``source``
    and the selected authorized ``action`` originate from the candidate.
    """
    if action not in ("check", "bench", "final"):
        raise ValueError("action must be check, bench or final")
    if not isinstance(source, bytes):
        raise TypeError("source must be the immutable submitted bytes")
    if seed is not None and (type(seed) is not int or seed < 0):
        raise ValueError("seed must be an administrator-supplied nonnegative integer")
    policy = SecurityPolicy(mode)
    started = time.monotonic()
    report: Dict[str, Any] = {
        "schema_version": 1, "action": action, "source_sha256": _sha(source),
        "outcome": "infrastructure_error", "stages": {name: "not_run" for name in
                                                       ("build", "runtime", "correctness", "performance")},
        "correctness": {"passed": 0, "total": 0, "executed": 0,
                        "scope": "public_and_hidden_full" if action == "final" else "public_full"},
        "cases": [], "build_commands": [], "score": None, "score_status": "unavailable",
        "metric": None, "rankable": False, "performance_reason": "no_performance_backend",
        "diagnostics_are_score": False,
    }
    root = Path(run_dir)
    writable_report = False

    def finish(outcome: str, *, stage=None, category=None, message=None):
        report["outcome"] = outcome
        report["elapsed_seconds"] = time.monotonic() - started
        if category:
            report["error_category"] = category
            report["error"] = {"stage": stage, "category": category, "message": message or category}
        if writable_report:
            (root / "report.json").write_text(json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n")
        return report

    try:
        problem = package if isinstance(package, ProblemPackage) else load_problem(Path(package))
        report.update(problem_id=problem.problem_id, problem_digest=problem.digest)
        try:
            report["security"] = policy.enforce(
                admin_evidence, operator_profile=problem.manifest.get("security", {}).get("operator_profile", "legacy_host_untrusted"),
                problem_digest=problem.digest, backend_profile="development_subprocess")
        except SecurityError as error:
            report["security"] = error.report
            return finish("formal_unavailable", stage="security", category=error.code,
                          message="Formal isolation and measurement boundaries are not implemented")
        problem.assert_unchanged()
        protocol = json.loads(problem.path("protocol").read_text())
        if protocol.get("platform") == "ascend_npu":
            from .npu import run_npu_candidate
            return run_npu_candidate(problem, source, action, run_dir, mode=mode,
                                     seed=seed, admin_evidence=admin_evidence, baseline=baseline)
        if protocol.get("platform") != "cpu_smoke_only":
            return finish("infrastructure_error", stage="setup", category="backend_platform_unsupported",
                          message="This development worker only supports the CPU integration fixture")
        if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
            return finish("infrastructure_error", stage="setup", category="run_directory_not_empty")
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()
        writable_report = True
        for name in ("build", "home", "tmp", "judge", "candidate"):
            (root / name).mkdir()
        report["score_status"] = "disabled_by_problem" if protocol.get("score", {}).get("enabled") is False else "unsupported_by_backend"
        report["performance_reason"] = "CPU smoke correctness only; no competitive timing or score"
        limits = protocol.get("limits", {})
        build_limit = _positive_number(limits.get("build_seconds", 60), "build_seconds")
        case_limit = _positive_number(limits.get("case_seconds", 10), "case_seconds")
        repeats = protocol.get("correctness", {}).get("repeats", 1)
        if type(repeats) is not int or not 1 <= repeats <= 1000:
            raise ValueError("Invalid correctness repeat count")
        try:
            validate_candidate_source(source, problem.manifest["candidate_policy"])
        except ProblemError as error:
            report["stages"]["build"] = "failed"
            return finish("compile_failed", stage="submission", category="candidate_source_rejected", message=str(error))
        problem.stage_project(source, root / "project")
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
               "HOME": str(root / "home"), "TMPDIR": str(root / "tmp")}
        for index, command in enumerate(problem.build_commands(root / "project", root / "build")):
            execution = _execute(command, cwd=root / "project", log=root / ("build-" + str(index) + ".log"),
                                 timeout=build_limit, env=env)
            report["build_commands"].append(execution)
            if not execution["started"] or execution.get("capture_error"):
                return finish("infrastructure_error", stage="build", category="build_tool_unavailable_or_capture_failed")
            if execution["returncode"] != 0 or execution["timed_out"]:
                report["stages"]["build"] = "failed"
                report["build_diagnostics"] = (root / execution["log"]).read_text(errors="replace")[-16000:].replace(str(root), "<run>")
                return finish("compile_failed", stage="build", category="build_timeout" if execution["timed_out"] else "compiler_exit_nonzero")
        binary = problem.runner_path(root / "build")
        if binary.is_symlink() or not binary.is_file() or not os.access(str(binary), os.X_OK):
            return finish("infrastructure_error", stage="build", category="build_contract_missing_executable")
        report["stages"]["build"] = "passed"
        report["binary_sha256"] = _sha(binary.read_bytes())
        problem.assert_unchanged()
        adapter = problem.load_adapter()
        specs = adapter.cases("all" if action == "final" else "public", seed if seed is not None else secrets.randbits(63))
        if not isinstance(specs, list) or not specs or any(not isinstance(spec, dict) for spec in specs):
            raise ValueError("Adapter returned no valid case suite")
        report["correctness"]["total"] = len(specs)
        runtime_failed = correctness_failed = False
        for index, spec in enumerate(specs):
            case_dir = root / "judge" / str(index)
            candidate_dir = root / "candidate" / str(index)
            input_dir = candidate_dir / "inputs"
            input_dir.mkdir(parents=True)
            manifest = adapter.prepare(spec, case_dir)
            _inputs_only(manifest, case_dir, input_dir)
            output = candidate_dir / "output.bin"
            args = adapter.runner_args(spec, input_dir, output, mode="correctness", repeat=repeats, warmup=0)
            if not isinstance(args, list):
                raise ValueError("runner_args must return a list")
            execution = _execute([str(binary), *args], cwd=candidate_dir,
                                 log=candidate_dir / "runtime.log", timeout=case_limit, env=env)
            hidden = spec.get("suite") == "hidden"
            item = {"case_id": "hidden_" + str(index) if hidden else spec.get("case_id", "public_" + str(index)),
                    "suite": "hidden" if hidden else "public", "runtime": execution,
                    "correctness": {"status": "NOT_RUN", "passed": False}}
            report["cases"].append(item)
            if not execution["started"] or execution.get("capture_error"):
                return finish("infrastructure_error", stage="runtime", category="runner_launch_or_capture_failed")
            if execution["returncode"] != 0 or execution["timed_out"]:
                runtime_failed = True
                item["error_category"] = "runtime_timeout" if execution["timed_out"] else "runner_exit_nonzero"
                if not hidden:
                    item["runtime_diagnostics"] = (candidate_dir / "runtime.log").read_text(errors="replace")[-8000:].replace(str(root), "<run>")
                continue
            verification = _bounded_outputs(manifest, output, repeats)
            if verification is None:
                verification = adapter.verify(spec, case_dir, output, expected_calls=repeats)
            if not isinstance(verification, dict) or type(verification.get("passed")) is not bool:
                raise ValueError("Verifier returned an invalid result")
            if hidden:
                item["correctness"] = {"passed": verification["passed"], "status": "PASS" if verification["passed"] else "FAIL"}
            else:
                item["correctness"] = {key: value for key, value in verification.items() if key in _PUBLIC_VERIFY_KEYS}
            report["correctness"]["executed"] += 1
            if verification["passed"]:
                report["correctness"]["passed"] += 1
            else:
                correctness_failed = True
        problem.assert_unchanged()
        report["stages"]["runtime"] = "failed" if runtime_failed else "passed"
        report["stages"]["correctness"] = ("failed" if correctness_failed else
                                               "passed" if not runtime_failed else
                                               "incomplete" if report["correctness"]["executed"] else "not_run")
        if runtime_failed:
            return finish("runtime_failed", stage="runtime", category="one_or_more_cases_failed_to_execute")
        if correctness_failed:
            return finish("correctness_failed", stage="correctness", category="independent_oracle_mismatch")
        return finish("passed")
    except Exception as error:
        # Trusted adapter/config/environment exceptions are not candidate scores.
        # Avoid echoing oracle data or hidden paths from arbitrary adapter text.
        return finish("infrastructure_error", stage="judge", category="trusted_judge_or_environment_error",
                      message=type(error).__name__)

