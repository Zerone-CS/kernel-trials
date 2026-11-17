"""Snapshot a comparison on its configured server and retrieve public evidence.

SSH transports trusted owner configuration (which may contain credentials) over
stdin. Raw configuration, stderr and Agent logs are never echoed. This uses the
same server account and is not an operating-system isolation boundary.
"""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shlex
import signal
import subprocess
import tempfile


_HOST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,252}\Z')
_ID = re.compile(r'[a-f0-9]{32}\Z')
_REPORTS = ('REPORT.md', 'comparison.json', 'trials.json', 'manifest.json')
_OUTPUT_LIMIT = 192 * 1024 * 1024


class RemoteExperimentError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


_UPLOAD = r'''
import base64, fcntl, hashlib, json, os, shutil, stat, sys, tempfile
from pathlib import Path, PurePosixPath
root, digest = Path(sys.argv[1]), sys.argv[2]
if not root.is_dir() or root.is_symlink():
    raise ValueError('Remote project must be a real directory')
payload = json.load(sys.stdin)
if payload['configuration_digest'] != digest or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
    raise ValueError('Invalid snapshot identity')
encoded_plan = json.dumps(payload['plan'], sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf8')
if hashlib.sha256(encoded_plan).hexdigest() != digest:
    raise ValueError('Snapshot plan digest mismatch')
def directory(path):
    try: path.mkdir(mode=0o700)
    except FileExistsError: pass
    if path.is_symlink() or not path.is_dir(): raise ValueError('Unsafe snapshot directory')
directory(root / '.eval')
directory(root / '.eval' / 'experiment-inputs')
base = root / '.eval' / 'experiment-inputs'
base.chmod(0o700)
target = base / digest
fd = os.open(str(base / (digest + '.lock')), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
with os.fdopen(fd, 'r+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    files = {}
    for entry in payload['files']:
        name = entry['path']
        if (not isinstance(name, str) or not name or name.startswith('/') or chr(92) in name or
                any(ord(c) < 32 for c in name) or any(p in ('', '.', '..') for p in name.split('/')) or
                name == '.snapshot.json' or name in files):
            raise ValueError('Invalid snapshot path')
        data = base64.b64decode(entry['content_b64'], validate=True)
        if hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError('Snapshot checksum mismatch')
        files[name] = data
    if 'config.json' not in files or len(files) > 8001 or sum(map(len, files.values())) > 136 * 1024 * 1024:
        raise ValueError('Snapshot missing configuration or exceeds limits')
    identity = {'configuration_digest': digest, 'files': {n: hashlib.sha256(v).hexdigest() for n,v in files.items()}}
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir(): raise ValueError('Unsafe existing snapshot')
        actual = {}
        for path in target.rglob('*'):
            if path.is_symlink(): raise ValueError('Symlink in existing snapshot')
            if path.is_file() and path.relative_to(target).as_posix() != '.snapshot.json':
                actual[path.relative_to(target).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if json.loads((target / '.snapshot.json').read_text()) != identity or actual != identity['files']:
            raise ValueError('Frozen snapshot changed; refusing replacement')
        reused = True
    else:
        temporary = Path(tempfile.mkdtemp(prefix='.upload-', dir=str(base)))
        try:
            for name, data in files.items():
                destination = temporary / name
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with destination.open('xb') as stream: stream.write(data)
                destination.chmod(0o600)
            sys.path.insert(0, str(root))
            from experiment import load_config
            plan, _, _ = load_config(temporary / 'config.json')
            if plan != payload['plan']:
                raise ValueError('Reconstructed method or runner identity changed')
            (temporary / '.snapshot.json').write_text(json.dumps(identity, sort_keys=True))
            (temporary / '.snapshot.json').chmod(0o600)
            temporary.rename(target)
            reused = False
        finally:
            if temporary.exists(): shutil.rmtree(temporary)
print(json.dumps({'uploaded': True, 'reused': reused, 'configuration_digest': digest}))
'''

