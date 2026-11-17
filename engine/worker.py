"""One supervised evaluation worker. Candidate CLI cannot invoke trusted jobs."""
import argparse
import json
from pathlib import Path
import signal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    def terminate(signum, frame):
        # Unwind _execute's finally block, killing its current child group.
        raise SystemExit(124)
    signal.signal(signal.SIGTERM, terminate)
    from .problems import load_problem
    from .runner import run_candidate
    job = json.loads(Path(args.job).read_text())
    package = load_problem(job["package"], expected_digest=job["package_digest"])
    report = run_candidate(package, Path(job["source"]).read_bytes(), job["action"], job["run_dir"],
                           mode=job["mode"], seed=job.get("seed"), baseline=job.get("baseline"))
    result = Path(args.result)
    temporary = result.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False))
    temporary.replace(result)


if __name__ == "__main__":
    main()
