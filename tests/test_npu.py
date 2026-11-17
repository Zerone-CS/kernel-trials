"""NPU orchestration contracts with simulated tools, never fake GPU claims.

The real CANN/NPU acceptance run is performed separately on the server. These
tests exercise independent byte verification, profiling parser, quota-facing
outcomes and baseline binding without requiring device access locally.
"""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch

from engine.calibration import create_baseline
from engine.identity import measurement_engine_digest, protocol_sha256
from engine.npu import NpuEnvironmentError, run_npu_candidate
from engine.problems import load_problem


SOURCE = Path(__file__).resolve().parents[1] / 'problems' / 'batch_matmul_reduce'


class Adapter:
    def cases(self, suite, seed):
        return [{'case_id': name, 'suite': suite, 'seed': seed, 'repeats': repeat}
                for name, repeat in ([('public_01', 1), ('public_02', 4)] if suite == 'public' else [('hidden_01', 5)])]

    def prepare(self, spec, directory):
        directory.mkdir(parents=True)
        data = struct.pack('<f', 2)
        (directory / 'input.bin').write_bytes(data)
        (directory / 'golden.bin').write_bytes(data)
        identity = {'bytes': 4, 'sha256': hashlib.sha256(data).hexdigest()}
        return {'inputs': ['input.bin'], 'oracle_files': ['golden.bin'],
                'artifacts': {'input.bin': identity, 'golden.bin': identity},
                'output_dtype': 'float32', 'output_elements': 1}

    def runner_args(self, spec, inputs, output, *, mode, repeat, warmup):
        return ['--input', str(inputs / 'input.bin'), '--output', str(output),
                '--mode', mode, '--repeat', str(repeat), '--warmup', str(warmup)]

    def validate_execution(self, spec, records, *, mode, repeat, warmup):
        return {'passed': len(records) == 1 and records[0].get('status') == 'ok',
                'actual_cores': 24, 'infrastructure_failure': False}

    def verify(self, spec, directory, output, *, expected_calls):
        golden = (directory / 'golden.bin').read_bytes()
        paths = [output] + [output.parent / (output.name + '.call-' + str(i)) for i in range(expected_calls)]
        passed = all(path.read_bytes() == golden for path in paths)
        return {'passed': passed, 'status': 'PASS' if passed else 'OUTPUT_MISMATCH',
                'checked_captures': len(paths)}


class NpuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(SOURCE, self.root / 'package')
        self.package = load_problem(self.root / 'package')
        self.calls = []
        self.compile_failure = False
        self.profile_bad_call = False
        self.profile_bad_csv = False
        self.runtime_failure = False
        self.environment = {'fingerprint': 'environment-v1', 'device': 0, 'cores': 0}
        for mock in (
            patch('engine.npu._cann_environment', return_value=({'PATH': '/usr/bin'}, Path('/mock'), Path('/mock/env'))),
            patch('engine.npu._environment_identity', return_value=self.environment),
            patch('engine.npu._device_lock', return_value=nullcontext()),
            patch('engine.npu._execute', side_effect=self.execute),
            patch('engine.npu.shutil.which', side_effect=lambda name, **kwargs: '/mock/' + name),
            patch.object(type(self.package), 'load_adapter', return_value=Adapter()),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def execute(self, argv, *, cwd, log, timeout, env):
        self.calls.append(argv)
        log.parent.mkdir(parents=True, exist_ok=True)
        result = {'started': True, 'returncode': 0, 'timed_out': False,
                  'log': log.name, 'log_truncated': False, 'elapsed_seconds': 0.01}
        if argv[0] == 'cmake':
            if self.compile_failure:
                log.write_text('kernel.asc: unknown name\n')
                return dict(result, returncode=1)
            if '--build' in argv:
                build = Path(argv[argv.index('--build') + 1])
                binary = build / 'op_eval_runner'
                binary.write_bytes(b'simulated-executable')
                binary.chmod(0o755)
            log.write_text('simulated build\n')
            return result
        output = Path(argv[argv.index('--output') + 1])
        mode = argv[argv.index('--mode') + 1]
        repeat = int(argv[argv.index('--repeat') + 1])
        warmup = int(argv[argv.index('--warmup') + 1])
        if self.runtime_failure and mode == 'correctness':
            log.write_text('{"status":"failed"}\n')
            return dict(result, returncode=1)
        raw = struct.pack('<f', 2)
        output.write_bytes(raw)
        for i in range(repeat + warmup):
            value = struct.pack('<f', 0) if mode == 'profile' and self.profile_bad_call and i == 0 else raw
            (output.parent / (output.name + '.call-' + str(i))).write_bytes(value)
        log.write_text('{"status":"ok"}\n')
        if mode == 'profile':
            directory = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--output=')))
            directory.mkdir()
            rows = ['Task ID,Stream ID,Task Type,Op Name,Device_id,Task Duration(us),Task Start Time(us)']
            calls = repeat + warmup - int(self.profile_bad_csv)
            rows.extend(f'{i},0,AI_CORE,kernel,0,2,{i * 10}' for i in range(calls))
            (directory / 'op_summary.csv').write_text('\n'.join(rows) + '\n')
        return result

    def run_case(self, action='final', **kwargs):
        run_dir = self.root / ('run-' + str(len(list(self.root.glob('run-*')))))
        return run_npu_candidate(self.package, self.package.path('reference').read_bytes(), action,
                                 run_dir, **kwargs)

    def baseline(self, report):
        return create_baseline(report, package_digest=self.package.digest, environment=self.environment,
                               reference_sha256=report['source_sha256'],
                               engine_digest=measurement_engine_digest(), protocol_digest=protocol_sha256(self.package),
                               expected_case_ids=['public_01', 'public_02'])

    def test_check_uses_public_only_and_case_specific_repeats(self):
        report = self.run_case('check')
        self.assertEqual(report['outcome'], 'passed')
        self.assertEqual(report['correctness']['passed'], 2)
        self.assertEqual(report['correctness']['scope'], 'public_full')
        self.assertEqual(report['stages']['performance'], 'not_run')
        repeats = [int(argv[argv.index('--repeat') + 1]) for argv in self.calls if '--repeat' in argv]
        self.assertEqual(repeats, [3, 4])

    def test_final_calibration_rechecks_every_profile_call_and_scores_compatible_baseline(self):
        first = self.run_case()
        self.assertEqual(first['outcome'], 'passed', first)
        self.assertEqual(first['correctness']['passed'], 3)
        self.assertEqual(first['stages']['performance'], 'passed')
        self.assertIsNone(first['score'])
        self.assertEqual(first['score_status'], 'CALIBRATION_REQUIRED')
        self.assertEqual(len(first['measurements']), 2)
        for measurement in first['measurements']:
            self.assertEqual(len(measurement['captures']), 3)
            self.assertEqual(measurement['median_us'], 2)
            self.assertTrue(all(c['correctness']['checked_captures'] == 41 for c in measurement['captures']))
        second = self.run_case(baseline=self.baseline(first))
        self.assertEqual(second['outcome'], 'passed', second)
        self.assertEqual(second['score'], 100)
        self.assertFalse(second['security']['rankable'])

    def test_bad_early_profile_call_cannot_hide_behind_correct_final_output(self):
        self.profile_bad_call = True
        report = self.run_case()
        self.assertEqual(report['outcome'], 'correctness_failed')
        self.assertEqual(report['score'], 0)
        self.assertEqual(report['error_category'], 'profile_invocation_output_mismatch')

    def test_missing_compute_task_is_invalid_measurement_not_zero_score(self):
        self.profile_bad_csv = True
        report = self.run_case()
        self.assertEqual(report['outcome'], 'measurement_invalid')
        self.assertIsNone(report['score'])
        self.assertEqual(report['error_category'], 'msprof_capture_unsupported')

    def test_compiler_failure_gates_runtime_and_profile(self):
        self.compile_failure = True
        report = self.run_case()
        self.assertEqual(report['outcome'], 'compile_failed')
        self.assertEqual(report['score'], 0)
        self.assertEqual(report['stages']['runtime'], 'not_run')
        self.assertIn('unknown name', report['build_diagnostics'])
        self.assertEqual(len(self.calls), 1)

    def test_formal_is_refused_without_environment_probe_or_process(self):
        report = self.run_case(mode='formal')
        self.assertEqual(report['outcome'], 'formal_unavailable')
        self.assertFalse(self.calls)
        self.assertFalse(list(self.root.glob('run-*')))

    def test_environment_failure_is_invalid_without_compilation(self):
        with patch('engine.npu._cann_environment', side_effect=NpuEnvironmentError('missing SDK')):
            report = self.run_case()
        self.assertEqual(report['outcome'], 'infrastructure_error')
        self.assertIsNone(report['score'])
        self.assertFalse(self.calls)

    def test_baseline_environment_change_cannot_be_ignored(self):
        first = self.run_case()
        baseline = self.baseline(first)
        self.environment['fingerprint'] = 'different-machine'
        before = len(self.calls)
        report = self.run_case(baseline=baseline)
        self.assertEqual(report['outcome'], 'infrastructure_error')
        self.assertIsNone(report['score'])
        self.assertEqual(len(self.calls), before)

    def test_runtime_failure_has_no_performance_measurements(self):
        self.runtime_failure = True
        report = self.run_case()
        self.assertEqual(report['outcome'], 'runtime_failed')
        self.assertEqual(report['score'], 0)
        self.assertEqual(report['measurements'], [])
