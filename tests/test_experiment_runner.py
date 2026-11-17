import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import experiment_runner as runner


class ExperimentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'trial workspace'
        self.workspace.mkdir()
        for name in ('OP_EVAL_WORKFLOW.md', 'kernel.asc', 'eval', 'METHOD.md'):
            (self.workspace / name).write_text('fixture')

    def run_cli(self, script, *, provider='command', command=None, **config):
        return runner.run_agent({'provider': provider, 'command': command or [sys.executable, '-c', script], **config},
                                self.workspace, 5, self.root / 'logs')

    def output(self, result):
        return json.loads(Path(result['stdout_path']).read_text())

    def test_explicit_argv_uses_trial_cwd_and_literal_placeholders(self):
        marker = self.root / 'shell-was-run'
        literal = '$(touch ' + str(marker) + ') `echo bad` {untouched}'
        script = 'import os,sys,json; print(json.dumps({"cwd":os.getcwd(),"args":sys.argv[1:],"env":os.environ["TRIAL_MARKER"]}))'
        result = self.run_cli(script, command=[sys.executable, '-c', script, '{workspace}', '{prompt}', literal],
                              env={'TRIAL_MARKER': 'unique'})
        self.assertEqual(result['status'], 'completed')
        evidence = self.output(result)
        self.assertEqual(evidence['cwd'], str(self.workspace.resolve()))
        self.assertEqual(evidence['args'], [str(self.workspace.resolve()), runner.PROMPT, literal])
        self.assertEqual(evidence['env'], 'unique')
        self.assertFalse(marker.exists())
        self.assertNotIn('unique', json.dumps(result))
        self.assertEqual(result['fresh_session_contract'], 'owner_supplied_command')
        self.assertEqual((self.workspace / 'kernel.asc').read_text(), 'fixture')

    def fake_binary(self, name):
        path = self.root / name
        path.write_text('#!' + sys.executable + '\nimport os,sys,json\nprint(json.dumps({"args":sys.argv[1:],"cwd":os.getcwd(),"config":os.environ.get("CLAUDE_CONFIG_DIR")}))\n')
        path.chmod(0o700)
        return path

    def test_claude_official_root_compatible_flags_and_user_config(self):
        self.fake_binary('claude')
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': '/configured/profile'}):
            result = runner.run_agent({'provider': 'claude', 'model': 'configured', 'env': {'PATH': str(self.root)}},
                                      self.workspace, 5, self.root / 'logs')
        self.assertEqual(result['status'], 'completed')
        evidence = self.output(result)
        self.assertEqual(evidence['config'], '/configured/profile')
        argv = evidence['args']
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'dontAsk')
        self.assertIn('--no-session-persistence', argv)
        self.assertIn('Bash(./eval *)', argv)
        for unwanted in ('--model', '--resume', '--continue', '--dangerously-skip-permissions'):
            self.assertNotIn(unwanted, argv)
        self.assertEqual(result['fresh_session_contract'], 'builtin_no_resume')

    def test_trial_invoked_from_claude_slash_does_not_inherit_parent_session_marker(self):
        with patch.dict(os.environ, {'CLAUDECODE':'1','CLAUDE_CODE_ENTRYPOINT':'cli','CLAUDE_CONFIG_DIR':'/configured/profile'}):
            result = self.run_cli('import os,json; print(json.dumps({"nested":os.environ.get("CLAUDECODE"),"entry":os.environ.get("CLAUDE_CODE_ENTRYPOINT"),"config":os.environ.get("CLAUDE_CONFIG_DIR")}))', provider='claude')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.output(result), {'nested':None,'entry':None,'config':'/configured/profile'})

    def test_claude_explicit_profile_overrides_inherited_profile(self):
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR':'/inherited/profile'}):
            result = self.run_cli('import os,json; print(json.dumps(os.environ.get("CLAUDE_CONFIG_DIR")))',
                provider='claude', env={'CLAUDE_CONFIG_DIR':'/explicit/profile'})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.output(result), '/explicit/profile')

    def test_claude_without_profile_preserves_cli_default(self):
        with patch.dict(os.environ, {}, clear=True):
            result = self.run_cli('import os,json; print(json.dumps(os.environ.get("CLAUDE_CONFIG_DIR")))', provider='claude')
        self.assertEqual(result['status'], 'completed')
        self.assertIsNone(self.output(result))

    def test_all_builtin_adapters_are_fresh_and_honor_model(self):
        for provider in ('claude', 'opencode', 'codex', 'gemini'):
            with self.subTest(provider=provider):
                self.fake_binary(provider)
                result = runner.run_agent({'provider': provider, 'model': 'test-model', 'env': {'PATH': str(self.root)}},
                                          self.workspace, 5, self.root / ('logs-' + provider))
                self.assertEqual(result['status'], 'completed')
                argv = self.output(result)['args']
                self.assertIn(runner.PROMPT, argv)
                self.assertEqual(argv[argv.index('--model') + 1], 'test-model')
                self.assertFalse(set(('--resume', '--continue', '--session', '--full-auto')) & set(argv))
                if provider == 'codex':
                    self.assertIn('--ephemeral', argv)
                    self.assertIn('sandbox_workspace_write.network_access=true', argv)
                if provider == 'opencode':
                    self.assertIn('--standalone', argv)

    def test_missing_provider_is_clear_and_does_not_install(self):
        result = runner.run_agent({'provider': 'claude', 'env': {'PATH': str(self.root)}},
                                  self.workspace, 5, self.root / 'logs')
        self.assertEqual(result['status'], 'provider_unavailable')
        self.assertIsNone(result['returncode'])
        self.assertIn('not installed', result['error'])

    def test_known_local_codex_wrapper_is_never_executed(self):
        with patch.object(runner.shutil, 'which', return_value='/opt/homebrew/bin/codex'), \
                patch.object(runner.subprocess, 'Popen') as spawn:
            result = runner.run_agent({'provider': 'codex'}, self.workspace, 5, self.root / 'logs')
        spawn.assert_not_called()
        self.assertEqual(result['status'], 'configuration_error')
        self.assertIn('must not be executed', result['error'])

    def test_agent_failure_is_recorded_once_and_never_finalized(self):
        script = 'from pathlib import Path; import sys; p=Path("calls"); p.write_text(p.read_text()+"x" if p.exists() else "x"); print("failure",file=sys.stderr); sys.exit(7)'
        result = self.run_cli(script)
        self.assertEqual(result['returncode'], 7)
        self.assertEqual(result['status'], 'agent_failed')
        self.assertEqual((self.workspace / 'calls').read_text(), 'x')
        self.assertEqual(Path(result['stderr_path']).read_text(), 'failure\n')
        self.assertFalse((self.workspace / 'REPORT.md').exists())

    def test_timeout_kills_same_group_descendants(self):
        # Child ignores TERM: the group KILL must work even after parent exits.
        child = 'import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path("child-ready").write_text("yes"); time.sleep(2); Path("survived").write_text("bad")'
        script = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(60)'
        result = runner.run_agent({'provider': 'command', 'command': [sys.executable, '-c', script]},
                                  self.workspace, 1, self.root / 'logs')
        self.assertTrue((self.workspace / 'child-ready').exists())
        self.assertEqual(result['status'], 'timeout')
        self.assertTrue(result['timed_out'])
        self.assertLess(result['elapsed_seconds'], 5)
        time.sleep(1.2)
        self.assertFalse((self.workspace / 'survived').exists())
        self.assertIn('SIGKILL', result['termination_signals'])

    def test_normal_exit_also_reaps_background_child(self):
        child = 'import time; from pathlib import Path; time.sleep(1); Path("survived").write_text("bad")'
        script = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c",' + repr(child) + '])'
        result = self.run_cli(script)
        self.assertEqual(result['status'], 'completed')
        time.sleep(1.1)
        self.assertFalse((self.workspace / 'survived').exists())

    def test_private_logs_are_not_overwritten(self):
        result = self.run_cli('print("first")')
        self.assertEqual(stat.S_IMODE((self.root / 'logs').stat().st_mode), 0o700)
        for key in ('stdout_path', 'stderr_path'):
            self.assertEqual(stat.S_IMODE(Path(result[key]).stat().st_mode), 0o600)
        again = self.run_cli('print("second")')
        self.assertEqual(again['status'], 'configuration_error')
        self.assertEqual(Path(result['stdout_path']).read_text(), 'first\n')

    def test_usage_metadata_excludes_response_and_secrets(self):
        event = {'type': 'result', 'is_error': False, 'result': 'PRIVATE response',
                 'usage': {'input_tokens': 10, 'output_tokens': 4, 'credential': 'SECRET'},
                 'total_cost_usd': 0.02, 'modelUsage': {'model-observed': {}}, 'num_turns': 2}
        result = self.run_cli('print(' + repr(json.dumps(event)) + ')', env={'API_KEY': 'SECRET'})
        self.assertEqual(result['tokens'], {'input_tokens': 10, 'output_tokens': 4})
        self.assertEqual(result['total_cost_usd'], 0.02)
        self.assertEqual(result['models_reported'], ['model-observed'])
        for secret in ('PRIVATE response', 'SECRET', 'API_KEY'):
            self.assertNotIn(secret, json.dumps(result))
        self.assertFalse(result['identity_verified'])

    def test_provider_reported_error_even_with_exit_zero_is_failure(self):
        result = self.run_cli('print(\'{"type":"result","is_error":true}\')')
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(result['status'], 'agent_failed')

    def test_codex_turn_usage_is_summed_without_response_content(self):
        events = [{'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 5}},
                  {'type': 'item.completed', 'text': 'private'},
                  {'type': 'turn.completed', 'usage': {'input_tokens': 20, 'output_tokens': 6}}]
        result = self.run_cli('print(' + repr('\n'.join(json.dumps(e) for e in events)) + ')', provider='codex')
        self.assertEqual(result['tokens'], {'input_tokens': 30, 'output_tokens': 11})
        self.assertNotIn('private', json.dumps(result))

    def test_bad_configuration_never_starts_process(self):
        invalid = [{'provider': 'command', 'command': 'echo nope'},
                   {'provider': 'claude', 'env': {'CLAUDE_CONFIG_DIR': 42}},
                   {'provider': 'claude', 'permission_mode': 'bypassPermissions'},
                   {'provider': 'unknown'}, {'provider': 'claude', 'unknown': True}]
        with patch.object(runner.os, 'geteuid', return_value=0), patch.object(runner.subprocess, 'Popen') as spawn:
            for config in invalid:
                with self.subTest(config=config):
                    result = runner.run_agent(config, self.workspace, 5, self.root / 'logs')
                    self.assertEqual(result['status'], 'configuration_error')
        spawn.assert_not_called()

    def test_adapter_fingerprint_stable_across_workspaces_but_changes_with_config(self):
        first = self.run_cli('pass')
        second_workspace = self.root / 'another-trial'
        second_workspace.mkdir()
        for name in ('OP_EVAL_WORKFLOW.md', 'kernel.asc', 'eval'):
            (second_workspace / name).write_text('fixture')
        config = {'provider': 'command', 'command': [sys.executable, '-c', 'pass']}
        second = runner.run_agent(config, second_workspace, 5, self.root / 'second-logs')
        self.assertEqual(first['adapter_fingerprint'], second['adapter_fingerprint'])
        config['env'] = {'MODEL_TEMPERATURE': '0.1'}
        third = runner.run_agent(config, second_workspace, 5, self.root / 'third-logs')
        self.assertNotEqual(second['adapter_fingerprint'], third['adapter_fingerprint'])


if __name__ == '__main__':
    unittest.main()
