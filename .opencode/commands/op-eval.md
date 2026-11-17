---
description: Run an operator evaluation or configured comparison
---
Run the requested operator evaluation or configured comparison through
the project launcher. Claude Code, Gemini CLI and OpenCode invoke /op-eval;
Codex invokes $op-eval. Do not ask the user to prepare sessions or workspaces.

Choose the mode from the user's arguments, preserving their task, track and
configuration. Run commands from this project root; do not change directories
before interpreting a relative comparison configuration path.

- No arguments: run `python3 launch.py` ONCE, selecting full and from_scratch.
- One evaluation: run `python3 launch.py case06 optimize`, for example. The optional task
  is full or case01 through case15; the optional track is from_scratch or
  optimize. Either order is accepted. Omitted task means full; omitted track
  means from_scratch. Single-shape tasks allow fixed-shape specialization and
  do not measure shape generalization. Do not select a different task to obtain
  more budget within one evaluation.
- tasks: run `python3 launch.py tasks`, show the returned task catalog and availability,
  then stop. Listing tasks does not allocate an evaluation.
- compare CONFIG: run `python3 launch.py compare CONFIG` ONCE and let its experiment driver
  complete the configured trials. CONFIG is the user-supplied file path relative
  to the invocation working directory (or an absolute path); shell-quote it as
  one argument. From the outer project a configuration may be
  op_eval_v2/experiments/knowledge_ab.json; from the v2 root it may be
  experiments/knowledge_ab.json. Both entrypoints share the same v2 launcher
  state. The driver starts a fresh Agent process for each trial and writes a
  comparison REPORT.md. The current conversation must not implement the trial
  kernels itself, share solutions across trials, or create/change conditions,
  methods, task selection, runner settings or budgets to improve the results.
  The driver already emits progress. Wait synchronously for that command, or
  poll that SAME command's execution handle if the tool returns one. Do not
  start a separate Monitor, persistent `tail -f`/`tail -F`, or background log
  watcher: these can outlive completed trials and prevent the Agent CLI from
  exiting. Do not rerun the command to poll progress. If the tool starts the
  command in the background or returns a task/job handle, use THAT tool's
  wait/result mechanism on the SAME handle until it reports successful process
  completion or an explicit failure. Reading a progress/output file once is
  NOT completion. Do not send a final assistant response while the driver or
  any configured trial is still running. A progress event, existing REPORT.md,
  or trial_started message is not the command's successful result.
  On success, require the driver's final result JSON with report_path and all
  configured trials terminal; then read that final report. On explicit driver
  failure, report the failure and incomplete trials without claiming completion
  or starting another run. Once the command returns its successful result JSON,
  read the returned report_path, deliver the comparison outcome
  and report, then finish this invocation without starting any additional run
  or monitoring task. Repeating the same config in a later user invocation
  resumes the existing experiment or reads its completed report. Add --new
  only when the USER explicitly requests a separate new experiment. Never use
  --new as an error recovery step. If the driver reports an unknown process
  outcome or an active experiment, inspect/wait for that existing run and report
  the concrete condition; do not launch a duplicate Agent process.

For one evaluation, launcher JSON contains the absolute workspace, task_id,
track, session_id, attempt_index, status, reused, next_action,
pending_operation_id and report_path. The launcher requests preparation from
the service and owns request-id persistence. Never implement session creation
yourself or modify its connection, request cache, quotas, or existing runs.

Use the returned workspace as the working directory for all subsequent steps:

- PENDING or OPEN with no pending operation and next_action=run_workflow:
  read OP_EVAL_WORKFLOW.md there and execute it through the final report.
- If pending_operation_id is set or next_action=resume_operation: do not submit
  another check, bench, or final. Read ./eval status. If .eval/pending.json exists,
  ./eval retry recovers that same request; otherwise wait and poll status, then
  read ./eval history to obtain the completed feedback before continuing the
  existing workflow. A pending final is awaited, not replaced.
- FINALIZING: wait for the existing final with ./eval status. Once it completes,
  run ./eval report and read REPORT.md. Do not launch another evaluation.
- FINISHED or EXPIRED, or next_action=read_report: run ./eval report and read the
  existing report. Do not reset or replace that run within this invocation.
- A launcher error: report its concrete reason. Only if launch itself reports
  transport uncertainty before a confirmed result, retry the same launch command;
  its persisted request_id must be reused. Do not remove the request cache.

After a workspace has been returned, do not call launch again during this
evaluation to obtain another budget. Use the workspace client for recovery and
reporting. A later, new USER invocation may request a fresh run after the earlier
one has ended; service policy, not the Agent, decides that allocation. Keep its
attempt_index in the delivered result so separate runs are not merged.

Only kernel.asc may be edited in the assigned project. Its start response decides
whether the purpose is agent_evaluation or bounded entry_smoke. Keep the server's
budget and stop rules, and deliver the final report path and actual outcome.

Requested arguments: $ARGUMENTS
