import base64
import contextlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import experiment_remote as remote_module
from experiment import load_config


class RemoteExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        resources = self.project / 'notes dir'
        resources.mkdir()
        (resources / 'tips.md').write_text('method resource')
        (resources / '.snapshot.json').write_text('an ordinary method resource')
        (self.project / 'extra.bin').write_bytes(b'\0\1\xff')
        (self.project / 'instructions.txt').write_text('File instructions\n')
        self.config = self.project / 'compare settings.json'
        self.config.write_text(json.dumps({'tasks': ['full'], 'repeats': 2,
            'runner': {'provider': 'claude', 'env': {'PRIVATE_API_KEY': 'secret-marker'}},
            'conditions': [{'id': 'control'}, {'id': 'method', 'instructions': 'Inline instructions',
                'instructions_file': 'instructions.txt', 'resources': ['notes dir', 'extra.bin']}]}))
        self.remote = {'ssh_host': 'npu-eval-server',
                       'project_root': "/remote project/with ' quote/op_eval_v2"}
        self.experiment_id = 'a' * 32
        self.remote_run = PurePosixPath(self.remote['project_root']).parent / 'op_eval_experiment_runs' / self.experiment_id
        self.calls = []

    def response(self, value, code=0):
        return {'returncode': code, 'stdout': json.dumps(value).encode() + b'\n'}

    def fake_transport(self, host, argv, *, input_bytes=None, stream=False):
        self.calls.append((host, argv, input_bytes, stream))
        self.assertEqual(host, self.remote['ssh_host'])
        if argv[2] == remote_module._UPLOAD:
            payload = json.loads(input_bytes)
            return self.response({'uploaded': True, 'configuration_digest': payload['configuration_digest']})
        if argv[2] == remote_module._RUN:
            return self.response({'experiment_id': self.experiment_id,
                'report_path': str(self.remote_run / 'REPORT.md'),
                'json_path': str(self.remote_run / 'comparison.json'),
                'trial_count': 4, 'completed_trials': 4, 'reused': False})
        if argv[2] == remote_module._DOWNLOAD:
            data = {'REPORT.md': b'Final report; trial paths remain remote.\n',
                    'comparison.json': json.dumps({'experiment_id': self.experiment_id}).encode(),
                    'trials.json': json.dumps([{'report_path': str(self.remote_run / 'trials/a/REPORT.md')}]).encode(),
                    'manifest.json': json.dumps({'experiment_id': self.experiment_id}).encode()}
            return self.response({'files': [remote_module._entry(n, v) for n, v in data.items()]})
        self.fail('Unexpected remote command')

    def test_snapshot_preserves_method_and_runner_identities_and_relative_resources(self):
        original, _, runners = load_config(self.config)
        snapshot = remote_module._snapshot(self.config)
        copy = self.root / 'copy'
        copy.mkdir()
        for file in snapshot['files']:
            path = copy / file['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(file['content_b64']))
        reconstructed, _, actual_runners = load_config(copy / 'config.json')
        self.assertEqual(original, reconstructed)
        self.assertEqual(runners, actual_runners)
        config = json.loads((copy / 'config.json').read_text())
        self.assertEqual(config['name'], self.config.stem)
        self.assertEqual(config['conditions'][1]['resources'],
            ['conditions/1/resources/extra.bin', 'conditions/1/resources/notes dir'])
        self.assertNotIn('secret-marker', json.dumps(snapshot['plan']))

    def test_upload_run_and_only_public_report_download(self):
        with patch.object(remote_module, '_ssh_transport', side_effect=self.fake_transport), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = remote_module.run_remote_experiment(self.project, self.config.name, self.remote, new=True)
        self.assertEqual(len(self.calls), 3)
        upload, run, download = self.calls
        self.assertEqual(run[1][-1], 'new')
        self.assertTrue(run[3])
        self.assertIn('/.eval/experiment-inputs/', run[1][-2])
        self.assertEqual(download[1][-1], str(self.remote_run))
        self.assertTrue(Path(result['report_path']).is_file())
        self.assertEqual(result['execution_location'], 'remote')
        self.assertEqual(result['remote_report_path'], str(self.remote_run / 'REPORT.md'))
        self.assertTrue(result['trial_report_paths_are_remote'])
        evidence = Path(result['report_path']).parent
        self.assertEqual(set(p.name for p in evidence.iterdir()), set(remote_module._REPORTS) | {'remote_origin.json'})
        for path in evidence.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o700)
        self.assertIn('/remote project/', Path(result['trials_path']).read_text())
        self.assertNotIn('secret-marker', json.dumps(result) + output.getvalue())

    def test_default_resume_and_nonzero_run_has_no_download_or_secret_echo(self):
        def transport(*args, **kwargs):
            if args[1][2] == remote_module._RUN:
                self.assertEqual(args[1][-1], 'resume')
                return {'returncode': 7, 'stdout': b'secret-marker PRIVATE_API_KEY\n'}
            return self.fake_transport(*args, **kwargs)
        with patch.object(remote_module, '_ssh_transport', side_effect=transport), \
                self.assertRaises(remote_module.RemoteExperimentError) as caught:
            remote_module.run_remote_experiment(self.project, self.config, self.remote)
        self.assertEqual(caught.exception.code, 'REMOTE_EXPERIMENT_FAILED')
        self.assertNotIn('secret-marker', str(caught.exception))
        self.assertEqual(len(self.calls), 1)

    def test_wrong_evidence_checksum_or_paths_never_materialize(self):
        for fault in ('checksum', 'path', 'manifest'):
            with self.subTest(fault=fault):
                def transport(*args, **kwargs):
                    result = self.fake_transport(*args, **kwargs)
                    if args[1][2] == remote_module._DOWNLOAD:
                        payload = json.loads(result['stdout'])
                        if fault == 'checksum':
                            payload['files'][0]['sha256'] = 'f' * 64
                        elif fault == 'path':
                            payload['files'][0]['path'] = '../session.json'
                        else:
                            payload['files'][-1] = remote_module._entry('manifest.json', b'{"experiment_id":"wrong"}')
                        return self.response(payload)
                    return result
                with patch.object(remote_module, '_ssh_transport', side_effect=transport), \
                        self.assertRaises(remote_module.RemoteExperimentError):
                    remote_module.run_remote_experiment(self.project, self.config, self.remote)
                self.assertFalse((self.project / '.eval/remote-experiments').exists())

    def test_upload_script_is_private_immutable_and_reconstructs_exact_digest(self):
        server = self.root / 'fake server'
        server.mkdir()
        snapshot = remote_module._snapshot(self.config)
        argv = [sys.executable, '-c', remote_module._UPLOAD, str(server), snapshot['configuration_digest']]
        env = dict(os.environ, PYTHONPATH=str(Path(remote_module.__file__).parent))
        def upload():
            return subprocess.run(argv, input=remote_module._json_bytes(snapshot), capture_output=True,
                                  env=env, timeout=10)
        first = upload()
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        self.assertFalse(json.loads(first.stdout)['reused'])
        destination = server / '.eval/experiment-inputs' / snapshot['configuration_digest']
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((destination / 'config.json').stat().st_mode), 0o600)
        repeated = upload()
        self.assertEqual(repeated.returncode, 0, repeated.stderr.decode())
        self.assertTrue(json.loads(repeated.stdout)['reused'])
        (destination / 'conditions/1/resources/extra.bin').write_bytes(b'changed')
        refused = upload()
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual((destination / 'conditions/1/resources/extra.bin').read_bytes(), b'changed')

    def test_upload_rejects_traversal_before_writing_target(self):
        server = self.root / 'server'
        server.mkdir()
        snapshot = remote_module._snapshot(self.config)
        snapshot['files'].append(remote_module._entry('../escape', b'bad'))
        result = subprocess.run([sys.executable, '-c', remote_module._UPLOAD, str(server), snapshot['configuration_digest']],
            input=remote_module._json_bytes(snapshot), capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((server / '.eval/escape').exists())

    def test_transport_quotes_remote_arguments_without_local_shell_and_filters_events(self):
        fake = self.root / 'ssh'
        fake.write_text('#!' + sys.executable + '\nimport json,sys\n'
            'print(json.dumps({"args":sys.argv[1:]}))\n')
        fake.chmod(0o700)
        args = ['python3', '-c', "print('quoted')", '/path with spaces/$(touch FAIL)', "a'b"]
        with patch.dict(os.environ, {'PATH': str(self.root)}):
            result = remote_module._ssh_transport('test-host', args)
        observed = json.loads(result['stdout'])['args']
        self.assertEqual(observed[-2], 'test-host')
        self.assertEqual(shlex.split(observed[-1]), args)
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            remote_module._progress(b'{"event":"trial_started","trial_id":"a","secret":"do-not-print"}')
            remote_module._progress(b'{"result":"do-not-print"}')
        self.assertNotIn('do-not-print', captured.getvalue())
        self.assertEqual(json.loads(captured.getvalue())['execution_location'], 'remote')

    def test_invalid_remote_settings_do_not_attempt_ssh(self):
        invalid = [{'ssh_host': '-oProxyCommand=bad', 'project_root': '/project'},
                   {'ssh_host': 'host', 'project_root': '../project'},
                   {'ssh_host': 'host', 'project_root': '/project/../other'},
                   {'ssh_host': 'host', 'project_root': '/'},
                   dict(self.remote, unknown=True)]
        with patch.object(remote_module, '_ssh_transport') as transport:
            for setting in invalid:
                with self.subTest(setting=setting), self.assertRaises(remote_module.RemoteExperimentError):
                    remote_module.run_remote_experiment(self.project, self.config, setting)
        transport.assert_not_called()

    def test_failed_ssh_retains_private_stderr_and_exposes_only_safe_diagnosis(self):
        fake = self.root / 'ssh'
        fake.write_text('#!' + sys.executable + '\nimport sys\n'
            'print("ModuleNotFoundError: No module named assigned", file=sys.stderr)\n'
            'print("SECRET credential value", file=sys.stderr)\n'
            'sys.exit(1)\n')
        fake.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(self.root)}):
            result = remote_module._ssh_transport('test-host', ['python3', '-c', 'pass'])
        diagnostic = Path(result['stderr_path'])
        self.addCleanup(diagnostic.unlink)
        self.assertEqual(stat.S_IMODE(diagnostic.stat().st_mode), 0o600)
        self.assertIn('SECRET credential value', diagnostic.read_text())
        self.assertEqual(result['diagnostic_code'], 'REMOTE_DEPENDENCY_MISSING')
        with self.assertRaises(remote_module.RemoteExperimentError) as caught:
            remote_module._response(result, 'upload')
        self.assertIn('missing a Python dependency', str(caught.exception))
        self.assertIn(str(diagnostic), str(caught.exception))
        self.assertNotIn('SECRET', str(caught.exception))

    def test_incomplete_remote_deployment_fails_without_committing_snapshot(self):
        server = self.root / 'incomplete server'
        server.mkdir()
        (server / 'experiment.py').write_text('from launch import load_config\n')
        (server / 'launch.py').write_text('from missing_assigned_module_fixture import load_config\n')
        snapshot = remote_module._snapshot(self.config)
        result = subprocess.run([sys.executable, '-c', remote_module._UPLOAD, str(server), snapshot['configuration_digest']],
            input=remote_module._json_bytes(snapshot), capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'ModuleNotFoundError', result.stderr)
        self.assertFalse((server / '.eval/experiment-inputs' / snapshot['configuration_digest']).exists())


if __name__ == '__main__':
    unittest.main()