_RUN = r'''
import json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
settings = json.loads((root / '.eval' / 'launcher.json').read_text())
if 'experiment_remote' in settings:
    raise ValueError('Remote launcher must not delegate experiments recursively')
os.chdir(root)
os.execvp('python3', ['python3', 'launch.py', 'compare', sys.argv[2]] + (['--new'] if sys.argv[3] == 'new' else []))
'''

_DOWNLOAD = r'''
import base64, hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
if root.is_symlink() or not root.is_dir(): raise ValueError('Report directory is unavailable')
files, size = [], 0
for name in ('REPORT.md', 'comparison.json', 'trials.json', 'manifest.json'):
    path = root / name
    if path.is_symlink() or not path.is_file(): raise ValueError('Report is not a regular file')
    data = path.read_bytes()
    size += len(data)
    if size > 136 * 1024 * 1024: raise ValueError('Reports exceed the evidence size limit')
    files.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(),
                  'content_b64': base64.b64encode(data).decode('ascii')})
print(json.dumps({'files': files}))
'''


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf8')


def _entry(name, data):
    return {'path': name, 'sha256': hashlib.sha256(data).hexdigest(),
            'content_b64': base64.b64encode(data).decode('ascii')}


def _snapshot(config_path):
    from experiment import load_config
    plan, methods, runners = load_config(config_path)
    digest = hashlib.sha256(_json_bytes(plan)).hexdigest()
    config = {key: plan[key] for key in ('name', 'track', 'tasks', 'repeats', 'control', 'policy')}
    config.update(schema_version=1, runner={}, conditions=[])
    files = []
    for index, condition in enumerate(plan['conditions']):
        method = methods[condition['id']]
        prefix = 'conditions/' + str(index) + '/resources/'
        tops = set()
        for name, data in method['files'].items():
            if (not name or name.startswith('/') or chr(92) in name or
                    any(ord(c) < 32 for c in name) or any(part in ('', '.', '..') for part in name.split('/'))):
                raise RemoteExperimentError('INVALID_METHOD_PATH', 'Method resource paths must be canonical relative paths')
            tops.add(name.split('/')[0])
            files.append(_entry(prefix + name, data))
        # Each top-level basename remains the same when load_config reconstructs
        # the resources, preserving the original method digest exactly.
        config['conditions'].append({'id': condition['id'], 'label': condition['label'],
            'instructions': method['instructions'], 'resources': [prefix + name for name in sorted(tops)],
            'runner': runners[condition['id']]})
    files.append(_entry('config.json', _json_bytes(config)))
    return {'configuration_digest': digest, 'plan': plan, 'files': files}


def _progress(line):
    try:
        item = json.loads(line)
    except (ValueError, UnicodeError):
        return
    if not isinstance(item, dict) or item.get('event') not in ('trial_started', 'trial_finished'):
        return
    allowed = ('event', 'trial_id', 'task_id', 'condition_id', 'repeat_index', 'attempt_index', 'report_path')
    event = {key: item[key] for key in allowed if key in item and type(item[key]) in (str, int, bool)}
    event['execution_location'] = 'remote'
    print(json.dumps(event, ensure_ascii=False), flush=True)


def _private_diagnostics(stream):
    """Keep bounded stderr for the owner; expose only safe error categories."""
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - 1024 * 1024))
    data = stream.read()
    descriptor, path = tempfile.mkstemp(prefix='op-eval-ssh-', suffix='.stderr.log')
    with os.fdopen(descriptor, 'wb') as log:
        log.write(data)
    result = {'stderr_path': path}
    text = data.decode('utf8', errors='replace')
    if re.search(r'(?m)^ModuleNotFoundError:', text):
        result['diagnostic_code'] = 'REMOTE_DEPENDENCY_MISSING'
    elif re.search(r'(?m)^ImportError:', text):
        result['diagnostic_code'] = 'REMOTE_IMPORT_FAILED'
    elif 'Permission denied (' in text:
        result['diagnostic_code'] = 'SSH_AUTHENTICATION_FAILED'
    elif 'Could not resolve hostname ' in text:
        result['diagnostic_code'] = 'SSH_HOST_NOT_FOUND'
    return result


