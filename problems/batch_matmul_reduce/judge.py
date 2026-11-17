"""Trusted BatchMatmulMaxSum adapter, independent of the v2 controller.

NumPy and ml_dtypes are needed only for prepare(); metadata loading and binary
output verification use the standard library. Candidate execution receives x1
and x2 only, never case.json, golden.bin, this adapter or the reference kernel.
"""
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import types


ROOT = Path(__file__).resolve().parent
_source = ROOT / 'trusted' / 'cases.py'
_oracle = types.ModuleType(__name__ + '_oracle')
_oracle.__file__ = str(_source)
sys.modules[_oracle.__name__] = _oracle
exec(compile(_source.read_bytes(), str(_source), 'exec'), _oracle.__dict__)
_FIELDS = ('case_id', 'b', 'm', 'n', 'k', 'dtype', 'ta', 'tb', 'seed', 'pattern', 'repeats')


def _spec(spec):
    if not isinstance(spec, dict):
        raise ValueError('Case specification must be an object')
    return _oracle.CaseSpec(**{key: spec[key] for key in _FIELDS})


def cases(suite, seed):
    if suite not in ('public', 'hidden', 'all') or type(seed) is not int or seed < 0:
        raise ValueError('Invalid suite/seed')
    catalog = json.loads((ROOT / 'cases.json').read_text())
    result = []
    for group in ('public', 'hidden'):
        if suite not in ('all', group):
            continue
        for definition in catalog[group]:
            spec = dict(definition)
            material = f'batch-matmul-reduce-v2:{group}:{spec["case_id"]}:{seed}'.encode()
            spec['seed'] = int.from_bytes(hashlib.sha256(material).digest()[:8], 'little')
            spec.update(suite=group, performance=(group == 'public'))
            _spec(spec)
            result.append(spec)
    if not result or len({item['case_id'] for item in result}) != len(result):
        raise ValueError('Empty or duplicate case catalog')
    return result


def prepare(spec, outdir):
    canonical = _spec(spec)
    directory = Path(outdir)
    if directory.is_symlink():
        raise ValueError('Judge case directory must not be a symlink')
    if directory.exists() and any(path.is_symlink() for path in directory.rglob('*')):
        raise ValueError('Symlinks are forbidden in cached judge artifacts')
    try:
        manifest = _oracle.generate_case(canonical, directory)
    except ImportError as error:
        raise RuntimeError('BatchMatmulMaxSum prepare() requires preinstalled numpy and ml_dtypes') from error
    return {
        'case_id': canonical.case_id, 'spec': dict(spec),
        'inputs': ['x1.bin', 'x2.bin'], 'oracle_files': ['golden.bin', 'case.json'],
        'artifacts': {item['file']: {'bytes': item['bytes'], 'sha256': item['sha256']}
                      for item in manifest['artifacts'].values()},
        'output_dtype': 'float32', 'output_elements': canonical.b,
        'golden_source': manifest['reference'],
    }


def runner_args(spec, datadir, output, *, mode='correctness', repeat=1, warmup=0):
    canonical = _spec(spec)
    if mode not in ('correctness', 'profile') or type(repeat) is not int or not 1 <= repeat <= 100000:
        raise ValueError('Invalid execution mode/repeat')
    if type(warmup) is not int or not 0 <= warmup <= 10000:
        raise ValueError('Invalid warmup count')
    runtime = json.loads((ROOT / 'protocol.json').read_text())['runtime']
    return [
        '--x1', str(Path(datadir) / 'x1.bin'), '--x2', str(Path(datadir) / 'x2.bin'),
        '--output', str(output), '--b', str(canonical.b), '--m', str(canonical.m),
        '--n', str(canonical.n), '--k', str(canonical.k), '--dtype', canonical.dtype,
        '--transpose-x1', str(int(canonical.ta)), '--transpose-x2', str(int(canonical.tb)),
        '--device', str(runtime['device']), '--cores', str(runtime['cores']),
        '--mode', mode, '--repeat', str(repeat), '--warmup', str(warmup),
    ]


