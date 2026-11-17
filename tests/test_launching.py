import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from engine.budget import Policy
from engine.controller import Controller
from engine.launching import Launcher, LaunchError
from service import make_server


PACKAGE = Path(__file__).resolve().parents[1] / 'problems' / 'vector_add_cpu_example'


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.controller = Controller(self.root / 'state', clock=lambda: self.now)
        self.launcher = Launcher(self.controller)
        self.config = self.launcher.admin_register(PACKAGE, owner_id='trusted-test-admin',
                                                   policy=Policy(max_feedback=2), evaluation_purpose='entry_smoke')

    def open(self, request='r1', track='from_scratch', token=None):
        return self.launcher.open(self.config['launcher_id'], token or self.config['token'],
                                  track=track, request_id=request)

    def history(self):
        return self.launcher.history(self.config['launcher_id'], self.config['token'])

    def finish(self, opened, *, score=0):
        credentials = opened['credentials']
        self.controller.start(credentials['session_id'], credentials['token'])
        operation = self.controller.store.reserve(credentials['session_id'], credentials['token'],
                                                   request_id='final', action='final', source_sha256='a' * 64)
        self.controller.store.complete_operation(operation['operation_id'],
                                                  {'outcome': 'compile_failed', 'score': score})

    def denied(self, code, function, *args, **kwargs):
        with self.assertRaises(LaunchError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_registration_does_not_mint_or_start_session_and_hashes_launcher_token(self):
        with sqlite3.connect(str(self.controller.store.db_path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
            token_hash = db.execute('SELECT token_hash FROM launchers').fetchone()[0]
            self.assertEqual(token_hash, hashlib.sha256(self.config['token'].encode()).hexdigest())
        self.assertEqual(self.history()['attempts'], [])
        self.assertEqual(self.config['tracks'], ['from_scratch', 'optimize'])

    def test_open_exports_only_public_files_and_freezes_metadata_policy(self):
        opened = self.open()
        self.assertFalse(opened['reused'])
        self.assertEqual(opened['attempt_index'], 1)
        self.assertEqual(opened['session']['state'], 'PENDING')
        self.assertIsNone(opened['session']['started_at'])
        self.assertEqual(opened['session']['policy']['max_feedback'], 2)
        self.assertEqual(opened['session']['claimed_agent_metadata']['evaluation_purpose'], 'entry_smoke')
        self.assertEqual(opened['session']['claimed_agent_metadata']['launcher_id'], self.config['launcher_id'])
        self.assertEqual(opened['session']['claimed_agent_metadata']['attempt_index'], 1)
        files = {file['path']: file for file in opened['files']}
        self.assertIn('kernel.asc', files)
        self.assertIn('.claude/skills/op-eval/SKILL.md', files)
        self.assertTrue(files['eval']['executable'])
        for private in ('.eval/session.json', 'judge.py', 'cases.json', 'protocol.json', 'reference/kernel.asc'):
            self.assertNotIn(private, files)
        for file in files.values():
            data = base64.b64decode(file['content_b64'], validate=True)
            self.assertEqual(hashlib.sha256(data).hexdigest(), file['sha256'])
            self.assertNotIn(opened['credentials']['token'].encode(), data)

    def test_active_reuse_never_resets_start_clock_or_feedback(self):
        first = self.open()
        c = first['credentials']
        started = self.controller.start(c['session_id'], c['token'])
        reserved = self.controller.store.reserve(c['session_id'], c['token'], request_id='compile',
                                                 action='check', source_sha256='a' * 64)
        self.controller.store.complete_operation(reserved['operation_id'], {'outcome': 'compile_failed'})
        self.now += 45
        again = self.open('another-random-request')
        self.assertTrue(again['reused'])
        self.assertEqual(first['credentials'], again['credentials'])
        self.assertEqual(again['session']['feedback_used'], 1)
        self.assertEqual(again['session']['started_at'], started['started_at'])
        self.assertEqual(again['files'], first['files'])
        self.assertEqual(len(self.history()['attempts']), 1)

    def test_historical_request_precedes_terminal_new_attempt(self):
        first = self.open()
        self.finish(first)
        second = self.open('second-attempt')
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertEqual(second['attempt_index'], 2)
        replay = self.open('r1')
        self.assertEqual(replay['session_id'], first['session_id'])
        self.assertEqual(replay['session']['state'], 'FINISHED')
        history = self.history()
        self.assertEqual([a['state'] for a in history['attempts']], ['FINISHED', 'PENDING'])
        self.assertEqual(history['attempts'][0]['score'], 0)
        self.assertEqual(history['attempts'][0]['outcome'], 'compile_failed')
        self.assertNotIn('token', json.dumps(history))

    def test_failed_final_without_numeric_score_matches_final_report_zero(self):
        opened = self.open()
        credentials = opened['credentials']
        self.controller.start(credentials['session_id'], credentials['token'])
        operation = self.controller.store.reserve(credentials['session_id'], credentials['token'],
                                                   request_id='runtime-failed-final', action='final', source_sha256='a' * 64)
        self.controller.store.complete_operation(operation['operation_id'], {
            'outcome': 'runtime_failed', 'score': None,
            'stages': {'build': 'passed', 'runtime': 'failed', 'correctness': 'not_run', 'performance': 'not_run'}})
        final = self.controller.report(credentials['session_id'], credentials['token'])['report']['result']
        historical = self.history()['attempts'][0]
        self.assertEqual(historical['outcome'], 'runtime_failed')
        self.assertEqual(historical['score'], 0)
        self.assertEqual(historical['score'], final['score'])

    def test_request_id_cannot_change_track_and_tracks_have_distinct_attempts(self):
        first = self.open()
        self.denied('REQUEST_CONFLICT', self.open, 'r1', 'optimize')
        second = self.open('optimize-request', 'optimize')
        self.assertEqual(second['attempt_index'], 2)
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertEqual(self.open('new-opt-request', 'optimize')['session_id'], second['session_id'])

    def test_wrong_and_cross_launcher_tokens_cannot_allocate_or_read(self):
        other = self.launcher.admin_register(PACKAGE, owner_id='another-admin')
        for token in ('wrong', other['token']):
            self.denied('AUTH_FAILED', self.open, 'r1', 'from_scratch', token)
            self.denied('AUTH_FAILED', self.launcher.history, self.config['launcher_id'], token)
        self.assertEqual(self.history()['attempts'], [])

    def test_crash_before_session_create_is_journaled_and_cannot_retry_around_failure(self):
        with patch.object(self.controller, 'admin_create', side_effect=RuntimeError('simulated crash')):
            self.denied('LAUNCH_INCOMPLETE', self.open)
        self.denied('LAUNCH_INCOMPLETE', self.open)
        self.denied('LAUNCH_INCOMPLETE', self.open, 'different-request')
        history = self.history()['attempts']
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['state'], 'ALLOCATING')
        with sqlite3.connect(str(self.controller.store.db_path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)

    def test_crash_after_session_create_keeps_one_incomplete_attempt(self):
        with patch.object(self.launcher, '_bundle', side_effect=RuntimeError('simulated export crash')):
            self.denied('LAUNCH_INCOMPLETE', self.open)
        self.denied('LAUNCH_INCOMPLETE', self.open, 'new-request')
        history = self.history()['attempts']
        self.assertEqual(len(history), 1)
        self.assertIsNotNone(history[0]['session_id'])
        with sqlite3.connect(str(self.controller.store.db_path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 1)

    def test_concurrent_threads_allocate_one_active_attempt(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.open('thread-' + str(i)), range(8)))
        self.assertEqual(len({r['session_id'] for r in results}), 1)
        self.assertEqual(sum(not r['reused'] for r in results), 1)
        self.assertEqual(len(self.history()['attempts']), 1)

    def test_independent_processes_share_node_local_allocation_lock(self):
        code = '''import json,sys
from engine.controller import Controller
from engine.launching import Launcher
p=json.load(sys.stdin)
c=Controller(p["state"])
r=Launcher(c).open(p["launcher_id"],p["token"],track="from_scratch",request_id=p["request_id"])
print(json.dumps({"session_id":r["session_id"],"attempt_index":r["attempt_index"]}))
'''
        requests = [{'state': str(self.controller.root), 'launcher_id': self.config['launcher_id'],
                     'token': self.config['token'], 'request_id': 'process-' + str(i)} for i in range(3)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda payload: subprocess.run([sys.executable, '-c', code], input=json.dumps(payload),
                                                                   capture_output=True, text=True,
                                                                   cwd=str(PACKAGE.parents[1]), timeout=30), requests))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        opened = [json.loads(r.stdout) for r in results]
        self.assertEqual(len({r['session_id'] for r in opened}), 1)
        self.assertEqual([r['attempt_index'] for r in opened], [1, 1, 1])

    def test_expired_session_allows_new_attempt_but_old_request_still_returns_old(self):
        self.config = self.launcher.admin_register(PACKAGE, owner_id='short-test',
                                                   policy=Policy(active_seconds=10, final_grace_seconds=2))
        first = self.open()
        self.controller.start(**dict(session_id=first['session_id'], token=first['credentials']['token']))
        self.now += 13
        second = self.open('new-attempt')
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertEqual(self.open('r1')['session']['state'], 'EXPIRED')

    def test_http_launch_allowlist_and_history_do_not_expose_credentials(self):
        server = make_server(self.controller, ('127.0.0.1', 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:' + str(server.server_address[1]) + '/v1/launchers/' + self.config['launcher_id']
            def call(action, body):
                request = urllib.request.Request(base + '/' + action, data=json.dumps(body).encode(),
                                                  headers={'Authorization': 'Bearer ' + self.config['token']})
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            valid = {'track': 'from_scratch', 'request_id': 'http'}
            for field in ('policy', 'evaluation_purpose', 'problem', 'mode', 'owner_id', 'baseline_path'):
                status, _ = call('open', dict(valid, **{field: 'override'}))
                self.assertEqual(status, 400)
            self.assertEqual(self.history()['attempts'], [])
            status, result = call('open', valid)
            self.assertEqual(status, 200)
            self.assertIn('credentials', result)
            status, history = call('history', {})
            self.assertEqual(status, 200)
            self.assertNotIn('token', json.dumps(history))
            self.assertEqual(call('history', {'extra': True})[0], 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_formal_controller_cannot_register_or_allocate_development_launcher(self):
        self.controller.mode = 'formal'
        self.denied('LAUNCH_DISABLED', self.launcher.admin_register, PACKAGE, owner_id='admin')
        self.denied('LAUNCH_DISABLED', self.open)

    def test_database_and_journals_are_private_when_session_credentials_are_retained(self):
        self.controller.store.db_path.chmod(0o644)
        launcher = Launcher(self.controller)
        self.assertEqual(self.controller.store.db_path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(str(self.controller.store.db_path)) as db:
            db.execute('SELECT * FROM launchers').fetchall()
            launcher.open(self.config['launcher_id'], self.config['token'], track='from_scratch', request_id='private')
            for suffix in ('-wal', '-shm'):
                path = self.controller.store.db_path.with_name(self.controller.store.db_path.name + suffix)
                if path.exists():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_shared_database_parent_is_rejected_without_chmod(self):
        shared = self.root / 'shared-node-local'
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        controller = Controller(self.root / 'other-state', database=shared / 'budget.sqlite')
        self.denied('LAUNCH_DISABLED', Launcher, controller)
        self.assertEqual(shared.stat().st_mode & 0o777, 0o755)