def _ssh_transport(host, argv, *, input_bytes=None, stream=False):
    """No local shell; SSH's remote shell sees individually quoted arguments.

    A comparison has no fixed wall-clock limit here. Its own trial budgets and
    process supervision control execution. Partial stdout is streamed promptly.
    """
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=30',
               '-o', 'ServerAliveCountMax=3', host, ' '.join(shlex.quote(str(arg)) for arg in argv)]
    with tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=stderr, shell=False, start_new_session=True)
        except OSError:
            raise RemoteExperimentError('REMOTE_TRANSPORT_FAILED', 'Could not start SSH; check the configured host and SSH installation') from None
        try:
            if input_bytes is not None:
                stdout, _ = process.communicate(input_bytes)
            else:
                chunks, partial, size = [], b'', 0
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        for key, _ in selector.select(timeout=0.5):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            chunks.append(chunk)
                            size += len(chunk)
                            if size > _OUTPUT_LIMIT:
                                raise RemoteExperimentError('REMOTE_OUTPUT_TOO_LARGE', 'Remote output exceeds the evidence transport limit')
                            if stream:
                                partial += chunk
                                lines = partial.split(b'\n')
                                partial = lines.pop()
                                for line in lines:
                                    _progress(line)
                    if stream and partial:
                        _progress(partial)
                stdout = b''.join(chunks)
                process.wait()
            if len(stdout) > _OUTPUT_LIMIT:
                raise RemoteExperimentError('REMOTE_OUTPUT_TOO_LARGE', 'Remote output exceeds the evidence transport limit')
            result = {'returncode': process.returncode, 'stdout': stdout}
            if process.returncode != 0:
                result.update(_private_diagnostics(stderr))
            return result
        finally:
            if process.poll() is None:
                # Closing SSH is not proof the remote driver stopped. Its saved
                # trial marker deliberately prevents spawning a duplicate Agent.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            if process.stdout:
                process.stdout.close()


