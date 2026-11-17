"""Frozen research plans and independent trials on the existing judge.

This is development experiment scheduling, not model identity attestation or an
OS sandbox. Only administrators install task packages. A launcher can select
installed tasks and smaller budgets, never replace the judge or raise its cap.
"""
from dataclasses import asdict
import hashlib
import json
import re
import time
import uuid

from .budget import Policy, BudgetError
from .identity import protocol_sha256
from .launching import Launcher, LaunchError, _json
from .problems import load_problem


_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")


class Research:
    def __init__(self, launcher):
        self.launcher = launcher
        self.controller = launcher.controller
        with launcher._serialized(), launcher._connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS research_tasks (
                    launcher_id TEXT NOT NULL REFERENCES launchers(launcher_id),
                    task_id TEXT NOT NULL, child_id TEXT NOT NULL,
                    child_token TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    PRIMARY KEY(launcher_id,task_id)
                );
                CREATE TABLE IF NOT EXISTS research_experiments (
                    experiment_id TEXT PRIMARY KEY, launcher_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, input_json TEXT NOT NULL,
                    manifest_json TEXT NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(launcher_id,request_id)
                );
                CREATE TABLE IF NOT EXISTS research_trials (
                    experiment_id TEXT NOT NULL REFERENCES research_experiments(experiment_id),
                    trial_id TEXT NOT NULL, spec_json TEXT NOT NULL,
                    state TEXT NOT NULL, session_id TEXT, session_token TEXT,
                    files_json TEXT, PRIMARY KEY(experiment_id,trial_id)
                );
            ''')

    def _auth(self, db, launcher_id, token):
        self.launcher._mode()
        return self.launcher._authenticate(db, launcher_id, token)

    def _tasks(self, db, parent):
        package = load_problem(self.controller.root / 'packages' / parent['package_digest'])
        full = {'id': 'full', 'label': '完整算子', 'case_id': None,
                'capabilities': [], 'shape': None, 'scope': 'full_operator'}
        result = {'full': (parent, full)}
        for row in db.execute('SELECT * FROM research_tasks WHERE launcher_id=?', (parent['launcher_id'],)):
            child = db.execute('SELECT * FROM launchers WHERE launcher_id=?', (row['child_id'],)).fetchone()
            if child is None:
                raise ValueError('Registered task launcher is missing')
            result[row['task_id']] = (dict(child), json.loads(row['metadata_json']))
        return result

    def admin_add_task(self, launcher_id, token, task_id, package_path, metadata):
        # Serialize installation separately from allocation. admin_register()
        # takes the normal launcher lock, so nesting that flock would deadlock.
        import fcntl
        import os
        lock = self.launcher.database.with_name(self.launcher.database.name + '.task-install.lock')
        descriptor = os.open(str(lock), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(descriptor, 'r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            return self._install_task(launcher_id, token, task_id, package_path, metadata)

    def _install_task(self, launcher_id, token, task_id, package_path, metadata):
        if not _ID.fullmatch(task_id) or task_id == 'full':
            raise ValueError('Invalid child task id')
        package = load_problem(package_path)
        with self.launcher._serialized():
            with self.launcher._connection() as db:
                parent = self._auth(db, launcher_id, token)
                prior = db.execute('SELECT * FROM research_tasks WHERE launcher_id=? AND task_id=?',
                                   (launcher_id, task_id)).fetchone()
                if prior:
                    child = db.execute('SELECT * FROM launchers WHERE launcher_id=?', (prior['child_id'],)).fetchone()
                    if child['package_digest'] != package.digest or json.loads(prior['metadata_json']) != metadata:
                        raise ValueError('Installed task is immutable; use a new launcher for another task version')
                    return {'task_id': task_id, 'problem_digest': package.digest, 'reused': True}
            # admin_register uses the same serialization lock; do it outside
            # this scope to avoid nesting flock descriptors on the same file.
        child = self.launcher.admin_register(package_path, owner_id=parent['owner_id'],
                    policy=Policy(**json.loads(parent['policy_json'])),
                    evaluation_purpose=parent['evaluation_purpose'])
        with self.launcher._serialized(), self.launcher._connection() as db:
            db.execute('INSERT INTO research_tasks VALUES (?,?,?,?,?)',
                       (launcher_id, task_id, child['launcher_id'], child['token'], _json(metadata)))
        return {'task_id': task_id, 'problem_digest': package.digest, 'reused': False}

    def catalog(self, launcher_id, token):
        with self.launcher._connection() as db:
            parent = self._auth(db, launcher_id, token)
            tasks = self._tasks(db, parent)
        rows = []
        for task_id, (binding, metadata) in tasks.items():
            package = load_problem(self.controller.root / 'packages' / binding['package_digest'])
            rows.append(dict(metadata, id=task_id, problem_digest=package.digest,
                             protocol_digest=protocol_sha256(package), baseline_id=binding['baseline_id'],
                             public_case_ids=[c['case_id'] for c in package.load_adapter().cases('public', 0)]))
        return {'tasks': rows, 'policy': json.loads(parent['policy_json']),
                'evaluation_purpose': parent['evaluation_purpose']}

    def open_task(self, launcher_id, token, *, task_id, track, request_id):
        with self.launcher._connection() as db:
            self._auth(db, launcher_id, token)
            row = db.execute('SELECT * FROM research_tasks WHERE launcher_id=? AND task_id=?',
                             (launcher_id, task_id)).fetchone()
        if task_id == 'full':
            return self.launcher.open(launcher_id, token, track=track, request_id=request_id)
        if row is None:
            raise LaunchError('TASK_NOT_INSTALLED', 'Task is not installed on this launcher: ' + str(task_id))
        result = self.launcher.open(row['child_id'], row['child_token'], track=track, request_id=request_id)
        result['task_id'] = task_id
        return result

    def create(self, launcher_id, token, *, request_id, plan):
        if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
            raise ValueError('Invalid experiment request id')
        if not isinstance(plan, dict):
            raise ValueError('Experiment plan must be an object')
        with self.launcher._serialized(), self.launcher._connection() as db:
            parent = self._auth(db, launcher_id, token)
            previous = db.execute('SELECT * FROM research_experiments WHERE launcher_id=? AND request_id=?',
                                   (launcher_id, request_id)).fetchone()
            if previous:
                if previous['input_json'] != _json(plan):
                    raise LaunchError('REQUEST_CONFLICT', 'An experiment request cannot change its plan')
                return json.loads(previous['manifest_json'])
            allowed = {'name', 'track', 'tasks', 'repeats', 'control', 'conditions', 'policy'}
            if set(plan) - allowed:
                raise ValueError('Unknown experiment fields')
            track = plan.get('track', 'from_scratch')
            repeats = plan.get('repeats', 3)
            task_ids = plan.get('tasks', ['full'])
            conditions = plan.get('conditions')
            name = plan.get('name', 'operator-experiment')
            if not isinstance(name, str) or not 1 <= len(name) <= 200:
                raise ValueError('name must contain 1..200 characters')
            if track not in ('from_scratch', 'optimize') or type(repeats) is not int or not 1 <= repeats <= 100:
                raise ValueError('Invalid track or repeats (1..100)')
            if (not isinstance(task_ids, list) or not task_ids or len(task_ids) != len(set(task_ids))
                    or any(not isinstance(t, str) for t in task_ids)):
                raise ValueError('tasks must be distinct task ids')
            tasks = self._tasks(db, parent)
            if any(t not in tasks for t in task_ids):
                raise LaunchError('TASK_NOT_INSTALLED', 'One or more selected tasks are not installed')
            if not isinstance(conditions, list) or not 1 <= len(conditions) <= 8:
                raise ValueError('Provide 1..8 experiment conditions')
            seen = set()
            for condition in conditions:
                if (not isinstance(condition, dict) or set(condition) - {'id','label','method_digest','runner'}
                        or not isinstance(condition.get('id'), str) or not _ID.fullmatch(condition['id'])
                        or condition['id'] in seen or not _SHA.fullmatch(condition.get('method_digest', ''))
                        or not isinstance(condition.get('runner', {}), dict)):
                    raise ValueError('Invalid, duplicate or unversioned condition')
                seen.add(condition['id'])
            control = plan.get('control', conditions[0]['id'])
            if control not in seen or len(task_ids) * repeats * len(conditions) > 1000:
                raise ValueError('Unknown control or more than 1000 planned trials')
            cap = json.loads(parent['policy_json'])
            requested = plan.get('policy', {})
            if not isinstance(requested, dict) or set(requested) - {'max_feedback', 'active_seconds'}:
                raise ValueError('Only feedback/time budgets may be reduced')
            policy = dict(cap, **requested)
            Policy(**policy)
            if any(policy[k] > cap[k] for k in requested):
                raise ValueError('Experiment budget exceeds the administrator cap')
            experiment_id = uuid.uuid4().hex
            metadata = {}
            for task_id in task_ids:
                binding, meta = tasks[task_id]
                package = load_problem(self.controller.root / 'packages' / binding['package_digest'])
                metadata[task_id] = dict(meta, problem_digest=package.digest,
                    protocol_digest=protocol_sha256(package), baseline_id=binding['baseline_id'],
                    public_case_ids=[c['case_id'] for c in package.load_adapter().cases('public', 0)])
            trials = []
            # Alternate condition order in each repetition to reduce systematic
            # warm-up/time-order confounding; this is not randomization proof.
            for repeat in range(1, repeats + 1):
                order = conditions if repeat % 2 else list(reversed(conditions))
                for task_id in task_ids:
                    for condition in order:
                        trials.append({'trial_id': uuid.uuid4().hex, 'task_id': task_id,
                            'condition_id': condition['id'], 'repeat_index': repeat,
                            'attempt_index': len(trials) + 1})
            manifest = dict(plan, schema_version=1, experiment_id=experiment_id, name=name,
                track=track, tasks=task_ids, repeats=repeats, control=control, policy=policy,
                task_metadata=metadata, trials=trials, created_at=time.time(),
                evaluation_purpose=parent['evaluation_purpose'],
                hidden_input_policy='same task/repeat shares hidden seed across conditions; different repeats use different seeds',
                agent_identity_verified=False, security_label='not_cheat_resistant')
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('INSERT INTO research_experiments VALUES (?,?,?,?,?,?)',
                           (experiment_id, launcher_id, request_id, _json(plan), _json(manifest), time.time()))
                db.executemany('INSERT INTO research_trials (experiment_id,trial_id,spec_json,state) VALUES (?,?,?,?)',
                    [(experiment_id, t['trial_id'], _json(t), 'PLANNED') for t in trials])
                db.commit()
            except BaseException:
                db.rollback()
                raise
            return manifest

    def _experiment(self, db, launcher_id, token, experiment_id):
        self._auth(db, launcher_id, token)
        row = db.execute('SELECT * FROM research_experiments WHERE experiment_id=? AND launcher_id=?',
                         (experiment_id, launcher_id)).fetchone()
        if not row:
            raise LaunchError('EXPERIMENT_NOT_FOUND', 'Experiment is not bound to this launcher')
        return json.loads(row['manifest_json'])

    def open_trial(self, launcher_id, token, *, experiment_id, trial_id):
        with self.launcher._serialized():
            with self.launcher._connection() as db:
                manifest = self._experiment(db, launcher_id, token, experiment_id)
                trial = db.execute('SELECT * FROM research_trials WHERE experiment_id=? AND trial_id=?',
                                    (experiment_id, trial_id)).fetchone()
                if not trial:
                    raise ValueError('Trial is not part of the frozen plan')
                spec = json.loads(trial['spec_json'])
                if trial['state'] == 'ALLOCATING':
                    raise LaunchError('LAUNCH_INCOMPLETE', 'Interrupted trial allocation requires administrator inspection')
                if trial['state'] == 'PLANNED':
                    db.execute("UPDATE research_trials SET state='ALLOCATING' WHERE experiment_id=? AND trial_id=?",
                               (experiment_id, trial_id))
                    meta = manifest['task_metadata'][spec['task_id']]
                    package_path = self.controller.root / 'packages' / meta['problem_digest']
                    baseline_path = self.controller.root / 'baselines' / (meta['baseline_id'] + '.json') if meta['baseline_id'] else None
                    credentials = self.controller.admin_create(package_path, track=manifest['track'],
                        owner_id='experiment:' + experiment_id, policy=Policy(**manifest['policy']),
                        baseline_path=baseline_path, evaluation_purpose=manifest['evaluation_purpose'])
                    # Pair the hidden inputs across methods, while repetitions
                    # use different hidden inputs. Never return the seed to the
                    # candidate or serialize it into public experiment records.
                    binding_path = self.controller.root / 'bindings' / (credentials['session_id'] + '.json')
                    binding = json.loads(binding_path.read_text())
                    private_seed = json.loads((self.controller.root / 'campaign_seed.json').read_text())['hidden_seed']
                    material = _json([private_seed, experiment_id, spec['task_id'], spec['repeat_index']])
                    binding['hidden_seed'] = int(hashlib.sha256(material.encode()).hexdigest()[:15], 16)
                    temporary = binding_path.with_suffix('.tmp-' + uuid.uuid4().hex)
                    temporary.write_text(_json(binding))
                    temporary.chmod(0o600)
                    temporary.replace(binding_path)
                    condition = next(c for c in manifest['conditions'] if c['id'] == spec['condition_id'])
                    metadata = dict(spec, experiment_id=experiment_id, launcher_id=launcher_id,
                        evaluation_purpose=manifest['evaluation_purpose'], method_digest=condition['method_digest'],
                        runner_claim=condition.get('runner', {}))
                    db.execute('UPDATE sessions SET claimed_agent_metadata_json=? WHERE session_id=?',
                               (_json(metadata), credentials['session_id']))
                    db.execute('UPDATE research_trials SET session_id=?,session_token=? WHERE experiment_id=? AND trial_id=?',
                               (credentials['session_id'], credentials['token'], experiment_id, trial_id))
                    files = self.launcher._bundle(credentials)
                    db.execute("UPDATE research_trials SET state='READY',files_json=? WHERE experiment_id=? AND trial_id=?",
                               (_json(files), experiment_id, trial_id))
                    trial = db.execute('SELECT * FROM research_trials WHERE experiment_id=? AND trial_id=?',
                                       (experiment_id, trial_id)).fetchone()
                    reused = False
                else:
                    reused = True
            session = self.controller.store.status(trial['session_id'], trial['session_token'])
            return dict(spec, launcher_id=launcher_id, session_id=trial['session_id'], track=manifest['track'],
                        session=session, credentials={'session_id': trial['session_id'], 'token': trial['session_token']},
                        files=json.loads(trial['files_json']), reused=reused, experiment_id=experiment_id)

    def finish_trial(self, launcher_id, token, *, experiment_id, trial_id):
        with self.launcher._connection() as db:
            self._experiment(db, launcher_id, token, experiment_id)
            row = db.execute('SELECT * FROM research_trials WHERE experiment_id=? AND trial_id=?',
                             (experiment_id, trial_id)).fetchone()
        if not row or row['state'] != 'READY':
            raise ValueError('Trial has not been allocated')
        sid, bearer = row['session_id'], row['session_token']
        status = self.controller.store.status(sid, bearer)
        if status.get('pending_operation_id'):
            return {'state': 'WAITING', 'session': status}
        if status['state'] not in ('FINISHED', 'EXPIRED', 'CANCELLED'):
            history = self.controller.store.history(sid, bearer)['operations']
            if history:
                try:
                    self.controller.final_previous(sid, bearer, request_id='experiment-final-' + trial_id)
                except BudgetError as error:
                    if error.code not in ('FINAL_CLOSED', 'SESSION_CLOSED', 'NO_SUBMISSION', 'FINAL_ALREADY_USED', 'OPERATION_PENDING'):
                        raise
            else:
                # A late HTTP submission can race the preceding history read.
                # Only close an empty session atomically; never cancel a worker
                # that the service has already accepted.
                self.controller.store.cancel_if_no_submission(sid, bearer, reason='agent_exited_without_submission')
        status = self.controller.store.status(sid, bearer)
        if status.get('pending_operation_id') or status['state'] not in ('FINISHED', 'EXPIRED', 'CANCELLED'):
            return {'state': 'WAITING', 'session': status}
        return {'state': 'FINISHED', **self.controller.report(sid, bearer)}

    def report(self, launcher_id, token, *, experiment_id):
        from .comparison import build_comparison, render_comparison
        with self.launcher._connection() as db:
            manifest = self._experiment(db, launcher_id, token, experiment_id)
            rows = db.execute('SELECT * FROM research_trials WHERE experiment_id=?', (experiment_id,)).fetchall()
        trials = []
        for row in rows:
            item = dict(json.loads(row['spec_json']), status=row['state'])
            if row['state'] == 'READY':
                item['report'] = self.controller.report(row['session_id'], row['session_token'])['report']
                item['status'] = item['report']['session']['state']
            trials.append(item)
        comparison = build_comparison(manifest, trials)
        return {'manifest': manifest, 'trials': trials, 'report': comparison,
                'markdown': render_comparison(comparison)}
