"""Bound whole-operation duration, including trusted oracle/data preparation."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys


def run_managed(package, source, action, run_dir, *, mode="development", seed=None, baseline=None):
    run_dir = Path(run_dir).resolve()
    control = run_dir.parent / (run_dir.name + ".worker")
    control.mkdir(parents=True, exist_ok=False, mode=0o700)
    source_file = control / "kernel.asc"
    source_file.write_bytes(source)
    protocol = json.loads(package.path("protocol").read_text())
    limit = protocol.get("limits", {}).get("operation_seconds", 1800)
    if type(limit) is not int or limit <= 0:
        raise ValueError("operation_seconds must be a positive integer")
    job = dict(package=str(package.root), package_digest=package.digest, source=str(source_file),
               action=action, run_dir=str(run_dir), mode=mode, seed=seed, baseline=baseline)
    job_path, result_path = control / "job.json", control / "result.json"
    job_path.write_text(json.dumps(job))
    job_path.chmod(0o600)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "ASCEND_HOME_PATH", "PYTHONPATH")}
    env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
    timed_out = False
    with (control / "worker.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m", "engine.worker", "--job", str(job_path),
                                    "--result", str(result_path)],
                                   cwd=str(Path(__file__).resolve().parents[1]), env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            process.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        if not timed_out and process.returncode == 0 and result_path.is_file():
            result = json.loads(result_path.read_text())
            if result.get("source_sha256") != hashlib.sha256(source).hexdigest():
                raise ValueError("Worker source identity mismatch")
            result["worker"] = {"supervised": True, "operation_limit_seconds": limit}
            return result
    return {"outcome": "infrastructure_error", "action": action,
            "source_sha256": hashlib.sha256(source).hexdigest(), "problem_digest": package.digest,
            "error_category": "operation_timeout" if timed_out else "worker_process_failed",
            "stages": {k: "not_run" for k in ("build", "runtime", "correctness", "performance")},
            "score": None, "score_status": "INFRASTRUCTURE_ERROR",
            "worker": {"supervised": True, "operation_limit_seconds": limit, "timed_out": timed_out}}
