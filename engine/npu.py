"""Ascend C development backend with independent output verification and msprof.

The selected package defines its ABI, inputs and oracle. This worker owns fixed
builds, process limits, profiler collection and baseline compatibility. It does
not claim hostile-code isolation: candidate Host C++ shares the runner process.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import stat
import subprocess
import time
from typing import Any, Dict, Mapping, Optional

from .problems import ProblemError, ProblemPackage, load_problem, validate_candidate_source
from .metrics import MeasurementError
from .runner import _bounded_outputs, _execute, _inputs_only, _positive_number
from .security import SecurityError, SecurityPolicy


class NpuEnvironmentError(ValueError):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _cann_environment():
    """Source administrator-installed CANN, without provider secrets or HOME."""
    roots = []
    configured = os.environ.get('ASCEND_HOME_PATH')
    if configured:
        roots.append(Path(configured))
    roots.extend(Path(p) for p in ('/usr/local/Ascend/cann-9.0.0',
                                  '/usr/local/Ascend/ascend-toolkit/latest'))
    sdk = next((root.resolve() for root in roots if root.is_dir()), None)
    if sdk is None:
        raise NpuEnvironmentError('CANN SDK is not installed at a configured administrator path')
    scripts = [sdk / 'set_env.sh', sdk.parent / 'set_env.sh']
    script = next((path for path in scripts if path.is_file()), None)
    if script is None:
        raise NpuEnvironmentError('CANN set_env.sh is unavailable')
    base = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'LANG': 'C', 'LC_ALL': 'C'}
    # Positional argv protects the administrator path from shell interpolation.
    process = subprocess.run(['/bin/bash', '-c',
                              'source "$1" >/dev/null 2>&1 && env -0',
                              'op-eval-cann-environment', str(script)],
                             env=base, capture_output=True, timeout=20, check=False)
    if process.returncode != 0:
        raise NpuEnvironmentError('CANN environment initialization failed')
    result = {}
    for entry in process.stdout.split(b'\0'):
        if b'=' in entry:
            key, value = entry.split(b'=', 1)
            result[key.decode()] = value.decode()
    for key in ('HOME', 'PWD', 'OLDPWD', 'SHLVL', '_'):
        result.pop(key, None)
    result.update(ASCEND_HOME_PATH=str(sdk), OMP_NUM_THREADS='1',
                  OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    for program in ('cmake', 'bisheng', 'msprof'):
        if not shutil.which(program, path=result.get('PATH')):
            raise NpuEnvironmentError('Required CANN build/profile tool is missing: ' + program)
    return result, sdk, script


def _environment_identity(package, env, sdk, script):
    from .identity import measurement_engine_digest, protocol_sha256
    protocol = json.loads(package.path('protocol').read_text())
    def read_optional(path):
        return path.read_text() if path.is_file() else None
    configuration = {
        'hostname': platform.node(), 'machine': platform.machine(),
        'sdk_root': str(sdk), 'sdk_version': read_optional(sdk / 'compiler/version.info'),
        'cann_environment_sha256': _file_sha(script),
        'driver_version': read_optional(Path('/usr/local/Ascend/driver/version.info')),
        'npu_arch': protocol['npu_arch'], 'device': protocol['runtime']['device'],
        'cores': protocol['runtime']['cores'],
        'tools': {name: shutil.which(name, path=env.get('PATH'))
                  for name in ('cmake', 'bisheng', 'msprof')},
    }
    return {'configuration': configuration,
            'fingerprint': _sha(json.dumps(configuration, sort_keys=True).encode()),
            'engine_sha256': measurement_engine_digest(), 'problem_digest': package.digest,
            'protocol_sha256': protocol_sha256(package),
            'device': protocol['runtime']['device'], 'cores': protocol['runtime']['cores']}


def npu_environment(package):
    """Read-only stable environment identity used before starting agent clocks."""
    package = package if isinstance(package, ProblemPackage) else load_problem(package)
    package.assert_unchanged()
    return _environment_identity(package, *_cann_environment())


@contextmanager
def _device_lock(device):
    """Nonblocking cooperative lease across calibration and worker processes."""
    import fcntl
    path = Path('/tmp') / ('op-eval-v2-npu-' + str(device) + '.lock')
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise NpuEnvironmentError('Device lock is not owned by the judge account')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise NpuEnvironmentError('NPU is occupied by another evaluator operation') from error
        yield
    finally:
        os.close(descriptor)


def _records(log):
    result = []
    for line in Path(log).read_text(errors='replace').splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                result.append(item)
        except (ValueError, TypeError):
            pass
    return result


def _inputs_unchanged(manifest, input_dir):
    for name in manifest['inputs']:
        path = input_dir / name
        identity = manifest['artifacts'][name]
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != identity['bytes']
                or _file_sha(path) != identity['sha256']):
            return False
    return True


def _verify(adapter, spec, judge_dir, manifest, input_dir, output, calls):
    if not _inputs_unchanged(manifest, input_dir):
        return {'passed': False, 'status': 'INPUT_FILE_CHANGED'}
    bounded = _bounded_outputs(manifest, output, calls)
    if bounded is not None:
        return bounded
    result = adapter.verify(spec, judge_dir, output, expected_calls=calls)
    if not isinstance(result, dict) or type(result.get('passed')) is not bool:
        raise ValueError('Adapter returned an invalid verification result')
    return result


def _public_verification(result, hidden=False):
    if hidden:
        return {'passed': result['passed'], 'status': 'PASS' if result['passed'] else 'FAIL'}
    return {key: result[key] for key in ('passed', 'status', 'checked_elements', 'failed_elements',
                                       'max_abs_error', 'checked_captures') if key in result}


def run_npu_candidate(package, source, action, run_dir, *, mode='development', seed=None,
                      admin_evidence=None, baseline=None):
    """Build/check/profile one admitted immutable source; no candidate options.

    A trusted calibration calls final with baseline=None. It obtains validated
    device times but no score; normal controller sessions require a matching
    administrator-calibrated baseline before their clocks start.
    """
    if action not in ('check', 'bench', 'final') or not isinstance(source, bytes):
        raise ValueError('Invalid action/source')
    if seed is not None and (type(seed) is not int or seed < 0):
        raise ValueError('Seed must be a trusted nonnegative integer')
    started = time.monotonic()
    root = Path(run_dir)
    writable = False
    report = {
        'schema_version': 1, 'action': action, 'source_sha256': _sha(source),
        'outcome': 'infrastructure_error', 'stages': {name: 'not_run' for name in
            ('build', 'runtime', 'correctness', 'performance')},
        'correctness': {'passed': 0, 'total': 0, 'executed': 0,
                        'scope': 'public_and_hidden_full' if action == 'final' else 'public_full'},
        'cases': [], 'measurements': [], 'build_commands': [], 'score': None,
        'score_status': 'NOT_MEASURED', 'metric': None, 'rankable': False,
        'diagnostics_are_score': False,
    }
    def finish(outcome, *, category=None, stage=None, message=None):
        report['outcome'] = outcome
        report['elapsed_seconds'] = time.monotonic() - started
        if category:
            report['error_category'] = category
            report['error'] = {'stage': stage, 'category': category, 'message': message or category}
        if outcome in ('compile_failed', 'runtime_failed', 'correctness_failed'):
            report['score'] = 0
            report['score_status'] = 'CANDIDATE_GATE_FAILED'
        elif outcome in ('measurement_invalid', 'infrastructure_error', 'formal_unavailable'):
            report['score'] = None
            report['score_status'] = outcome.upper()
        if writable:
            (root / 'report.json').write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n')
        return report
    try:
        package = package if isinstance(package, ProblemPackage) else load_problem(package)
        report.update(problem_id=package.problem_id, problem_digest=package.digest)
        try:
            report['security'] = SecurityPolicy(mode).enforce(
                admin_evidence, operator_profile=package.manifest.get('security', {}).get('operator_profile', 'legacy_host_untrusted'),
                problem_digest=package.digest, backend_profile='development_subprocess')
        except SecurityError as error:
            report['security'] = error.report
            return finish('formal_unavailable', stage='security', category=error.code)
        package.assert_unchanged()
        protocol = json.loads(package.path('protocol').read_text())
        if protocol.get('platform') != 'ascend_npu':
            raise NpuEnvironmentError('NPU backend requires an Ascend problem protocol')
        if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
            return finish('infrastructure_error', stage='setup', category='run_directory_not_empty')
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()
        writable = True
        for name in ('build', 'home', 'tmp', 'judge', 'candidate', 'timing'):
            (root / name).mkdir()
        env, sdk, script = _cann_environment()
        report['environment'] = _environment_identity(package, env, sdk, script)
        env.update(HOME=str(root / 'home'), TMPDIR=str(root / 'tmp'))
        adapter = package.load_adapter()
        if not callable(getattr(adapter, 'validate_execution', None)):
            raise NpuEnvironmentError('NPU package must implement validate_execution()')
        public_seed = protocol.get('public_seed', 0)
        if type(public_seed) is not int or public_seed < 0:
            raise ValueError('Invalid fixed public seed')
        public_specs = adapter.cases('public', public_seed)
        hidden_seed = seed if seed is not None else secrets.randbits(63)
        specs = public_specs + (adapter.cases('hidden', hidden_seed) if action == 'final' else [])
        case_ids = [spec['case_id'] for spec in public_specs]
        if not public_specs or len(set(case_ids)) != len(case_ids):
            raise ValueError('Public case list must be nonempty with unique ids')
        report['correctness']['total'] = len(specs)
        if baseline is not None:
            from .calibration import validate_baseline
            from .identity import measurement_engine_digest, protocol_sha256
            validate_baseline(baseline, package_digest=package.digest,
                              environment=report['environment'],
                              reference_sha256=_sha(package.path('reference').read_bytes()),
                              engine_digest=measurement_engine_digest(), protocol_digest=protocol_sha256(package),
                              expected_case_ids=case_ids)
            report['baseline_identity'] = {key: baseline[key] for key in
                                           ('baseline_id', 'source_sha256', 'package_digest')}
            report['baseline_identity']['reference_is_theoretical_optimum'] = False
        try:
            validate_candidate_source(source, package.manifest['candidate_policy'])
        except ProblemError as error:
            report['stages']['build'] = 'failed'
            return finish('compile_failed', stage='submission', category='candidate_source_rejected', message=str(error))
        with _device_lock(protocol['runtime']['device']):
            package.stage_project(source, root / 'project')
            limits = protocol.get('limits', {})
            build_limit = _positive_number(limits.get('build_seconds', 300), 'build_seconds')
            case_limit = _positive_number(limits.get('case_seconds', 120), 'case_seconds')
            profile_limit = _positive_number(limits.get('profile_seconds', 120), 'profile_seconds')
            for index, command in enumerate(package.build_commands(root / 'project', root / 'build')):
                execution = _execute(command, cwd=root / 'project', log=root / ('build-' + str(index) + '.log'),
                                     timeout=build_limit, env=env)
                report['build_commands'].append(execution)
                if not execution['started'] or execution.get('capture_error'):
                    return finish('infrastructure_error', stage='build', category='build_tool_unavailable_or_capture_failed')
                if execution['returncode'] != 0 or execution['timed_out']:
                    report['stages']['build'] = 'failed'
                    report['build_diagnostics'] = (root / execution['log']).read_text(errors='replace')[-16000:].replace(str(root), '<run>')
                    return finish('compile_failed', stage='build', category='build_timeout' if execution['timed_out'] else 'compiler_exit_nonzero')
            binary = package.runner_path(root / 'build')
            if binary.is_symlink() or not binary.is_file() or not os.access(str(binary), os.X_OK):
                return finish('infrastructure_error', stage='build', category='build_contract_missing_executable')
            report['stages']['build'] = 'passed'
            report['binary_sha256'] = _file_sha(binary)
            package.assert_unchanged()
            prepared = {}
            runtime_failed = correctness_failed = False
            repeats_default = protocol['correctness']['repeats']
            if type(repeats_default) is not int or not 1 <= repeats_default <= 1000:
                raise ValueError('Invalid correctness repeat count')
            for index, spec in enumerate(specs):
                hidden = spec.get('suite') == 'hidden'
                judge_dir = root / 'judge' / str(index)
                manifest = adapter.prepare(spec, judge_dir)
                prepared[spec['case_id']] = (judge_dir, manifest)
                target = root / 'candidate' / str(index)
                inputs = target / 'inputs'
                inputs.mkdir(parents=True)
                _inputs_only(manifest, judge_dir, inputs)
                repeats = max(repeats_default, spec.get('repeats', 1))
                if type(repeats) is not int or not 1 <= repeats <= 1000:
                    raise ValueError('Invalid case repeat count')
                output = target / 'output.bin'
                args = adapter.runner_args(spec, inputs, output, mode='correctness', repeat=repeats, warmup=0)
                execution = _execute([str(binary), *args], cwd=target, log=target / 'runtime.log', timeout=case_limit, env=env)
                contract = adapter.validate_execution(spec, _records(target / 'runtime.log') if execution['started'] else [],
                                                      mode='correctness', repeat=repeats, warmup=0)
                item = {'case_id': ('hidden_' + str(index)) if hidden else spec['case_id'],
                        'suite': 'hidden' if hidden else 'public', 'runtime': execution,
                        'correctness': {'passed': False, 'status': 'NOT_RUN'}}
                report['cases'].append(item)
                if not execution['started'] or execution.get('capture_error') or contract.get('infrastructure_failure'):
                    return finish('infrastructure_error', stage='runtime', category='device_or_runner_unavailable')
                if execution['returncode'] != 0 or execution['timed_out'] or not contract['passed']:
                    runtime_failed = True
                    item['error_category'] = 'runtime_timeout' if execution['timed_out'] else 'runner_or_carrier_contract_failed'
                    if not hidden:
                        item['runtime_diagnostics'] = (target / 'runtime.log').read_text(errors='replace')[-8000:].replace(str(root), '<run>')
                    continue
                verification = _verify(adapter, spec, judge_dir, manifest, inputs, output, repeats)
                item['correctness'] = _public_verification(verification, hidden)
                report['correctness']['executed'] += 1
                if verification['passed']:
                    report['correctness']['passed'] += 1
                else:
                    correctness_failed = True
            report['stages']['runtime'] = 'failed' if runtime_failed else 'passed'
            report['stages']['correctness'] = ('failed' if correctness_failed else
                                               'passed' if not runtime_failed else
                                               'incomplete' if report['correctness']['executed'] else 'not_run')
            if runtime_failed:
                return finish('runtime_failed', stage='runtime', category='one_or_more_cases_failed_to_execute')
            if correctness_failed:
                return finish('correctness_failed', stage='correctness', category='independent_oracle_mismatch')
            if action == 'check':
                return finish('passed')
            from .metrics import parse_msprof_csvs, summarize_captures, score_cases
            timing = protocol['timing']
            repeat, warmup, captures = (timing[key] for key in ('measured_calls', 'warmup_calls', 'independent_captures'))
            if any(type(x) is not int for x in (repeat, warmup, captures)) or not (repeat > 0 and warmup >= 0 and captures > 0):
                raise ValueError('Invalid profiling call/capture counts')
            report['metric'] = timing['metric']
            for index, spec in enumerate(public_specs):
                judge_dir, manifest = prepared[spec['case_id']]
                blocks = []
                for capture_index in range(captures):
                    target = root / 'timing' / str(index) / str(capture_index)
                    inputs = target / 'inputs'
                    inputs.mkdir(parents=True)
                    _inputs_only(manifest, judge_dir, inputs)
                    output = target / 'output.bin'
                    args = adapter.runner_args(spec, inputs, output, mode='profile', repeat=repeat, warmup=warmup)
                    command = [shutil.which('msprof', path=env['PATH']), '--output=' + str(target / 'profile'),
                               '--task-time=on', '--ai-core=on', '--aic-metrics=PipeUtilization',
                               '--runtime-api=on', str(binary), *args]
                    execution = _execute(command, cwd=target, log=target / 'profile.log', timeout=profile_limit, env=env)
                    contract = adapter.validate_execution(spec, _records(target / 'profile.log') if execution['started'] else [],
                                                          mode='profile', repeat=repeat, warmup=warmup)
                    if not execution['started'] or execution.get('capture_error') or contract.get('infrastructure_failure'):
                        return finish('infrastructure_error', stage='performance', category='profiler_or_device_unavailable')
                    if execution['returncode'] != 0 or execution['timed_out'] or not contract['passed']:
                        report['stages']['performance'] = 'invalid'
                        report['performance_diagnostics'] = (target / 'profile.log').read_text(errors='replace')[-8000:].replace(str(root), '<run>')
                        return finish('measurement_invalid', stage='performance', category='profiler_or_carrier_contract_failed')
                    verification = _verify(adapter, spec, judge_dir, manifest, inputs, output, repeat + warmup)
                    if not verification['passed']:
                        report['stages']['correctness'] = 'failed'
                        report['profile_correctness_failure'] = {'case_id': spec['case_id'], **_public_verification(verification)}
                        return finish('correctness_failed', stage='performance', category='profile_invocation_output_mismatch')
                    csvs = sorted((target / 'profile').rglob('op_summary*.csv'))
                    if not csvs:
                        csvs = sorted((target / 'profile').rglob('task_time*.csv'))
                    try:
                        parsed = parse_msprof_csvs(csvs, expected_invocations=repeat + warmup, warmups=warmup,
                                                  expected_device_id=protocol['runtime']['device'],
                                                  assume_one_kernel_per_invocation=True)
                    except MeasurementError as error:
                        report['stages']['performance'] = 'invalid'
                        return finish('measurement_invalid', stage='performance', category='msprof_capture_unsupported',
                                      message=str(error).replace(str(root), '<run>'))
                    block = {'execution': execution, 'parsed': parsed, 'measurement_valid': bool(parsed['measurement_valid']),
                             'actual_cores': contract['actual_cores'], 'correctness': _public_verification(verification)}
                    (target / 'capture.json').write_text(json.dumps(block, sort_keys=True, indent=2))
                    blocks.append(block)
                aggregate = summarize_captures([block['parsed'] for block in blocks], expected_captures=captures)
                actual_cores = {block['actual_cores'] for block in blocks}
                if len(actual_cores) != 1 or None in actual_cores:
                    report['stages']['performance'] = 'invalid'
                    return finish('measurement_invalid', stage='performance', category='profile_core_count_changed')
                measurement = {'case_id': spec['case_id'], 'source_sha256': report['source_sha256'],
                               'correctness_passed': True, 'measurement_valid': True, 'metric': timing['metric'],
                               'median_us': aggregate['median_us'], 'capture_medians_us': aggregate['capture_medians_us'],
                               'actual_cores': next(iter(actual_cores)), 'captures': blocks,
                               'input_manifest_sha256': _sha(json.dumps(manifest, sort_keys=True).encode())}
                report['measurements'].append(measurement)
                (root / 'measurements.json').write_text(json.dumps(report['measurements'], sort_keys=True, indent=2))
            package.assert_unchanged()
            report['stages']['performance'] = 'passed'
            if baseline is None:
                report['score_status'] = 'CALIBRATION_REQUIRED'
                report['performance'] = {'metric': timing['metric'], 'cases': report['measurements']}
            else:
                scoring = score_cases(report['measurements'], baseline, source_sha256=report['source_sha256'],
                                      expected_case_ids=case_ids)
                report.update(score=scoring['score'], score_status=scoring['score_status'], scoring=scoring)
                report['performance'] = {'metric': timing['metric'], 'cases': scoring['cases']}
            return finish('passed')
    except MeasurementError as error:
        report['stages']['performance'] = 'invalid'
        return finish('measurement_invalid', stage='performance', category='measurement_or_baseline_comparison_invalid',
                      message=str(error).replace(str(root), '<run>'))
    except NpuEnvironmentError as error:
        return finish('infrastructure_error', stage='environment', category='npu_environment_unavailable', message=str(error))
    except Exception as error:
        # Exception details may contain private oracle data; keep them private.
        if writable:
            (root / 'private_error.txt').write_text(type(error).__name__ + ': ' + str(error))
        return finish('infrastructure_error', stage='judge', category='trusted_judge_or_environment_error',
                      message=type(error).__name__)
