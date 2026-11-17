"""Trusted CPU fixture adapter. Oracle files stay on the judge side."""
from pathlib import Path
import hashlib
import json
import math
import random
import struct


ROOT = Path(__file__).resolve().parent


def _float32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def _spec(spec):
    if not isinstance(spec, dict) or not isinstance(spec.get('case_id'), str):
        raise ValueError('Invalid case specification')
    if type(spec.get('n')) is not int or not 1 <= spec['n'] <= 1048576:
        raise ValueError('Invalid vector length')
    if spec.get('pattern') not in ('random', 'signed') or type(spec.get('seed')) is not int:
        raise ValueError('Invalid input pattern/seed')
    return spec


def cases(suite, seed):
    if suite not in ('public', 'hidden', 'all') or type(seed) is not int or seed < 0:
        raise ValueError('Invalid suite/seed')
    definitions = json.loads((ROOT / 'cases.json').read_text())
    result = []
    for group in ('public', 'hidden'):
        if suite not in (group, 'all'):
            continue
        for item in definitions[group]:
            item = dict(item)
            material = f'vector-add-v1:{group}:{item["case_id"]}:{seed}'.encode()
            item['seed'] = int.from_bytes(hashlib.sha256(material).digest()[:8], 'little')
            item['suite'] = group
            result.append(_spec(item))
    if len({item['case_id'] for item in result}) != len(result):
        raise ValueError('Duplicate case identity')
    return result


def _write(directory, name, contents):
    path = directory / name
    if path.is_symlink() or (path.exists() and path.read_bytes() != contents):
        raise ValueError('Refusing mismatched or symlinked cached case artifact')
    if not path.exists():
        path.write_bytes(contents)
    return {'bytes': len(contents), 'sha256': hashlib.sha256(contents).hexdigest()}


def prepare(spec, outdir):
    spec = _spec(spec)
    directory = Path(outdir)
    if directory.is_symlink():
        raise ValueError('Case directory must not be a symlink')
    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(spec['seed'])
    x = [_float32(rng.uniform(-8, 8)) for _ in range(spec['n'])]
    if spec['pattern'] == 'signed':
        z = [_float32(-value + rng.uniform(-0.01, 0.01)) for value in x]
    else:
        z = [_float32(rng.uniform(-8, 8)) for _ in range(spec['n'])]
    expected = [_float32(a + b) for a, b in zip(x, z)]
    inputs = struct.pack('<' + 'f' * (2 * spec['n']), *(x + z))
    golden = struct.pack('<' + 'f' * spec['n'], *expected)
    artifacts = {'input.bin': _write(directory, 'input.bin', inputs),
                 'golden.bin': _write(directory, 'golden.bin', golden)}
    manifest = {'case_id': spec['case_id'], 'spec': dict(spec), 'inputs': ['input.bin'],
                'oracle_files': ['golden.bin'], 'artifacts': artifacts,
                'output_dtype': 'float32', 'output_elements': spec['n']}
    _write(directory, 'manifest.json', (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode())
    return manifest


def runner_args(spec, datadir, output, *, mode='correctness', repeat=1, warmup=0):
    spec = _spec(spec)
    if mode not in ('correctness', 'profile') or type(repeat) is not int or not 1 <= repeat <= 100000:
        raise ValueError('Invalid runner mode/repeat')
    if type(warmup) is not int or not 0 <= warmup <= 10000:
        raise ValueError('Invalid runner warmup')
    return ['--input', str(Path(datadir) / 'input.bin'), '--output', str(output), '--n', str(spec['n']),
            '--mode', mode, '--repeat', str(repeat), '--warmup', str(warmup)]


def verify(spec, datadir, output, *, expected_calls=None):
    """Judge-owned bytes decide correctness; runner JSON is never an oracle."""
    spec = _spec(spec)
    directory = Path(datadir)
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest['spec'] != spec:
        raise ValueError('Oracle case identity mismatch')
    for name, identity in manifest['artifacts'].items():
        data = (directory / name).read_bytes()
        if len(data) != identity['bytes'] or hashlib.sha256(data).hexdigest() != identity['sha256']:
            raise ValueError('Judge case artifact hash mismatch')
    actual_file = Path(output)
    result = _verify_one(spec, directory, actual_file)
    if not result['passed']:
        return result
    # Correctness-mode carrier writes every invocation, including warmups.
    # Check these independent bytes as well, so a correct last invocation does
    # not hide an ordinary intermittent implementation error. Their existence
    # is not an anti-cheating boundary; the controller must enforce run counts.
    captures = list(actual_file.parent.glob(actual_file.name + '.call-*'))
    if expected_calls is not None:
        if type(expected_calls) is not int or expected_calls < 0:
            raise ValueError('Invalid expected call count')
        if len(captures) != expected_calls:
            return {'passed': False, 'status': 'CAPTURE_COUNT_MISMATCH',
                    'expected_calls': expected_calls, 'observed_calls': len(captures)}
    indexes = []
    for capture in captures:
        suffix = capture.name[len(actual_file.name + '.call-'):]
        if not suffix.isdigit():
            return {'passed': False, 'status': 'INVALID_CAPTURE_NAME'}
        indexes.append(int(suffix))
        captured = _verify_one(spec, directory, capture)
        if not captured['passed']:
            return {**captured, 'failed_capture': capture.name}
        result['max_abs_error'] = max(result['max_abs_error'], captured['max_abs_error'])
    if sorted(indexes) != list(range(len(indexes))):
        return {'passed': False, 'status': 'MISSING_CAPTURE'}
    return {**result, 'checked_captures': len(captures) + 1}


def _verify_one(spec, directory, actual_file):
    if not actual_file.is_file() or actual_file.is_symlink():
        return {'passed': False, 'status': 'OUTPUT_MISSING_OR_SYMLINK'}
    raw = actual_file.read_bytes()
    if len(raw) != spec['n'] * 4:
        return {'passed': False, 'status': 'OUTPUT_SIZE_MISMATCH'}
    actual = struct.unpack('<' + 'f' * spec['n'], raw)
    golden = struct.unpack('<' + 'f' * spec['n'], (directory / 'golden.bin').read_bytes())
    if any(not math.isfinite(value) for value in actual):
        return {'passed': False, 'status': 'OUTPUT_NONFINITE'}
    errors = [abs(a - b) for a, b in zip(actual, golden)]
    bad = sum(error > 1e-6 + 1e-6 * abs(b) for error, b in zip(errors, golden))
    return {'passed': bad == 0, 'status': 'PASS' if bad == 0 else 'OUTPUT_MISMATCH',
            'checked_elements': spec['n'], 'failed_elements': bad, 'max_abs_error': max(errors)}
