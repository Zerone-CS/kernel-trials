import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from engine.budget import Policy, BudgetError
from engine.controller import Controller
from engine.launching import Launcher, LaunchError
from engine.research import Research
from experiment import run_experiment, load_config
from launch import _save, open_workspace
from service import make_server


PACKAGE = Path(__file__).resolve().parents[1] / 'problems/vector_add_cpu_example'


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.controller = Controller(self.root / 'judge')
        self.launcher = Launcher(self.controller)
        self.grant = self.launcher.admin_register(PACKAGE, owner_id='research-test',
            policy=Policy(max_feedback=2, active_seconds=60), evaluation_purpose='entry_smoke')
        self.research = Research(self.launcher)
        self.server = make_server(self.controller, ('127.0.0.1', 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.settings = dict(launcher_id=self.grant['launcher_id'], token=self.grant['token'],
            endpoint='http://127.0.0.1:' + str(self.server.server_address[1]))
        _save(self.project / '.eval/launcher.json', self.settings)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def plan(self):
        return {'name': 'CPU integration only', 'track': 'optimize', 'tasks': ['full'], 'repeats': 2,
            'control': 'baseline', 'conditions': [
                {'id': 'baseline', 'method_digest': 'a' * 64, 'runner': {}},
                {'id': 'method', 'method_digest': 'b' * 64, 'runner': {}}],
            'policy': {'max_feedback': 1, 'active_seconds': 30}}

    def test_frozen_plan_idempotence_identity_and_caps(self):
        args = self.settings
        plan = self.plan()
        first = self.research.create(args['launcher_id'], args['token'], request_id='request1', plan=plan)
        self.assertEqual(first, self.research.create(args['launcher_id'], args['token'], request_id='request1', plan=plan))
        self.assertEqual([t['condition_id'] for t in first['trials']], ['baseline','method','method','baseline'])
        self.assertEqual([t['repeat_index'] for t in first['trials']], [1,1,2,2])
        with self.assertRaises(LaunchError):
            self.research.create(args['launcher_id'], args['token'], request_id='request1', plan=dict(plan, repeats=3))
        for policy in ({'max_feedback': 3}, {'active_seconds': 61}, {'final_grace_seconds': 999}):
            with self.assertRaises(ValueError):
                self.research.create(args['launcher_id'], args['token'], request_id='invalid', plan=dict(plan, policy=policy))
        with self.assertRaises(LaunchError):
            self.research.create(args['launcher_id'], 'wrong', request_id='other', plan=plan)
        t = first['trials'][0]
        one = self.research.open_trial(args['launcher_id'], args['token'], experiment_id=first['experiment_id'], trial_id=t['trial_id'])
        two = self.research.open_trial(args['launcher_id'], args['token'], experiment_id=first['experiment_id'], trial_id=t['trial_id'])
        self.assertEqual(one['session_id'], two['session_id'])
        self.assertEqual(one['session']['policy']['max_feedback'], 1)
        final = self.research.finish_trial(args['launcher_id'], args['token'], experiment_id=first['experiment_id'], trial_id=t['trial_id'])
        self.assertEqual(final['report']['result']['outcome'], 'no_final_result')
        self.assertIsNone(final['report']['result']['score'])
        self.assertEqual(final['report']['session']['claimed_agent_metadata']['method_digest'], 'a' * 64)
        summary = self.research.report(args['launcher_id'], args['token'], experiment_id=first['experiment_id'])
        self.assertEqual(len(summary['trials']), 4)
        self.assertNotIn(args['token'], json.dumps(summary))

    def test_task_selection_does_not_reuse_full_session(self):
        self.research.admin_add_task(self.grant['launcher_id'], self.grant['token'], 'case08', PACKAGE,
            {'id':'case08','label':'CPU transport fixture','capabilities':['tails'],'scope':'fixed_shape'})
        full = open_workspace(self.project, 'optimize')
        child = open_workspace(self.project, 'optimize', 'case08')
        resumed = open_workspace(self.project, 'optimize', 'case08')
        self.assertNotEqual(full['session_id'], child['session_id'])
        self.assertEqual(child['session_id'], resumed['session_id'])
        self.assertEqual(child['budget']['max_feedback'], 2)
        self.assertEqual(len(self.research.catalog(self.grant['launcher_id'], self.grant['token'])['tasks']), 2)
        with self.assertRaises(ValueError):
            self.research.admin_add_task(self.grant['launcher_id'], self.grant['token'], 'case08', PACKAGE, {'id':'changed'})

    def test_two_conditions_repeated_real_cpu_feedback_final_and_resume(self):
        resource = self.root / 'notes.txt'
        resource.write_text('Method resource only for treatment')
        config = self.root / 'experiment.json'
        config.write_text(json.dumps({'name':'CPU integration only','track':'optimize','tasks':['full'],
            'repeats':2,'policy':{'max_feedback':1,'active_seconds':30},
            'conditions':[{'id':'baseline'}, {'id':'method','resources':['notes.txt'], 'instructions':'Read the supplied notes.'}]}))
        visits = []
        def driver(runner, workspace, timeout, logs):
            visits.append(str(workspace))
            self.assertFalse((workspace / 'REPORT.md').exists())
            if (workspace / 'method/notes.txt').exists():
                self.assertIn('Read the supplied notes.', (workspace / 'METHOD.md').read_text())
            for action in ('start','check'):
                completed = subprocess.run([sys.executable, str(workspace/'eval'), action], cwd=workspace,
                    text=True, capture_output=True, timeout=20)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            # Leave final to the controller to verify automatic historical-source finalization.
            return {'returncode':0,'timed_out':False,'elapsed_seconds':1,'provider':'test_fixture'}
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_experiment(self.project, config, run_agent_fn=driver)
        self.assertEqual(result['completed_trials'], 4)
        self.assertEqual(len(set(visits)), 4)
        trials = json.loads((Path(result['report_path']).parent / 'trials.json').read_text())
        self.assertEqual([t['report']['result']['outcome'] for t in trials], ['passed'] * 4)
        self.assertTrue(all(t['report']['budget']['feedback_used'] == 1 for t in trials))
        self.assertTrue(all(t['report']['result']['score'] is None for t in trials))
        self.assertTrue(all(t['report']['session']['evaluation_purpose'] == 'entry_smoke' for t in trials))
        seeds = {}
        for trial in trials:
            sid = trial['report']['session']['session_id']
            seed = json.loads((self.controller.root / 'bindings' / (sid + '.json')).read_text())['hidden_seed']
            seeds.setdefault(trial['repeat_index'], set()).add(seed)
        self.assertEqual([len(values) for values in seeds.values()], [1, 1])
        self.assertNotEqual(seeds[1], seeds[2])
        with contextlib.redirect_stdout(io.StringIO()):
            repeated = run_experiment(self.project, config, run_agent_fn=driver)
        self.assertEqual(repeated['experiment_id'], result['experiment_id'])
        self.assertTrue(repeated['reused'])
        self.assertEqual(len(visits), 4)
        self.assertNotIn(self.grant['token'], Path(result['json_path']).read_text())

    def test_method_contents_and_runner_configuration_are_fingerprinted(self):
        resource = self.root / 'notes.txt'
        resource.write_text('first revision')
        config = self.root / 'experiment.json'
        config.write_text(json.dumps({'conditions':[{'id':'method','resources':['notes.txt']}],
            'runner':{'provider':'command','command':['python3','fake.py'],'env':{'SECRET':'sensitive'}}}))
        first, _, _ = load_config(config)
        self.assertNotIn('sensitive', json.dumps(first))
        resource.write_text('second revision')
        second, _, _ = load_config(config)
        self.assertNotEqual(first['conditions'][0]['method_digest'], second['conditions'][0]['method_digest'])

    def test_interrupted_agent_resumes_without_second_process(self):
        config = self.root / 'interrupted.json'
        config.write_text(json.dumps({'track':'optimize','tasks':['full'], 'repeats':1,
            'conditions':[{'id':'baseline'}]}))
        calls = []
        def interrupted(*args):
            calls.append(args)
            raise KeyboardInterrupt()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            run_experiment(self.project, config, run_agent_fn=interrupted)
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_experiment(self.project, config, run_agent_fn=interrupted)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['completed_trials'], 1)
        trials = json.loads((Path(result['report_path']).parent / 'trials.json').read_text())
        self.assertEqual(trials[0]['report']['result']['outcome'], 'no_final_result')
        self.assertIsNone(trials[0]['report']['result']['score'])
        self.assertEqual(trials[0]['runner_metadata']['status'], 'interrupted')

    def test_finish_waits_when_feedback_is_admitted_after_status_read(self):
        plan = self.plan()
        plan['policy']['max_feedback'] = 2
        manifest = self.research.create(self.grant['launcher_id'], self.grant['token'],
                                       request_id='finish-race', plan=plan)
        trial_id = manifest['trials'][0]['trial_id']
        opened = self.research.open_trial(self.grant['launcher_id'], self.grant['token'],
            experiment_id=manifest['experiment_id'], trial_id=trial_id)
        sid, token = opened['session_id'], opened['credentials']['token']
        self.controller.start(sid, token)
        self.controller.submit(sid, token, request_id='first', action='check',
            source=(PACKAGE/'starters/optimize/kernel.asc').read_text())
        def late_submission(*args, **kwargs):
            self.controller.store.reserve(sid, token, request_id='late', action='check', source_sha256='a'*64)
            raise BudgetError('OPERATION_PENDING', 'Late feedback was admitted')
        with patch.object(self.controller, 'final_previous', side_effect=late_submission):
            result = self.research.finish_trial(self.grant['launcher_id'], self.grant['token'],
                experiment_id=manifest['experiment_id'], trial_id=trial_id)
        self.assertEqual(result['state'], 'WAITING')
        self.assertTrue(result['session']['pending_operation_id'])
        self.assertEqual(result['session']['state'], 'OPEN')

    def test_concurrent_trial_open_and_crash_do_not_allocate_again(self):
        manifest = self.research.create(self.grant['launcher_id'], self.grant['token'],
                                       request_id='concurrent', plan=self.plan())
        spec = manifest['trials'][0]
        def open_trial():
            return self.research.open_trial(self.grant['launcher_id'], self.grant['token'],
                experiment_id=manifest['experiment_id'], trial_id=spec['trial_id'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: open_trial(), range(2)))
        self.assertEqual(responses[0]['session_id'], responses[1]['session_id'])
        other = manifest['trials'][1]
        with patch.object(self.launcher, '_bundle', side_effect=RuntimeError('export crash')):
            with self.assertRaises(RuntimeError):
                self.research.open_trial(self.grant['launcher_id'], self.grant['token'],
                    experiment_id=manifest['experiment_id'], trial_id=other['trial_id'])
        with self.assertRaises(LaunchError) as raised:
            self.research.open_trial(self.grant['launcher_id'], self.grant['token'],
                experiment_id=manifest['experiment_id'], trial_id=other['trial_id'])
        self.assertEqual(raised.exception.code, 'LAUNCH_INCOMPLETE')

    def test_concurrent_task_install_is_idempotent(self):
        def install():
            return self.research.admin_add_task(self.grant['launcher_id'], self.grant['token'], 'case08',
                                               PACKAGE, {'id':'case08','capabilities':['tails']})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: install(), range(2)))
        self.assertEqual(sum(r['reused'] is False for r in results), 1)
        with self.launcher._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM launchers').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