def _response(result, phase):
    if result.get('returncode') != 0:
        diagnostics = ''
        if result.get('diagnostic_code') == 'REMOTE_DEPENDENCY_MISSING':
            diagnostics = ' The remote project is missing a Python dependency; deploy the complete evaluator project.'
        elif result.get('diagnostic_code') == 'REMOTE_IMPORT_FAILED':
            diagnostics = ' The remote project could not import its Python dependencies.'
        elif result.get('diagnostic_code') == 'SSH_AUTHENTICATION_FAILED':
            diagnostics = ' SSH authentication failed.'
        elif result.get('diagnostic_code') == 'SSH_HOST_NOT_FOUND':
            diagnostics = ' The configured SSH hostname could not be resolved.'
        if result.get('stderr_path'):
            diagnostics += ' Private SSH diagnostics: ' + str(result['stderr_path']) + '.'
        raise RemoteExperimentError('REMOTE_' + phase.upper() + '_FAILED',
            'Remote ' + phase + ' failed (SSH exit ' + str(result.get('returncode')) +
            ').' + diagnostics + ' Inspect the server state. Retry without --new to recover the saved experiment; '
            'requesting --new after an uncertain completion can create another experiment.')
    data = result.get('stdout', b'')
    if isinstance(data, bytes):
        data = data.decode('utf8', errors='replace')
    for line in reversed(data.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and 'event' not in value:
            return value
    raise RemoteExperimentError('REMOTE_INVALID_RESPONSE', 'Remote ' + phase + ' returned no valid completion object')


def run_remote_experiment(project_root, config_path, remote, *, new=False):
    from launch import _safe_path, _save
    root = Path(project_root).resolve(strict=True)
    if (not isinstance(remote, dict) or set(remote) != {'ssh_host', 'project_root'} or
            not isinstance(remote.get('ssh_host'), str) or not _HOST.fullmatch(remote['ssh_host'])):
        raise RemoteExperimentError('REMOTE_CONFIG_INVALID', 'Remote settings require ssh_host and an absolute project_root')
    supplied_root = remote['project_root']
    if (not isinstance(supplied_root, str) or not supplied_root.startswith('/') or
            any(ord(c) < 32 for c in supplied_root) or '..' in supplied_root.split('/')):
        raise RemoteExperimentError('REMOTE_CONFIG_INVALID', 'Remote project_root must be an absolute POSIX path without traversal')
    remote_root = PurePosixPath(supplied_root)
    if remote_root == PurePosixPath('/'):
        raise RemoteExperimentError('REMOTE_CONFIG_INVALID', 'Remote project_root cannot be the filesystem root')
    config_path = Path(config_path)
    if not config_path.is_absolute():
        config_path = root / config_path
    snapshot = _snapshot(config_path)
    digest = snapshot['configuration_digest']
    uploaded = _response(_ssh_transport(remote['ssh_host'], ['python3', '-c', _UPLOAD, str(remote_root), digest],
                                         input_bytes=_json_bytes(snapshot)), 'upload')
    if uploaded.get('uploaded') is not True or uploaded.get('configuration_digest') != digest:
        raise RemoteExperimentError('REMOTE_INVALID_RESPONSE', 'Remote snapshot identity does not match the requested configuration')
    remote_config = remote_root / '.eval' / 'experiment-inputs' / digest / 'config.json'
    completed = _response(_ssh_transport(remote['ssh_host'],
        ['python3', '-c', _RUN, str(remote_root), str(remote_config), 'new' if new else 'resume'], stream=True), 'experiment')
    experiment_id = completed.get('experiment_id')
    if not isinstance(experiment_id, str) or not _ID.fullmatch(experiment_id):
        raise RemoteExperimentError('REMOTE_INVALID_RESPONSE', 'Remote completion has an invalid experiment identity')
    remote_directory = remote_root.parent / 'op_eval_experiment_runs' / experiment_id
    if (completed.get('report_path') != str(remote_directory / 'REPORT.md') or
            completed.get('json_path') != str(remote_directory / 'comparison.json')):
        raise RemoteExperimentError('REMOTE_INVALID_RESPONSE', 'Remote report paths do not match the experiment identity')
    downloaded = _response(_ssh_transport(remote['ssh_host'],
        ['python3', '-c', _DOWNLOAD, str(remote_directory)]), 'download')
    entries = downloaded.get('files')
    if not isinstance(entries, list) or len(entries) != len(_REPORTS):
        raise RemoteExperimentError('REMOTE_INVALID_EVIDENCE', 'Remote evidence bundle is incomplete')
    files = {}
    try:
        for entry in entries:
            name = entry['path']
            if name not in _REPORTS or name in files:
                raise ValueError('unexpected evidence path')
            data = base64.b64decode(entry['content_b64'], validate=True)
            if hashlib.sha256(data).hexdigest() != entry['sha256']:
                raise ValueError('evidence checksum mismatch')
            files[name] = data
        if json.loads(files['manifest.json'])['experiment_id'] != experiment_id:
            raise ValueError('manifest identity mismatch')
        json.loads(files['comparison.json'])
        json.loads(files['trials.json'])
    except (ValueError, KeyError, TypeError):
        raise RemoteExperimentError('REMOTE_INVALID_EVIDENCE', 'Remote reports failed completeness, checksum or identity validation') from None
    destination = _safe_path(root, '.eval/remote-experiments/' + experiment_id)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    # Stage the complete validated bundle before replacing previous local copies.
    with tempfile.TemporaryDirectory(prefix='.evidence-', dir=str(destination.parent)) as temporary:
        for name, data in files.items():
            staged = Path(temporary) / name
            staged.write_bytes(data)
            staged.chmod(0o600)
        for name in _REPORTS:
            os.replace(str(Path(temporary) / name), str(_safe_path(destination, name)))
    result = {key: completed[key] for key in ('experiment_id', 'trial_count', 'completed_trials', 'reused') if key in completed}
    result.update(report_path=str(destination / 'REPORT.md'), json_path=str(destination / 'comparison.json'),
        manifest_path=str(destination / 'manifest.json'), trials_path=str(destination / 'trials.json'),
        remote_report_path=str(remote_directory / 'REPORT.md'), remote_json_path=str(remote_directory / 'comparison.json'),
        remote_project_root=str(remote_root), remote_ssh_host=remote['ssh_host'],
        configuration_digest=digest, execution_location='remote', trial_report_paths_are_remote=True)
    _save(destination / 'remote_origin.json', result)
    return result