def validate_execution(spec, records, *, mode, repeat, warmup):
    """Check the fixed carrier's public ABI contract, never use it as an oracle.

    Numerical correctness is independently verified from every binary capture.
    This metadata check remains a development check, not a hostile Host boundary.
    """
    canonical = _spec(spec)
    runtime = json.loads((ROOT / 'protocol.json').read_text())['runtime']
    calls = [record for record in records if record.get('type') == 'call']
    summaries = [record for record in records if record.get('type') == 'summary']
    errors = [record for record in records if record.get('type') in ('error', 'cleanup_error')]
    summary = summaries[0] if len(summaries) == 1 else {}
    cores = summary.get('cores')
    valid = bool(
        not errors and len(summaries) == 1 and summary.get('status') == 'ok'
        and len(calls) == repeat + warmup
        and [record.get('iteration') for record in calls] == list(range(-warmup, repeat))
        and [record.get('warmup') for record in calls] == [i < 0 for i in range(-warmup, repeat)]
        and summary.get('mode') == mode
        and summary.get('shape') == [canonical.b, canonical.m, canonical.n, canonical.k]
        and summary.get('dtype') == (1 if canonical.dtype == 'fp16' else 2)
        and summary.get('device') == runtime['device']
        and summary.get('warmup_calls') == warmup and summary.get('measured_calls') == repeat
        and type(cores) is int and cores > 0
        and (runtime['cores'] == 0 or runtime['cores'] == cores)
        and all(summary.get(key) is True for key in ('input_unchanged', 'guards_ok', 'output_finite')))
    return {'passed': valid, 'actual_cores': cores if valid else None,
            'status': 'PASS' if valid else 'CARRIER_CONTRACT_FAILED',
            'infrastructure_failure': any(record.get('stage') == 'device_init' for record in errors)}


def verify(spec, datadir, output, *, expected_calls=None):
    canonical = _spec(spec)
    directory = Path(datadir)
    manifest = json.loads((directory / 'case.json').read_text())
    if manifest.get('spec') != asdict(canonical):
        raise ValueError('Judge case identity mismatch')
    if manifest.get('generator_sha256') != hashlib.sha256(_source.read_bytes()).hexdigest():
        raise ValueError('Judge generator identity mismatch')
    expected_sizes = {'x1.bin': canonical.b * canonical.m * canonical.k * 2,
                      'x2.bin': canonical.b * canonical.n * canonical.k * 2,
                      'golden.bin': canonical.b * 4}
    for key, filename in (('x1', 'x1.bin'), ('x2', 'x2.bin'), ('golden', 'golden.bin')):
        identity = manifest['artifacts'][key]
        path = directory / filename
        if path.is_symlink() or not path.is_file() or identity['file'] != filename:
            raise ValueError('Invalid judge artifact path')
        raw = path.read_bytes()
        if (len(raw) != expected_sizes[filename] or len(raw) != identity['bytes']
                or hashlib.sha256(raw).hexdigest() != identity['sha256']):
            raise ValueError('Judge artifact identity mismatch')
    golden = struct.unpack('<' + 'f' * canonical.b, (directory / 'golden.bin').read_bytes())
    if any(not math.isfinite(value) for value in golden):
        raise ValueError('Oracle contains nonfinite values')
    actual = Path(output)
    result = _verify_one(golden, actual)
    if not result['passed']:
        return result
    captures = list(actual.parent.glob(actual.name + '.call-*'))
    if expected_calls is not None:
        if type(expected_calls) is not int or expected_calls < 0:
            raise ValueError('Invalid expected call count')
        if len(captures) != expected_calls:
            return {'passed': False, 'status': 'CAPTURE_COUNT_MISMATCH',
                    'expected_calls': expected_calls, 'observed_calls': len(captures)}
    indices = []
    for capture in captures:
        suffix = capture.name[len(actual.name + '.call-'):]
        if not suffix.isdigit():
            return {'passed': False, 'status': 'INVALID_CAPTURE_NAME'}
        indices.append(int(suffix))
        comparison = _verify_one(golden, capture)
        if not comparison['passed']:
            return {**comparison, 'failed_capture': capture.name}
        result['max_abs_error'] = max(result['max_abs_error'], comparison['max_abs_error'])
    if sorted(indices) != list(range(len(indices))):
        return {'passed': False, 'status': 'MISSING_CAPTURE'}
    return {**result, 'checked_captures': 1 + len(captures), 'case_id': canonical.case_id}


def _verify_one(golden, path):
    if path.is_symlink() or not path.is_file():
        return {'passed': False, 'status': 'OUTPUT_MISSING_OR_SYMLINK'}
    raw = path.read_bytes()
    if len(raw) != len(golden) * 4:
        return {'passed': False, 'status': 'OUTPUT_SIZE_MISMATCH'}
    actual = struct.unpack('<' + 'f' * len(golden), raw)
    if any(not math.isfinite(value) for value in actual):
        return {'passed': False, 'status': 'OUTPUT_NONFINITE'}
    errors = [abs(a - b) for a, b in zip(actual, golden)]
    failed = sum(error > 1e-4 + 1e-4 * abs(b) for error, b in zip(errors, golden))
    return {'passed': failed == 0, 'status': 'PASS' if failed == 0 else 'OUTPUT_MISMATCH',
            'checked_elements': len(golden), 'failed_elements': failed,
            'max_abs_error': max(errors), 'atol': 1e-4, 'rtol': 1e-4}
