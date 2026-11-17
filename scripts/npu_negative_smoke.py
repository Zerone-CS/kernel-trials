"""Administrator-owned negative controls; never run in a real agent session."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.budget import Policy
from engine.controller import Controller
from workspace import export_workspace


SOURCES = {
    "compile_failure": "this is deliberately invalid C++ !!!\n",
    "wrong_output": '''#include "kernel_operator.h"
extern "C" void run_kernel(GM_ADDR, const TensorGroupInfo &,
    GM_ADDR, const TensorGroupInfo &, GM_ADDR y, const TensorGroupInfo &info_y,
    int64_t, aclrtStream, bool, bool)
{
    const size_t bytes = static_cast<size_t>(info_y.tensors[0].shape[0]) * sizeof(float);
    if (aclrtMemset((void *)y, bytes, 0, bytes) != ACL_SUCCESS)
        throw std::runtime_error("Negative-control initialization failed");
}
''',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8766")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    controller = Controller(args.state, mode="development")
    summaries = []
    for kind, source in SOURCES.items():
        credentials = controller.admin_create(ROOT / "problems/batch_matmul_reduce", track="from_scratch",
                                               owner_id="negative-control", evaluation_purpose="entry_smoke",
                                               policy=Policy(max_feedback=1, active_seconds=300))
        workspace = export_workspace(controller, credentials, output / kind, args.endpoint)
        (workspace / "kernel.asc").write_text(source)
        def call(action, *flags, expected=0):
            result = subprocess.run([sys.executable, str(workspace / "eval"), action, *flags],
                                    cwd=workspace, text=True, capture_output=True, timeout=1800)
            if result.returncode != expected:
                raise RuntimeError("Negative-control client returned unexpected exit for " + action)
            return json.loads(result.stdout)
        call("start")
        checked = call("check")
        expected_outcome = "compile_failed" if kind == "compile_failure" else "correctness_failed"
        if checked["report"]["outcome"] != expected_outcome:
            raise RuntimeError("Negative control did not hit expected gate: " + kind)
        (workspace / "kernel.asc").write_text(source + "\n// unsubmitted post-budget edit\n")
        denied = call("final", "--current", expected=1)
        if denied.get("error") != "UNSUBMITTED_FINAL_SOURCE":
            raise RuntimeError("Budget exhaustion did not freeze new source")
        final = call("final")
        report = json.loads((workspace / ".eval/report.json").read_text())
        assert final["report"]["outcome"] == expected_outcome
        assert report["result"]["score"] == 0
        assert final["source_sha256"] == checked["source_sha256"]
        assert final["session"]["feedback_used"] == 1
        assert final["report"]["stages"]["performance"] == "not_run"
        summaries.append({"fixture": kind, "session_id": credentials["session_id"], "outcome": expected_outcome,
                          "score": report["result"]["score"], "feedback_used": 1,
                          "post_budget_new_source_denied": True, "historical_source_rejudged": True,
                          "report": str(workspace / "REPORT.md")})
    (output / "summary.json").write_text(json.dumps(summaries, indent=2))
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
