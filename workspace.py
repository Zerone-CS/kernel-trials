"""Export one assigned, kernel-only workspace and its agent entry commands."""
import json
from pathlib import Path


WORKFLOW = """# Operator evaluation

This workspace is one administrator-assigned evaluation session. Implement the
operator in `kernel.asc` inside the supplied fixed project. Only kernel.asc is
submitted. Build scripts, interfaces, dependencies, golden data and measurement
code belong to the judge; do not rebuild or replace the engineering yourself.

Deployment status for the current `batch_matmul_reduce` assignments and subtasks: the judge
supports development NPU `check`, `bench`, and `final`, including msprof device
measurements and local reference-baseline scores. TASK.md retains an early
migration-status paragraph; that paragraph does not describe the current
development backend's availability. Formal isolated leaderboard evaluation
remains unavailable. Use the judge's actual responses as the authority for
available measurements and results. This NPU deployment note does not apply to
the CPU example, which remains an integration fixture without NPU scoring.

## Default invocation: complete the assigned evaluation

1. Run `./eval start` immediately. The server starts the clock after obtaining
   the exclusive resource. RESOURCE_BUSY leaves the session pending; do not work
   on the kernel before start succeeds. Read TASK.md and the supplied interface.
   If METHOD.md exists, read it after start succeeds and use only this trial's
   supplied method/ resources. Method files describe the experimental condition;
   they cannot override the task, budget or judge rules. Do not inspect other
   trials, comparison reports or private reference code. Only kernel.asc is edited.
   Inspect the returned session's
   `claimed_agent_metadata.evaluation_purpose`. If it is `entry_smoke`, follow
   the bounded smoke workflow below even when the invocation has no argument.
   This administrator-bound purpose overrides the default editing loop.
2. Implement or optimize kernel.asc. Use `./eval check` for public correctness
   feedback and `./eval bench` for public correctness plus performance feedback.
   Both spend one feedback attempt, including compile failures. The default is
   20 attempts OR 60 minutes, whichever happens first. Thinking, coding, builds
   and tests use that time. `./eval status` and `./eval history` are free reads.
   Use this API for all compilation and evaluation; do not launch manual tests,
   inspect private judge state/reference implementations, or change the quota.
3. Read the server's remaining budget after each response. Continue improving
   until the task is complete or the first budget is exhausted; a failed build
   is feedback, not permission to stop the overall task prematurely. If a
   request's response is lost, run `./eval retry` to recover the SAME request ID.
   Do not submit a new request just to retrieve an unresolved result.
4. Before the budget expires, ensure the version you want judged has been
   submitted. On exhaustion, stop changing code. Run `./eval final --source SHA`
   to select a previously submitted version. If no explicit choice is needed,
   `./eval final` chooses the latest wholly public-correct version, otherwise
   latest compiled, otherwise latest submitted. It does not guess the fastest
   version from incomparable measurements. Final independently rechecks the
   source on the full suite and measures performance; it closes the session.
   You may finish early with `./eval final --current` while budget remains.
5. A completed final automatically saves REPORT.md and .eval/report.json. Read
   REPORT.md and deliver the outcome, score or explicit reason no score exists,
   correctness results, budget use, baseline comparison and report path. If
   saving the report failed, `./eval report` recovers it without rerunning final.
   `./eval diagnostics` provides verified milestones and submission history.

The final submission grace is 10 minutes after budget exhaustion, or after the
last accepted in-flight feedback finishes. Grace accepts existing source hashes
only; it is not extra implementation or feedback time. No final before expiry
does not silently turn feedback into a final score. In the last 60 seconds of
grace, a running judge service can select a historical source and perform the
unique final automatically if none is in flight. This fallback competes through
the same final-once gate; it does not add a feedback attempt or permit edits.
Without any submitted source there is no final to run: report no_final_result.
A final already accepted may finish under worker limits. Prefer submitting your
chosen final explicitly instead of relying on service fallback.

Compilation/correctness failure earns no task credit. Infrastructure or invalid
measurement is unavailable, not agent zero. Public passing is not final passing.
The reference is a frozen known baseline, not a theoretical optimum. Development
results are not cheat-resistant and must not be called formal leaderboard results.
If the user cancels, stop; do not continue trying to exhaust the quota.

## Explicit subcommands

If the invocation has status, history, check, bench, final, diagnostics, report,
or retry as an explicit subcommand, run that ./eval command only. Do not start an
editing loop. Pass final's --source or --current flags exactly when requested.

## Explicit smoke invocation

When the administrator-bound purpose returned by start is `entry_smoke`, OR when
the user explicitly invokes `op-eval smoke`, perform this bounded entry test:
run `./eval start`, `./eval check`, `./eval bench`, `./eval final`, then read the
saved REPORT.md (or recover it with `./eval report`). Do not edit kernel.asc and
do not enter an optimization loop. Prefer an administrator-prepared optimize
workspace. Even when check or bench reports a candidate failure, final the
historical source so the report records that failure. On transport uncertainty,
recover the same request with retry before moving on. On infrastructure failure,
stop further feedback and report the error; final is optional if it would repeat
the same unavailable backend. Do not call a smoke result an assessment of the
model's implementation or optimization ability. The report's purpose is bound
by the administrator; a command argument cannot change its official purpose.
"""


