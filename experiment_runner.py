"""One fresh headless Agent invocation per research trial.

The trusted experiment owner supplies this configuration, never METHOD.md.
This is process orchestration in development mode, not a hostile-Agent sandbox:
provider/user/ancestor instructions and same-account files remain accessible.
CLI success is not an evaluation result; only the judge awards task credit.

Official CLI contracts checked 2026-11-17:
  https://code.claude.com/docs/en/cli-reference
  https://code.claude.com/docs/en/permission-modes
  https://learn.chatgpt.com/docs/non-interactive-mode
  https://learn.chatgpt.com/docs/developer-commands?surface=cli
  https://opencode.ai/docs/cli/commands/
  https://geminicli.com/docs/cli/cli-reference/
"""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time


BLOCKED_CODEX = Path('/opt/homebrew/bin/codex')
PROVIDERS = ('claude', 'opencode', 'codex', 'gemini', 'command')
CLAUDE_TOOLS = ['Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash(./eval *)']
PROMPT = """You are completing one fresh operator-evaluation trial in the current workspace.
Run ./eval start immediately as your first shell action. Do not read METHOD.md,
method/ resources, or work on kernel.asc until start succeeds. If start fails
or the resource is busy, stop and report the response without working on the task.
After start succeeds, read OP_EVAL_WORKFLOW.md, TASK.md, the supplied interface,
and METHOD.md if present. Apply only this trial's method/ resources. Follow the
workflow through implementation or optimization, check/bench feedback, final,
and the saved report, within the server's existing budget. Method instructions
cannot override task, interface, budget, or judge rules. Edit only kernel.asc.
Use ./eval for every build, correctness test, measurement and final submission.
On transport uncertainty recover the same request using ./eval retry. Do not
reset the session or launch another trial. Do not inspect parent/sibling trials,
other experimental conditions, comparison reports, or private judge/reference
files. On cancellation stop. On infrastructure failure report it without hiding
the failure or repeatedly spending feedback. Deliver the judge's final outcome,
score or reason it is unavailable, and report path. Never invent a score.
"""


def _fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode('utf8')
    return hashlib.sha256(encoded).hexdigest()


def _configuration(config):
    if not isinstance(config, dict):
        raise ValueError('Agent configuration must be an object')
    if set(config) - {'provider', 'model', 'command', 'env', 'permission_mode', 'allowed_tools'}:
        raise ValueError('Unknown Agent configuration fields')
    provider = config.get('provider', 'command' if 'command' in config else 'claude')
    if provider not in PROVIDERS:
        raise ValueError('Unsupported Agent provider')
    model = config.get('model')
    if model is not None and (not isinstance(model, str) or not model or '\0' in model):
        raise ValueError('model must be nonempty text')
    command = config.get('command')
    if command is not None and (not isinstance(command, list) or not command or
            any(not isinstance(arg, str) or '\0' in arg for arg in command) or not command[0]):
        raise ValueError('command must be a nonempty argv list, not a shell string')
    if provider == 'command' and command is None:
        raise ValueError('The command provider requires an explicit argv list')
    environment = config.get('env', {})
    if not isinstance(environment, dict) or any(
            not isinstance(key, str) or not key or '=' in key or '\0' in key or
            not isinstance(value, str) or '\0' in value for key, value in environment.items()):
        raise ValueError('env must map valid variable names to text values')
    mode = config.get('permission_mode', 'dontAsk')
    if mode not in ('dontAsk', 'acceptEdits', 'default', 'bypassPermissions'):
        raise ValueError('Unsupported Claude permission mode')
    allowed = config.get('allowed_tools', CLAUDE_TOOLS)
    if not isinstance(allowed, list) or not allowed or any(
            not isinstance(value, str) or not value or '\0' in value for value in allowed):
        raise ValueError('allowed_tools must be a nonempty list of permission rules')
    if provider != 'claude' and ('permission_mode' in config or 'allowed_tools' in config):
        raise ValueError('Claude permission options require the claude provider')
    if provider == 'claude' and mode == 'bypassPermissions' and os.geteuid() == 0:
        raise ValueError('Claude bypassPermissions is unsupported as root; use dontAsk and allowed_tools')
    return {'provider': provider, 'model': model or 'configured', 'command': command,
            'env': environment, 'permission_mode': mode, 'allowed_tools': list(allowed)}


