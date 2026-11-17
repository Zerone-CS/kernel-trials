"""Run controlled operator experiments in fresh CLI Agent processes.

The service freezes task, starter, budget and trial identities. This trusted
research driver supplies method artifacts, starts one fresh Agent per trial,
and obtains reports from the judge rather than trusting local candidate files.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from launch import _read, _save, _safe_path, _post, _materialize, LaunchClientError
from assigned import _atomic_write
from engine.task_catalog import resolve_tasks


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _method(condition, base):
    instructions = condition.get('instructions', '')
    if not isinstance(instructions, str):
        raise ValueError('Condition instructions must be text')
    if 'instructions_file' in condition and (not isinstance(condition['instructions_file'], str) or not condition['instructions_file']):
        raise ValueError('instructions_file must be a nonempty path')
    resources = condition.get('resources', [])
    if not isinstance(resources, list) or any(not isinstance(p, str) or not p or '\x00' in p for p in resources):
        raise ValueError('resources must be a list of file or directory paths')
    if condition.get('instructions_file'):
        instructions += '\n' + (base / condition['instructions_file']).read_text(encoding='utf8')
    files = {}
    for supplied in resources:
        source = base / supplied
        if source.is_symlink() or not source.exists():
            raise ValueError('Method resources must exist and cannot be symlinks: ' + str(source))
        paths = sorted(source.rglob('*')) if source.is_dir() else [source]
        for path in paths:
            if path.is_symlink():
                raise ValueError('Symlink in method resources: ' + str(path))
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError('Method resources must be regular files')
            name = (Path(source.name) / path.relative_to(source)).as_posix() if source.is_dir() else source.name
            if name in files:
                raise ValueError('Colliding method resource: ' + name)
            files[name] = path.read_bytes()
            if len(files) > 1000 or sum(len(v) for v in files.values()) > 16 * 1024 * 1024:
                raise ValueError('Method bundle exceeds 1000 files / 16 MiB')
    identity = {'instructions': instructions,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    return {'digest': _digest(identity), 'instructions': instructions, 'files': files}


def load_config(path):
    path = Path(path).resolve()
    config = _read(path)
    allowed = {'schema_version', 'name', 'track', 'tasks', 'repeats', 'control', 'conditions', 'runner', 'policy'}
    if set(config) - allowed or config.get('schema_version', 1) != 1:
        raise ValueError('Unknown experiment configuration fields or schema')
    conditions = config.get('conditions')
    if not isinstance(conditions, list) or not conditions:
        raise ValueError('Provide at least one condition')
    methods, runners, clean_conditions = {}, {}, []
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) - {'id','label','instructions','instructions_file','resources','runner'}:
            raise ValueError('Unknown condition fields')
        condition_id = condition['id']
        if not isinstance(condition_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', condition_id):
            raise ValueError('Condition id must be a short alphanumeric identifier')
        if condition_id in methods:
            raise ValueError('Duplicate condition id')
        method = _method(condition, path.parent)
        if not isinstance(config.get('runner', {}), dict) or not isinstance(condition.get('runner', {}), dict):
            raise ValueError('Runner configuration must be an object')
        runner = dict(config.get('runner', {'provider': 'claude'}), **condition.get('runner', {}))
        # Environment/command values can contain credentials. Only hash them in
        # the plan and reports; the local trusted driver retains the real config.
        public_runner = {'provider': runner.get('provider', 'command'), 'model_claim': runner.get('model'),
                         'configuration_digest': _digest(runner), 'identity_verified': False}
        clean_conditions.append({'id': condition_id, 'label': condition.get('label', condition_id),
                                 'method_digest': method['digest'], 'runner': public_runner})
        methods[condition_id], runners[condition_id] = method, runner
    plan = {'name': config.get('name', path.stem), 'track': config.get('track', 'from_scratch'),
            'tasks': resolve_tasks(config.get('tasks', ['full'])), 'repeats': config.get('repeats', 3),
            'control': config.get('control', clean_conditions[0]['id']), 'conditions': clean_conditions,
            'policy': config.get('policy', {})}
    return plan, methods, runners


def _install_method(workspace, method):
    record = workspace / '.eval' / 'method.json'
    text = '# Experimental method\n\n' + (method['instructions'] or 'Use your normal operator development method.')
    if method['files']:
        text += '\n\nResources available in this trial:\n' + ''.join('- method/' + n + '\n' for n in method['files'])
    text += '\nTask, submission and budget rules remain in OP_EVAL_WORKFLOW.md.\n'
    if record.exists():
        if _read(record).get('method_digest') != method['digest'] or (workspace / 'METHOD.md').read_text() != text:
            raise ValueError('Trial method changed after preparation')
        for name, data in method['files'].items():
            if _safe_path(workspace, 'method/' + name).read_bytes() != data:
                raise ValueError('Trial method resource changed: ' + name)
        directory = workspace / 'method'
        actual = {p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file()} if directory.exists() else set()
        if actual != set(method['files']):
            raise ValueError('Unexpected files in trial method resources')
        return
    (workspace / 'METHOD.md').write_text(text, encoding='utf8')
    for name, data in method['files'].items():
        destination = _safe_path(workspace, 'method/' + name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    _save(record, {'method_digest': method['digest']})


def _finish(root, settings, experiment_id, trial_id, deadline):
    while True:
        result = _post(settings, 'experiment_finish', {'experiment_id': experiment_id, 'trial_id': trial_id}, root)
        if result.get('state') != 'WAITING':
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError('Judge operation is still running; resume this experiment after it finishes')
        time.sleep(2)


def _write_reports(root, settings, directory, experiment_id):
    result = _post(settings, 'experiment_report', {'experiment_id': experiment_id}, root)
    # Add trusted-driver execution metadata separately from judge evidence.
    by_id = {trial['trial_id']: trial for trial in result['trials']}
    for trial_id, trial in by_id.items():
        record = directory / 'trials' / trial_id / 'execution.json'
        if record.is_file():
            trial['runner_metadata'] = _read(record)
            trial['report_path'] = str(directory / 'trials' / trial_id / 'REPORT.md')
    from engine.comparison import build_comparison, render_comparison
    comparison = build_comparison(result['manifest'], result['trials'])
    _save(directory / 'comparison.json', comparison)
    _save(directory / 'trials.json', result['trials'])
    _atomic_write(directory / 'REPORT.md', render_comparison(comparison).encode('utf8'))
    return {'experiment_id': experiment_id, 'report_path': str(directory / 'REPORT.md'),
            'json_path': str(directory / 'comparison.json'), 'trial_count': len(by_id),
            'completed_trials': sum(t.get('status') in ('FINISHED', 'EXPIRED', 'CANCELLED') for t in by_id.values())}


def run_experiment(project_root, config_path, *, new=False, run_agent_fn=None):
    root = Path(project_root).resolve()
    settings = _read(_safe_path(root, '.eval/launcher.json'))
    config_path = Path(config_path)
    if not config_path.is_absolute() and not config_path.exists() and (root / config_path).is_file():
        config_path = root / config_path
    config_path = config_path.resolve()
    if settings.get('experiment_remote'):
        from experiment_remote import run_remote_experiment
        return run_remote_experiment(root, config_path, settings['experiment_remote'], new=new)
    plan, methods, runners = load_config(config_path)
    # Stable config/method identity makes interruption resume the same plan.
    key = _digest({'launcher_id': settings['launcher_id'], 'plan': plan})
    registry = _safe_path(root, '.eval/experiment-' + key + '.json')
    lock = _safe_path(root, '.eval/experiment-' + key + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'r+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('This experiment is already running; inspect its report or wait') from None
        previous = _read(registry) if registry.is_file() else None
        if new and previous and previous.get('state') != 'FINISHED':
            raise ValueError('An unfinished experiment must be resumed before requesting --new')
        if previous is None or new:
            previous = {'request_id': uuid.uuid4().hex, 'state': 'PREPARING', 'plan_digest': key}
            _save(registry, previous)
        manifest = _post(settings, 'experiment_create', {'request_id': previous['request_id'], 'plan': plan}, root)
        experiment_id = manifest['experiment_id']
        # A sibling directory avoids loading this project's parent Agent prompts
        # and method bundles as normal working-directory context. Same-account
        # filesystem access is still not a hostile-Agent isolation boundary.
        directory = root.parent / 'op_eval_experiment_runs' / experiment_id
        directory.mkdir(parents=True, exist_ok=True)
        _save(directory / 'manifest.json', manifest)
        previous.update(experiment_id=experiment_id, directory=str(directory))
        _save(registry, previous)
        if previous['state'] == 'FINISHED':
            return dict(_write_reports(root, settings, directory, experiment_id), reused=True)
        if run_agent_fn is None:
            from experiment_runner import run_agent
            run_agent_fn = run_agent
        _save(registry, dict(previous, state='RUNNING'))
        for spec in manifest['trials']:
            trial_id = spec['trial_id']
            trial_dir = directory / 'trials' / trial_id
            trial_dir.mkdir(parents=True, exist_ok=True)
            response = _post(settings, 'experiment_open', {'experiment_id': experiment_id, 'trial_id': trial_id}, root)
            assigned = _materialize(trial_dir, settings, response, manifest['track'])
            workspace = Path(assigned['workspace'])
            execution_path = trial_dir / 'execution.json'
            marker = trial_dir / 'started.json'
            _install_method(workspace, methods[spec['condition_id']])
            terminal = assigned['status'] in ('FINISHED', 'EXPIRED', 'CANCELLED')
            if not terminal and not marker.exists():
                _save(marker, {'started_at': time.time(), 'driver_pid': os.getpid(), 'trial_id': trial_id})
                timeout = int(manifest['policy']['active_seconds'] + manifest['policy']['final_grace_seconds'] + 1800)
                print(json.dumps({'event': 'trial_started', **spec}, ensure_ascii=False), flush=True)
                try:
                    execution = run_agent_fn(runners[spec['condition_id']], workspace, timeout, trial_dir / 'logs')
                except KeyboardInterrupt:
                    # Built-in run_agent cleans up its process group before
                    # propagating cancellation. Preserve the interrupted trial;
                    # a later resume closes it without spawning a second Agent.
                    _save(execution_path, {'status':'interrupted', 'returncode':None,
                                          'timed_out':False, 'error':'user_interrupted'})
                    raise
                except Exception as error:
                    execution = {'status': 'driver_error', 'error': type(error).__name__,
                                 'returncode': None, 'timed_out': False}
                _save(execution_path, execution)
            elif not terminal and not execution_path.exists():
                # An interrupted driver may have left its CLI alive. Never
                # spawn a second Agent or close its session based on a guess.
                raise RuntimeError('Trial process outcome is unknown after driver interruption. Check the original process before recovering: ' + str(marker))
            final = _finish(root, settings, experiment_id, trial_id, time.monotonic() + 1900)
            if final.get('report'):
                _save(trial_dir / 'report.json', final['report'])
                _atomic_write(trial_dir / 'REPORT.md', final['markdown'].encode('utf8'))
            summary = _write_reports(root, settings, directory, experiment_id)
            print(json.dumps({'event': 'trial_finished', **spec, 'report_path': summary['report_path']}, ensure_ascii=False), flush=True)
        _save(registry, dict(previous, state='FINISHED'))
        return dict(_write_reports(root, settings, directory, experiment_id), reused=False)


def main(argv=None, project_root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', help='Experiment JSON configuration')
    parser.add_argument('--new', action='store_true', help='Start another recorded experiment after the previous one completed')
    args = parser.parse_args(argv)
    result = run_experiment(project_root or Path(__file__).resolve().parent, args.config, new=args.new)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
