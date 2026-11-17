"""Administrator-bound launchers allocate auditable attempts, never reset them.

Launcher tokens may open only their frozen task/policy and either predefined
track. An active attempt is reused; terminal attempts remain permanently in
history. Allocation is journaled before session creation. A crash between those
steps is deliberately fail-closed and requires administrator inspection.

Launcher credentials are hash-only. Session bearer credentials must be retained
in the judge's private allocation table so an authorized launcher can recover
the same session. They are never included in launcher history. Therefore this
module requires an owner-only node-local database directory and tightens only
its own database/journal files, never a shared parent directory.
"""
import base64
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid

from .budget import Policy
from .problems import load_problem


_REQUEST = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_TRACKS = ('from_scratch', 'optimize')
_ACTIVE = {'PENDING', 'OPEN', 'FINALIZING'}
_TERMINAL = {'FINISHED', 'CANCELLED', 'EXPIRED'}
_THREAD_LOCKS = {}
_LOCK_GUARD = threading.Lock()


class LaunchError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Launcher:
    def __init__(self, controller):
        self.controller = controller
        self.database = controller.store.db_path
        self._secure_database()
        self.lock_path = self.database.with_name(self.database.name + '.launch.lock')
        with self._serialized(), self._connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS launchers (
                    launcher_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
                    package_digest TEXT NOT NULL, baseline_id TEXT,
                    owner_id TEXT NOT NULL, policy_json TEXT NOT NULL,
                    evaluation_purpose TEXT NOT NULL, mode TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS launch_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    launcher_id TEXT NOT NULL REFERENCES launchers(launcher_id),
                    attempt_index INTEGER NOT NULL, track TEXT NOT NULL,
                    allocation_state TEXT NOT NULL,
                    session_id TEXT REFERENCES sessions(session_id),
                    session_token TEXT, files_json TEXT, failure_type TEXT,
                    created_at REAL NOT NULL, ready_at REAL,
                    UNIQUE(launcher_id, attempt_index)
                );
                CREATE TABLE IF NOT EXISTS launch_requests (
                    launcher_id TEXT NOT NULL REFERENCES launchers(launcher_id),
                    request_id TEXT NOT NULL, track TEXT NOT NULL,
                    attempt_id TEXT NOT NULL REFERENCES launch_attempts(attempt_id),
                    created_at REAL NOT NULL,
                    PRIMARY KEY(launcher_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS launch_attempt_track
                    ON launch_attempts(launcher_id, track, attempt_index);
            ''')

    def _secure_database(self):
        directory = self.database.parent.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid()
                or directory.st_mode & 0o077):
            raise LaunchError('LAUNCH_DISABLED', 'Launcher credentials require an owner-only node-local database directory')
        for suffix in ('', '-wal', '-shm', '-journal'):
            path = self.database.with_name(self.database.name + suffix)
            try:
                descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            except FileNotFoundError:
                if not suffix:
                    raise LaunchError('LAUNCH_DISABLED', 'Authoritative database is unavailable')
                continue
            try:
                identity = os.fstat(descriptor)
                if not stat.S_ISREG(identity.st_mode) or identity.st_uid != os.geteuid():
                    raise LaunchError('LAUNCH_DISABLED', 'Database and journal files must belong to the judge account')
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(str(self.database), timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=30000')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _serialized(self):
        import fcntl
        key = str(self.database.resolve())
        with _LOCK_GUARD:
            lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
        with lock:
            descriptor = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                identity = os.fstat(descriptor)
                if not stat.S_ISREG(identity.st_mode) or identity.st_uid != os.geteuid():
                    raise LaunchError('LAUNCH_DISABLED', 'Launcher lock must belong to the judge account')
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)

    def _mode(self):
        if self.controller.mode != 'development':
            raise LaunchError('LAUNCH_DISABLED', 'This launcher supports development evaluation only')

    @staticmethod
    def _authenticate(db, launcher_id, token):
        row = db.execute('SELECT * FROM launchers WHERE launcher_id=?', (launcher_id,)).fetchone()
        supplied = hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) else ''
        expected = row['token_hash'] if row else '0' * 64
        valid = secrets.compare_digest(supplied, expected)
        if not row or not token or not valid:
            raise LaunchError('AUTH_FAILED', 'Unknown launcher or invalid launcher token')
        return dict(row)

    def admin_register(self, package_path, *, owner_id, policy=None, evaluation_purpose='agent_evaluation'):
        """Administrator-only: freeze package/baseline/policy before any launch."""
        self._mode()
        if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 256:
            raise ValueError('owner_id must be a nonempty string of at most 256 characters')
        if evaluation_purpose not in ('agent_evaluation', 'entry_smoke'):
            raise ValueError('Unknown evaluation purpose')
        policy = policy if policy is not None else Policy()
        if not isinstance(policy, Policy):
            raise ValueError('policy must be a Policy instance')
        with self._serialized():
            package = load_problem(package_path)
            baseline = self.controller._baseline(package)
            registered = self.controller.root / 'packages' / package.digest
            if not registered.exists():
                registered.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(package.root, registered)
            load_problem(registered, expected_digest=package.digest)
            baseline_id = baseline['baseline_id'] if baseline else None
            if baseline:
                target = self.controller.root / 'baselines' / (baseline_id + '.json')
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with target.open('x') as stream:
                        json.dump(baseline, stream, sort_keys=True, allow_nan=False)
                    target.chmod(0o600)
                except FileExistsError:
                    if json.loads(target.read_text()) != baseline:
                        raise ValueError('Registered baseline bytes differ from the existing immutable identity')
            launcher_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
            with self._connection() as db:
                db.execute('''INSERT INTO launchers
                    (launcher_id,token_hash,package_digest,baseline_id,owner_id,policy_json,
                     evaluation_purpose,mode,created_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                           (launcher_id, hashlib.sha256(token.encode()).hexdigest(), package.digest,
                            baseline_id, owner_id, _json(asdict(policy)), evaluation_purpose,
                            self.controller.mode, time.time()))
        return {'launcher_id': launcher_id, 'token': token, 'problem_digest': package.digest,
                'baseline_id': baseline_id, 'tracks': list(_TRACKS), 'mode': self.controller.mode,
                'policy': asdict(policy), 'evaluation_purpose': evaluation_purpose}

    @staticmethod
    def _ready(attempt):
        if attempt['allocation_state'] != 'READY':
            raise LaunchError('LAUNCH_INCOMPLETE', 'A prior allocation is incomplete; administrator inspection is required')

    def _response(self, launcher_id, attempt, *, reused):
        self._ready(attempt)
        status = self.controller.store.status(attempt['session_id'], attempt['session_token'])
        return {'launcher_id': launcher_id, 'session_id': attempt['session_id'], 'track': attempt['track'],
                'attempt_index': attempt['attempt_index'], 'session': status,
                'credentials': {'session_id': attempt['session_id'], 'token': attempt['session_token']},
                'files': json.loads(attempt['files_json']), 'reused': reused}

    def _bundle(self, credentials):
        from workspace import export_workspace
        with tempfile.TemporaryDirectory(prefix='op-eval-launch-') as temporary:
            directory = Path(temporary) / 'workspace'
            export_workspace(self.controller, credentials, directory, 'http://127.0.0.1:0')
            files = []
            for path in sorted(directory.rglob('*')):
                if path.is_symlink():
                    raise ValueError('Exported workspace must not contain symlinks')
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise ValueError('Exported workspace must contain regular files only')
                relative = path.relative_to(directory).as_posix()
                if relative == '.eval/session.json':
                    continue
                data = path.read_bytes()
                files.append({'path': relative, 'content_b64': base64.b64encode(data).decode('ascii'),
                              'sha256': hashlib.sha256(data).hexdigest(),
                              'executable': bool(path.stat().st_mode & 0o111)})
            return files

    def open(self, launcher_id, token, *, track, request_id):
        self._mode()
        if track not in _TRACKS:
            raise ValueError('track must be from_scratch or optimize')
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            raise ValueError('request_id must be 1-128 ASCII letters/digits/_.:-')
        with self._serialized():
            with self._connection() as db:
                launcher = self._authenticate(db, launcher_id, token)
                previous = db.execute('''SELECT a.*,r.track AS request_track FROM launch_requests r
                    JOIN launch_attempts a ON a.attempt_id=r.attempt_id
                    WHERE r.launcher_id=? AND r.request_id=?''', (launcher_id, request_id)).fetchone()
                latest = db.execute('''SELECT * FROM launch_attempts WHERE launcher_id=? AND track=?
                    ORDER BY attempt_index DESC LIMIT 1''', (launcher_id, track)).fetchone()
            if launcher['mode'] != self.controller.mode:
                raise LaunchError('LAUNCH_DISABLED', 'Launcher mode differs from the server mode')
            if previous:
                if previous['request_track'] != track:
                    raise LaunchError('REQUEST_CONFLICT', 'Request ID was already assigned to another track')
                return self._response(launcher_id, previous, reused=True)
            if latest:
                self._ready(latest)
                status = self.controller.store.status(latest['session_id'], latest['session_token'])
                if status['state'] in _ACTIVE:
                    with self._connection() as db:
                        db.execute('INSERT INTO launch_requests VALUES (?,?,?,?,?)',
                                   (launcher_id, request_id, track, latest['attempt_id'], time.time()))
                    return self._response(launcher_id, latest, reused=True)
                if status['state'] not in _TERMINAL:
                    raise LaunchError('LAUNCH_INCOMPLETE', 'Unexpected session state; administrator inspection is required')
            # Journal first, commit, then call Controller. Never hold a SQLite
            # transaction across admin_create(), which uses its own transaction.
            with self._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    index = db.execute('SELECT COALESCE(MAX(attempt_index),0)+1 FROM launch_attempts WHERE launcher_id=?',
                                       (launcher_id,)).fetchone()[0]
                    attempt_id = uuid.uuid4().hex
                    now = time.time()
                    db.execute('''INSERT INTO launch_attempts
                        (attempt_id,launcher_id,attempt_index,track,allocation_state,created_at)
                        VALUES (?,?,?,?,'ALLOCATING',?)''', (attempt_id, launcher_id, index, track, now))
                    db.execute('INSERT INTO launch_requests VALUES (?,?,?,?,?)',
                               (launcher_id, request_id, track, attempt_id, now))
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            try:
                package_path = self.controller.root / 'packages' / launcher['package_digest']
                load_problem(package_path, expected_digest=launcher['package_digest'])
                baseline_path = (self.controller.root / 'baselines' / (launcher['baseline_id'] + '.json')
                                 if launcher['baseline_id'] else None)
                credentials = self.controller.admin_create(
                    package_path, track=track, owner_id=launcher['owner_id'],
                    policy=Policy(**json.loads(launcher['policy_json'])), baseline_path=baseline_path,
                    evaluation_purpose=launcher['evaluation_purpose'])
                # Persist credentials immediately so interrupted export remains
                # diagnosable; it still cannot be reused until READY is committed.
                with self._connection() as db:
                    db.execute('UPDATE launch_attempts SET session_id=?,session_token=? WHERE attempt_id=?',
                               (credentials['session_id'], credentials['token'], attempt_id))
                    metadata = db.execute('SELECT claimed_agent_metadata_json FROM sessions WHERE session_id=?',
                                          (credentials['session_id'],)).fetchone()[0]
                    metadata = dict(json.loads(metadata), launcher_id=launcher_id, attempt_index=index)
                    db.execute('UPDATE sessions SET claimed_agent_metadata_json=? WHERE session_id=?',
                               (_json(metadata), credentials['session_id']))
                files = self._bundle(credentials)
                with self._connection() as db:
                    db.execute('''UPDATE launch_attempts SET allocation_state='READY',files_json=?,ready_at=?
                        WHERE attempt_id=?''', (_json(files), time.time(), attempt_id))
                    attempt = db.execute('SELECT * FROM launch_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
                return self._response(launcher_id, attempt, reused=False)
            except Exception as error:
                with self._connection() as db:
                    db.execute('UPDATE launch_attempts SET failure_type=? WHERE attempt_id=?',
                               (type(error).__name__, attempt_id))
                raise LaunchError('LAUNCH_INCOMPLETE', 'Allocation was interrupted; administrator inspection is required') from error

    def history(self, launcher_id, token):
        """Authenticated read-only history; no tokens, files or reference data."""
        from .reporting import _score
        with self._connection() as db:
            self._authenticate(db, launcher_id, token)
            rows = db.execute('SELECT * FROM launch_attempts WHERE launcher_id=? ORDER BY attempt_index',
                              (launcher_id,)).fetchall()
        attempts = []
        for row in rows:
            item = {'session_id': row['session_id'], 'track': row['track'], 'attempt_index': row['attempt_index'],
                    'state': 'ALLOCATING', 'score': None, 'outcome': 'launch_incomplete'}
            if row['allocation_state'] == 'READY':
                status = self.controller.store.status(row['session_id'], row['session_token'])
                final = status.get('final_report') or {}
                item.update(state=status['state'], score=_score(final), outcome=final.get('outcome'))
            attempts.append(item)
        return {'launcher_id': launcher_id, 'attempts': attempts}