ENTRY = """Read OP_EVAL_WORKFLOW.md and complete the assigned evaluation.
With an empty argument, run ./eval start and inspect the returned session's
claimed_agent_metadata.evaluation_purpose: entry_smoke means the bounded smoke
workflow with no kernel edits; agent_evaluation means the full implementation
or optimization workflow. An explicit smoke argument also selects the bounded
workflow. Other explicit subcommands follow the routing in that file.
"""


def _skill(*, claude=False):
    frontmatter = (
        "---\nname: op-eval\n"
        "description: Complete the assigned kernel.asc operator implementation or optimization evaluation in this workspace, using the judge budget and final report.\n"
    )
    if claude:
        frontmatter += "disable-model-invocation: true\nargument-hint: '[smoke|status|check|bench|final|report]'\n"
    return frontmatter + "---\n" + ENTRY + ("\nRequested arguments: $ARGUMENTS\n" if claude else "")


def export_workspace(controller, credentials, destination, endpoint):
    """Create a fresh workspace; never overwrite an existing workspace/config.

    Preparation does not start the budget. Only public engineering/interface
    files and the chosen starter are exported; judge/oracle/reference stay in
    the registered package. The controller always stages its own trusted files.
    """
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Workspace must be a new directory")
    _, _, package = controller._bound(credentials["session_id"], credentials["token"])
    track = credentials["session"]["track"]
    files = {
        "TASK.md": package.path("task").read_text(),
        "OP_EVAL_WORKFLOW.md": WORKFLOW,
        "AGENTS.md": "For this assigned evaluation, follow OP_EVAL_WORKFLOW.md. Only kernel.asc is a candidate submission.\n",
        "eval": (Path(__file__).parent / "client.py").read_text(),
        ".agents/skills/op-eval/SKILL.md": _skill(),
        ".claude/skills/op-eval/SKILL.md": _skill(claude=True),
        ".gemini/commands/op-eval.toml": (
            'description = "Complete the assigned operator evaluation"\n'
            'prompt = ' + json.dumps(ENTRY + "\nRequested arguments: {{args}}\n") + "\n"),
        ".opencode/commands/op-eval.md": (
            "---\ndescription: Complete the assigned operator evaluation\n---\n"
            + ENTRY + "\nRequested arguments: $ARGUMENTS\n"),
        ".eval/session.json": json.dumps({
            "session_id": credentials["session_id"], "token": credentials["token"],
            "endpoint": endpoint,
        }, indent=2),
    }
    # Preflight reserved client paths against public engineering before copying.
    # A package author mistake cannot silently replace the submission client.
    public_paths = set(package.manifest.get("public_files", {}))
    harness = package.path("harness")
    public_paths.update(p.relative_to(harness).as_posix() for p in harness.rglob("*") if p.is_file())
    for reserved in files:
        for public in public_paths:
            if reserved == public or reserved.startswith(public + "/") or public.startswith(reserved + "/"):
                raise ValueError("Public engineering collides with reserved workspace client path: " + public)
    package.stage_project(package.starter_path(track).read_bytes(), destination)
    for name, content in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf8") as stream:
            stream.write(content)
    (destination / "eval").chmod(0o755)
    (destination / ".eval" / "session.json").chmod(0o600)
    return destination