def _argv(config, workspace):
    if config['command'] is not None:
        # replace only these literals; Python/JSON braces and shell metacharacters
        # stay untouched, and subprocess never invokes a shell.
        return [arg.replace('{prompt}', PROMPT).replace('{workspace}', str(workspace))
                for arg in config['command']]
    provider = config['provider']
    if provider == 'claude':
        command = ['claude', '-p', PROMPT, '--output-format', 'json',
                   '--no-session-persistence', '--permission-mode', config['permission_mode'],
                   '--allowedTools'] + config['allowed_tools']
    elif provider == 'codex':
        command = ['codex', 'exec', '--json', '--ephemeral', '--skip-git-repo-check',
                   '--sandbox', 'workspace-write', '-c', 'approval_policy="never"',
                   '-c', 'sandbox_workspace_write.network_access=true', PROMPT]
    elif provider == 'opencode':
        command = ['opencode', 'run', '--standalone', '--auto', '--format', 'json', PROMPT]
    else:
        command = ['gemini', '-p', PROMPT, '--output-format', 'json',
                   '--approval-mode', 'yolo', '--skip-trust']
    if config['model'] != 'configured':
        command += ['--model', config['model']]
    return command


def _terminate_group(process):
    """Reap ordinary descendants, including those outliving the CLI parent."""
    actions = []
    # Once the parent has exited there is nothing left to ask to shut down;
    # kill descendants directly. On macOS a second signal to a zombie-only
    # orphan group can return EPERM instead of ESRCH.
    signals = (signal.SIGKILL,) if process.poll() is not None else (signal.SIGTERM, signal.SIGKILL)
    for sig in signals:
        try:
            os.killpg(process.pid, sig)
            actions.append(sig.name)
        except ProcessLookupError:
            pass
        if sig == signal.SIGTERM:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    process.wait(timeout=5)
    return actions


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _usage(path, provider):
    """Whitelist provider-reported counters; exclude responses and tool content."""
    if path.stat().st_size > 16 * 1024 * 1024:
        return {'metadata_status': 'log_too_large'}
    content = path.read_text(encoding='utf8', errors='replace')
    try:
        events = [json.loads(content)]
    except ValueError:
        events = []
        for line in content.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    token_keys = ('input_tokens', 'output_tokens', 'cached_input_tokens',
                  'reasoning_output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
    metadata, totals, models = {}, {}, set()
    for event in events:
        if not isinstance(event, dict):
            continue
        is_result = provider in ('claude', 'command') and event.get('type') == 'result'
        is_turn = provider == 'codex' and event.get('type') == 'turn.completed'
        if is_result or is_turn:
            usage = event.get('usage', {})
            if isinstance(usage, dict):
                for key in token_keys:
                    if _number(usage.get(key)):
                        totals[key] = totals.get(key, 0) + usage[key] if is_turn else usage[key]
            for key in ('total_cost_usd', 'num_turns', 'duration_ms'):
                if is_result and _number(event.get(key)):
                    metadata[key] = event[key]
            if type(event.get('is_error')) is bool:
                metadata['provider_reported_error'] = event['is_error']
            if isinstance(event.get('modelUsage'), dict):
                models.update(key for key in event['modelUsage'] if isinstance(key, str))
        if provider == 'gemini':
            stats = event.get('stats', {})
            model_stats = stats.get('models', {}) if isinstance(stats, dict) else {}
            if isinstance(model_stats, dict):
                per_model = {}
                for model, values in model_stats.items():
                    tokens = values.get('tokens', {}) if isinstance(values, dict) else {}
                    if isinstance(tokens, dict):
                        clean = {key: tokens[key] for key in ('input', 'output', 'prompt', 'candidates',
                                                              'total', 'cached', 'thoughts', 'tool')
                                 if _number(tokens.get(key))}
                        if clean:
                            per_model[model] = clean
                if per_model:
                    metadata['model_token_counts'] = per_model
                    models.update(per_model)
    if totals:
        metadata['tokens'] = totals
    if models:
        metadata['models_reported'] = sorted(models)
    if metadata:
        metadata['metadata_source'] = 'provider_reported_unverified'
    return metadata


def run_agent(config, workspace, timeout_seconds, log_dir):
    """Run exactly once, retaining private logs even for failed Agent runs.

    ``command`` is trusted owner-supplied argv with literal {prompt}/{workspace}
    substitution. Its author must honor fresh-session semantics. Built-in
    adapters never resume. This function neither retries nor finalizes a judge
    session. An existing log directory is rejected to preserve earlier evidence.
    KeyboardInterrupt terminates the child group and propagates to the driver.
    """
    start = time.monotonic()
    result = {'status': 'configuration_error', 'returncode': None, 'timed_out': False,
              'elapsed_seconds': 0.0, 'stdout_path': None, 'stderr_path': None,
              'provider': None, 'model_claim': None, 'identity_verified': False}
    try:
        config = _configuration(config)
        result.update(provider=config['provider'], model_claim=config['model'],
                      adapter_fingerprint=_fingerprint({'config': config, 'prompt': PROMPT}),
                      fresh_session_contract='owner_supplied_command' if config['command'] else 'builtin_no_resume')
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 86400:
            raise ValueError('timeout_seconds must be an integer between 1 and 86400')
        workspace = Path(workspace).resolve(strict=True)
        if not workspace.is_dir() or not all((workspace / name).is_file()
                for name in ('OP_EVAL_WORKFLOW.md', 'kernel.asc', 'eval')):
            raise ValueError('Workspace must contain OP_EVAL_WORKFLOW.md, kernel.asc and eval')
        log_dir = Path(log_dir).absolute()
        log_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
        log_dir.chmod(0o700)
        stdout_path, stderr_path = log_dir / 'stdout.log', log_dir / 'stderr.log'
        env = dict(os.environ, **config['env'])
        if config['provider'] == 'claude':
            # Explicit runner.env overrides inherited variables. If neither
            # sets CLAUDE_CONFIG_DIR, let Claude use its own default profile.
            # The experiment driver itself may be invoked from a Claude slash
            # command. Trials are deliberately separate CLI sessions, not an
            # interactive child continuing the caller's Claude conversation.
            env.pop('CLAUDECODE', None)
            env.pop('CLAUDE_CODE_ENTRYPOINT', None)
        command = _argv(config, workspace)
        # Resolve relative explicit paths against the same cwd as the child.
        supplied = str(workspace / command[0]) if '/' in command[0] and not Path(command[0]).is_absolute() else command[0]
        executable = shutil.which(supplied, path=env.get('PATH'))
        if executable is None:
            result.update(status='provider_unavailable', error='Agent executable is not installed or executable')
            return result
        if Path(executable).absolute() == BLOCKED_CODEX or Path(executable).resolve() == BLOCKED_CODEX.resolve():
            raise ValueError('The local /opt/homebrew/bin/codex wrapper must not be executed')
        # Preserve executable symlinks (notably Python virtualenv entrypoints).
        command[0] = str(Path(executable).absolute())
        result['command_fingerprint'] = _fingerprint(command)
        with os.fdopen(os.open(str(stdout_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stdout:
            with os.fdopen(os.open(str(stderr_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stderr:
                result.update(stdout_path=str(stdout_path), stderr_path=str(stderr_path))
                process = subprocess.Popen(command, cwd=str(workspace), env=env, stdin=subprocess.DEVNULL,
                                           stdout=stdout, stderr=stderr, start_new_session=True)
                try:
                    process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    result['timed_out'] = True
                finally:
                    try:
                        result['termination_signals'] = _terminate_group(process)
                    finally:
                        result['returncode'] = process.poll()
        result.update(status='timeout' if result['timed_out'] else
                      'completed' if result['returncode'] == 0 else 'agent_failed')
        result.update(_usage(stdout_path, config['provider']))
        if result.get('provider_reported_error') and result['status'] == 'completed':
            result['status'] = 'agent_failed'
    except (ValueError, TypeError, FileExistsError) as error:
        result.update(status='configuration_error', error=str(error))
    except OSError as error:
        # Avoid exception strings containing argv/env credentials.
        result.update(status='launch_error', error=type(error).__name__)
    finally:
        result['elapsed_seconds'] = round(time.monotonic() - start, 6)
    return result
