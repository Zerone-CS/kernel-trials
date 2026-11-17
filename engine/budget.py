"""Authoritative, transactional experiment budgets (Python 3.9, stdlib only).

Only the trusted service may call ``admin_create_session``,
``complete_operation`` or ``admin_cancel_session``.  Do not expose those methods
on the candidate-facing API.  This library is a state machine, not an OS
security boundary: an agent with database/service-account access can bypass it.

Creating a session leaves it PENDING. ``start_session`` obtains its resource
lease and starts elapsed SERVER WALL time, including agent thinking, coding,
documentation search, compilation and execution. There is no internal queue:
an occupied resource rejects start without starting the clock. One pending
operation per session prevents parallel evaluation. Either the time deadline
or the last permitted feedback reservation freezes new sources immediately.
``stop_at`` records the earlier event. Final grace starts at ``stop_at`` unless
an accepted feedback is still pending, in which case it starts when the trusted
worker records that feedback's completion. During that wait ``final_deadline``
and ``final_grace_started_at`` are null, and no new feedback/final can execute.
This preserves a full grace window after the last result without permitting
code changes after budget exhaustion. The service MUST enforce worker timeouts
and complete crashed operations with failure reports; this database does not
supervise processes. Final then accepts only an already submitted source hash,
even if that source failed compilation/correctness. A final admitted on time
may finish after the grace window, subject to the worker's own timeout.

The service must hash the actual submitted bytes before reserving them, then
durably snapshot the admitted bytes before executing the worker. A crash in
that gap is an invalid lost-worker result; its reservation remains spent and
must not be replayed as another execution or silently retried.
An admitted reservation spends a feedback slot even if compilation fails, the
worker crashes, or no result is produced.  The caller executes work ONLY when
``reserve(...)["created"]`` is true.  Retries with the same request ID return the
original operation, never another permission to execute it.  Reports passed to
``complete_operation`` must be trusted, candidate-safe judge results; do not
store secret golden data in a report returned to the candidate.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import time
import threading
from typing import Any, Callable, Dict, Mapping, Optional
import uuid


_SHA = re.compile(r"[a-f0-9]{64}\Z")
_REQUEST = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_TRACKS = frozenset(("from_scratch", "optimize"))
_ACTIONS = frozenset(("check", "bench", "final"))
# In-process capability for the trusted deadline finalizer. It cannot be
# represented by a JSON/header value and is never exported to candidate files.
_ADMIN_FINALIZER = object()
_CONNECTION_LOCKS = {}
_CONNECTION_LOCKS_GUARD = threading.Lock()


def network_filesystem(path, mountinfo=None):
    """Detect known network mounts on Linux before enabling SQLite WAL."""
    if mountinfo is None:
        mount_file = Path("/proc/self/mountinfo")
        if not mount_file.exists():
            return None
        mountinfo = mount_file.read_text()
    selected = (0, None)
    for line in mountinfo.splitlines():
        left, separator, right = line.partition(" - ")
        fields, filesystem = left.split(), right.split()
        if not separator or len(fields) < 5 or not filesystem:
            continue
        mount = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        try:
            Path(path).relative_to(mount)
        except ValueError:
            continue
        if len(mount) > selected[0]:
            selected = (len(mount), filesystem[0])
    return selected[1] if selected[1] in ("nfs", "nfs4", "cifs", "smb3") else None


class BudgetError(ValueError):
    """Expected denial; ``code`` is stable for API clients."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Policy:
    max_feedback: int = 20
    active_seconds: int = 3600
    final_grace_seconds: int = 600

    def __post_init__(self):
        for name in ("max_feedback", "active_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.final_grace_seconds) is not int or self.final_grace_seconds < 0:
            raise ValueError("final_grace_seconds must be a nonnegative integer")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA256 digest")
    return value


class BudgetStore:
    def __init__(self, db_path, *, clock: Callable[[], float] = time.time):
        self.db_path = Path(db_path).resolve()
        if network_filesystem(self.db_path):
            raise ValueError("SQLite WAL budget state requires node-local storage; use --database with a local disk path, not NFS")
        with _CONNECTION_LOCKS_GUARD:
            self._connection_lock = _CONNECTION_LOCKS.setdefault(str(self.db_path), threading.RLock())
        self.clock = clock
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    problem_digest TEXT NOT NULL,
                    track TEXT NOT NULL,
                    claimed_agent_metadata_json TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    max_feedback INTEGER NOT NULL CHECK(max_feedback > 0),
                    feedback_used INTEGER NOT NULL DEFAULT 0 CHECK(feedback_used >= 0),
                    created_at REAL NOT NULL,
                    resource_id TEXT NOT NULL,
                    started_at REAL,
                    feedback_deadline REAL,
                    final_deadline REAL,
                    stop_at REAL,
                    budget_stop_reason TEXT,
                    final_grace_started_at REAL,
                    last_seen_at REAL NOT NULL,
                    state TEXT NOT NULL,
                    final_operation_id TEXT,
                    pending_operation_id TEXT,
                    closed_at REAL,
                    cancel_reason TEXT,
                    final_report_json TEXT
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    request_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reserved_at REAL NOT NULL,
                    completed_at REAL,
                    report_json TEXT,
                    UNIQUE(session_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS operations_session
                    ON operations(session_id, reserved_at);
                CREATE UNIQUE INDEX IF NOT EXISTS one_final_per_session
                    ON operations(session_id) WHERE action='final';
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_operation_per_session
                    ON operations(session_id) WHERE state='RESERVED';
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_resource
                    ON sessions(resource_id) WHERE state IN ('OPEN','FINALIZING');
            """)
            # These nullable columns extend the initial, unreleased v2 schema.
            # Serialize introspection and ALTERs across service processes.
            db.execute("BEGIN IMMEDIATE")
            try:
                columns = {column["name"] for column in db.execute("PRAGMA table_info(sessions)")}
                for name, kind in (("stop_at", "REAL"), ("budget_stop_reason", "TEXT"),
                                   ("final_grace_started_at", "REAL")):
                    if name not in columns:
                        db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {kind}")
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def _connection(self):
        # Serialize short database transactions within a service process. On
        # the server's shared storage, simultaneous SQLite connections from
        # HTTP and the deadline poller otherwise stall in reader/writer locks.
        # SQLite BEGIN IMMEDIATE still arbitrates distinct admin processes.
        with self._connection_lock:
            db = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
            try:
                yield db
            finally:
                db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BudgetError:
                # Deadline/clock high-water updates survive expected denials.
                # All admission validations precede reservation mutations.
                db.commit()
                raise
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def _now(self) -> float:
        value = self.clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("server clock must return finite epoch seconds")
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError("server clock must return finite nonnegative epoch seconds")
        return value

    @staticmethod
    def _authenticate(db, session_id, token):
        row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if token is _ADMIN_FINALIZER and row is not None:
            return row
        # Always use the same public error for absent session, absent token, and
        # wrong token; no plaintext token is stored or returned after creation.
        supplied = hashlib.sha256(token.encode("utf8")).hexdigest() if isinstance(token, str) else ""
        expected = row["token_hash"] if row is not None else "0" * 64
        valid = secrets.compare_digest(supplied, expected)
        if row is None or not token or not valid:
            raise BudgetError("AUTH_FAILED", "Unknown session or invalid session token")
        return row

    def _refresh(self, db, row):
        now = max(self._now(), row["last_seen_at"])
        db.execute("UPDATE sessions SET last_seen_at=? WHERE session_id=?", (now, row["session_id"]))
        if row["state"] == "OPEN" and row["stop_at"] is None:
            stop_at, reason = None, None
            if now >= row["feedback_deadline"]:
                stop_at, reason = row["feedback_deadline"], "time_limit"
            if row["feedback_used"] >= row["max_feedback"]:
                latest = db.execute("""SELECT reserved_at FROM operations WHERE session_id=?
                    AND action IN ('check','bench') ORDER BY rowid DESC LIMIT 1""",
                                    (row["session_id"],)).fetchone()
                quota_at = latest["reserved_at"] if latest is not None else now
                if stop_at is None or quota_at <= stop_at:
                    stop_at, reason = quota_at, "feedback_limit"
            if stop_at is not None:
                grace_start = None if row["pending_operation_id"] else stop_at
                grace = json.loads(row["policy_json"])["final_grace_seconds"]
                deadline = None if grace_start is None else grace_start + grace
                db.execute("""UPDATE sessions SET stop_at=?,budget_stop_reason=?,
                    final_grace_started_at=?,final_deadline=? WHERE session_id=?""",
                           (stop_at, reason, grace_start, deadline, row["session_id"]))
                row = db.execute("SELECT * FROM sessions WHERE session_id=?", (row["session_id"],)).fetchone()
        if row["state"] == "OPEN" and row["final_deadline"] is not None and now >= row["final_deadline"]:
            db.execute("UPDATE sessions SET state='EXPIRED', closed_at=? WHERE session_id=?",
                       (row["final_deadline"], row["session_id"]))
        row = db.execute("SELECT * FROM sessions WHERE session_id=?", (row["session_id"],)).fetchone()
        return row, now

    @staticmethod
    def _session_view(row, now):
        is_open = row["state"] == "OPEN"
        before_deadline = row["feedback_deadline"] is not None and now < row["feedback_deadline"]
        idle = row["pending_operation_id"] is None
        policy = json.loads(row["policy_json"])
        remaining = (policy["active_seconds"] if row["started_at"] is None else
                     max(0, row["feedback_deadline"] - now))
        return {
            "session_id": row["session_id"],
            "problem_digest": row["problem_digest"],
            "track": row["track"],
            "claimed_agent_metadata": json.loads(row["claimed_agent_metadata_json"]),
            "agent_metadata_verified": False,
            "policy": policy,
            "clock_basis": "server_wall_seconds_since_resource_lease_no_internal_queue",
            "state": row["state"],
            "created_at": row["created_at"],
            "resource_id": row["resource_id"],
            "started_at": row["started_at"],
            "server_time": now,
            "feedback_deadline": row["feedback_deadline"],
            "final_deadline": row["final_deadline"],
            "stop_at": row["stop_at"],
            "budget_stop_reason": row["budget_stop_reason"],
            "final_grace_started_at": row["final_grace_started_at"],
            "feedback_used": row["feedback_used"],
            "feedback_remaining": max(0, row["max_feedback"] - row["feedback_used"]),
            "remaining_wall_seconds": remaining,
            "feedback_allowed": is_open and idle and row["stop_at"] is None and before_deadline and row["feedback_used"] < row["max_feedback"],
            "final_allowed": is_open and idle and row["final_deadline"] is not None and now < row["final_deadline"],
            "final_requires_submitted_source": row["stop_at"] is not None,
            "final_operation_id": row["final_operation_id"],
            "pending_operation_id": row["pending_operation_id"],
            "closed_at": row["closed_at"],
            "cancel_reason": row["cancel_reason"],
            "final_report": json.loads(row["final_report_json"]) if row["final_report_json"] else None,
        }

    @staticmethod
    def _operation_view(row):
        return {"operation_id": row["operation_id"], "session_id": row["session_id"],
                "request_id": row["request_id"], "action": row["action"],
                "source_sha256": row["source_sha256"], "state": row["state"],
                "reserved_at": row["reserved_at"], "completed_at": row["completed_at"],
                "report": json.loads(row["report_json"]) if row["report_json"] else None}

    def admin_create_session(self, *, problem_digest: str, track: str, owner_id: str,
                             resource_id: str = "default",
                             policy: Optional[Policy] = None,
                             claimed_agent_metadata: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Trusted admin only. Return the bearer token once; never log it."""
        _digest(problem_digest, "problem_digest")
        if track not in _TRACKS:
            raise ValueError("track must be from_scratch or optimize")
        if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 256:
            raise ValueError("owner_id must be a nonempty string of at most 256 characters")
        if not isinstance(resource_id, str) or not resource_id or len(resource_id) > 256:
            raise ValueError("resource_id must be a nonempty string of at most 256 characters")
        policy = policy if policy is not None else Policy()
        if not isinstance(policy, Policy):
            raise ValueError("policy must be a Policy instance")
        if claimed_agent_metadata is not None and not isinstance(claimed_agent_metadata, Mapping):
            raise ValueError("claimed_agent_metadata must be an object")
        metadata = _json(dict(claimed_agent_metadata or {}))
        if len(metadata.encode("utf8")) > 65536:
            raise ValueError("claimed_agent_metadata is too large")
        session_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        with self._transaction() as db:
            now = self._now()
            db.execute("""INSERT INTO sessions
                (session_id,token_hash,owner_id,problem_digest,track,claimed_agent_metadata_json,
                 policy_json,max_feedback,created_at,resource_id,last_seen_at,state)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'PENDING')""",
                       (session_id, hashlib.sha256(token.encode()).hexdigest(), owner_id,
                        problem_digest, track, metadata, _json(asdict(policy)), policy.max_feedback,
                        now, resource_id, now))
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return {"session_id": session_id, "token": token, "session": self._session_view(row, now)}

    def start_session(self, session_id: str, token: str) -> Dict[str, Any]:
        """Candidate-facing, idempotent start at slash-command entry.

        Resource contention raises RESOURCE_BUSY and leaves the session PENDING
        without consuming time. The trusted admin selects a real exclusive
        resource; candidates cannot change it. An expired session with pending
        work keeps its resource until the worker records completion. Admin
        cancellation must terminate/confirm termination of the worker first.
        """
        with self._transaction() as db:
            row, now = self._refresh(db, self._authenticate(db, session_id, token))
            if row["state"] in ("OPEN", "FINALIZING"):
                return self._session_view(row, now)
            if row["state"] != "PENDING":
                raise BudgetError("SESSION_CLOSED", "Session cannot be restarted: " + row["state"])
            occupants = db.execute("""SELECT * FROM sessions WHERE resource_id=?
                AND session_id!=? AND (state IN ('OPEN','FINALIZING') OR pending_operation_id IS NOT NULL)""",
                                   (row["resource_id"], session_id)).fetchall()
            for occupant in occupants:
                occupant, _ = self._refresh(db, occupant)
                if occupant["state"] in ("OPEN", "FINALIZING") or occupant["pending_operation_id"]:
                    raise BudgetError("RESOURCE_BUSY", "Resource is occupied; session remains pending and its clock has not started")
            policy = json.loads(row["policy_json"])
            normal = now + policy["active_seconds"]
            final = normal + policy["final_grace_seconds"]
            if not math.isfinite(final):
                raise ValueError("policy deadline overflows the server clock")
            db.execute("""UPDATE sessions SET state='OPEN',started_at=?,feedback_deadline=?,final_deadline=?
                WHERE session_id=?""", (now, normal, final, session_id))
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return self._session_view(row, now)

    def status(self, session_id: str, token: str) -> Dict[str, Any]:
        with self._transaction() as db:
            row, now = self._refresh(db, self._authenticate(db, session_id, token))
            return self._session_view(row, now)

    def reserve(self, session_id: str, token: str, *, request_id: str,
                action: str, source_sha256: str) -> Dict[str, Any]:
        """Atomically admit one immutable source/action; run only if created=True.

        There is deliberately no client policy, deadline, problem, track, counter
        or claimed score argument to override the frozen server-side session.
        """
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            raise ValueError("request_id must be 1-128 ASCII letters/digits/_.:-")
        if action not in _ACTIONS or token is _ADMIN_FINALIZER and action != "final":
            raise ValueError("action must be check, bench or final")
        _digest(source_sha256, "source_sha256")
        with self._transaction() as db:
            session, now = self._refresh(db, self._authenticate(db, session_id, token))
            previous = db.execute("SELECT * FROM operations WHERE session_id=? AND request_id=?",
                                  (session_id, request_id)).fetchone()
            if previous:
                if previous["action"] != action or previous["source_sha256"] != source_sha256:
                    raise BudgetError("REQUEST_CONFLICT", "Request ID was already bound to another action or source")
                return {**self._operation_view(previous), "created": False, "replayed": True,
                        "session": self._session_view(session, now)}
            if session["state"] == "PENDING":
                raise BudgetError("SESSION_NOT_STARTED", "Start the session before submitting work")
            if session["state"] != "OPEN":
                raise BudgetError("SESSION_CLOSED", "Session does not accept new reservations: " + session["state"])
            if action in ("check", "bench"):
                if now >= session["feedback_deadline"]:
                    raise BudgetError("DEADLINE_REACHED", "Feedback submission deadline has passed")
                if session["feedback_used"] >= session["max_feedback"]:
                    raise BudgetError("FEEDBACK_EXHAUSTED", "Feedback budget is exhausted; only final is available")
            elif session["stop_at"] is not None or token is _ADMIN_FINALIZER:
                known = db.execute("""SELECT 1 FROM operations WHERE session_id=?
                    AND source_sha256=? AND action IN ('check','bench') LIMIT 1""",
                                   (session_id, source_sha256)).fetchone()
                if not known:
                    raise BudgetError("UNSUBMITTED_FINAL_SOURCE", "Exhausted budget accepts only a previously submitted final source")
            if session["pending_operation_id"]:
                raise BudgetError("OPERATION_PENDING", "Complete the pending operation before submitting another")
            operation_id = uuid.uuid4().hex
            db.execute("""INSERT INTO operations
                (operation_id,session_id,request_id,action,source_sha256,state,reserved_at)
                VALUES (?,?,?,?,?,'RESERVED',?)""",
                       (operation_id, session_id, request_id, action, source_sha256, now))
            db.execute("UPDATE sessions SET pending_operation_id=? WHERE session_id=?", (operation_id, session_id))
            if action == "final":
                db.execute("UPDATE sessions SET state='FINALIZING', final_operation_id=? WHERE session_id=?",
                           (operation_id, session_id))
            else:
                db.execute("UPDATE sessions SET feedback_used=feedback_used+1 WHERE session_id=?", (session_id,))
            operation = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            session = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            session, now = self._refresh(db, session)
            return {**self._operation_view(operation), "created": True, "replayed": False,
                    "session": self._session_view(session, now)}

    def history(self, session_id: str, token: str) -> Dict[str, Any]:
        """Authenticated accepted-operation history, in reservation order.

        Reports are the same candidate-safe trusted results exposed by
        operation_status. This never includes token hashes, owner identities,
        source bytes, private judge files, or another session's operations.
        A reservation remains visible even if its worker crashed or it failed.
        """
        with self._transaction() as db:
            session, now = self._refresh(db, self._authenticate(db, session_id, token))
            operations = db.execute("""SELECT * FROM operations WHERE session_id=?
                ORDER BY reserved_at,rowid""", (session_id,)).fetchall()
            return {"session": self._session_view(session, now),
                    "operations": [self._operation_view(operation) for operation in operations]}

    def admin_open_sessions(self):
        """Trusted scheduler inventory; never a public API endpoint."""
        with self._connection() as db:
            return [row[0] for row in db.execute("SELECT session_id FROM sessions WHERE state='OPEN'")]

    def operation_status(self, session_id: str, token: str, operation_id: str) -> Dict[str, Any]:
        with self._transaction() as db:
            session, now = self._refresh(db, self._authenticate(db, session_id, token))
            operation = db.execute("SELECT * FROM operations WHERE operation_id=? AND session_id=?",
                                   (operation_id, session_id)).fetchone()
            if operation is None:
                raise BudgetError("OPERATION_NOT_FOUND", "Operation does not belong to this session")
            return {**self._operation_view(operation), "session": self._session_view(session, now)}

    def complete_operation(self, operation_id: str, report: Mapping[str, Any]) -> Dict[str, Any]:
        """Trusted worker only; persist its result and close a final atomically.

        A failure report is still a spent feedback/final attempt.  Repeating the
        same completion is harmless; changing an already recorded report is
        forbidden.  Cancellation cannot be undone by a late worker completion.
        """
        if not isinstance(report, Mapping):
            raise ValueError("report must be an object")
        encoded = _json(dict(report))
        with self._transaction() as db:
            operation = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if operation is None:
                raise BudgetError("OPERATION_NOT_FOUND", "Unknown operation")
            session = db.execute("SELECT * FROM sessions WHERE session_id=?", (operation["session_id"],)).fetchone()
            session, now = self._refresh(db, session)
            if operation["state"] == "FINISHED":
                if operation["report_json"] != encoded:
                    raise BudgetError("RESULT_CONFLICT", "Operation already has a different immutable result")
                return {**self._operation_view(operation), "session": self._session_view(session, now)}
            if operation["state"] != "RESERVED" or session["state"] == "CANCELLED":
                raise BudgetError("OPERATION_CANCELLED", "Cancelled operation cannot accept a late result")
            if operation["action"] == "final" and (
                    session["state"] != "FINALIZING" or session["final_operation_id"] != operation_id):
                raise BudgetError("SESSION_CLOSED", "This final is no longer the session's accepted final")
            db.execute("UPDATE operations SET state='FINISHED',completed_at=?,report_json=? WHERE operation_id=?",
                       (now, encoded, operation_id))
            db.execute("UPDATE sessions SET pending_operation_id=NULL WHERE session_id=? AND pending_operation_id=?",
                       (operation["session_id"], operation_id))
            if operation["action"] == "final":
                db.execute("UPDATE sessions SET state='FINISHED',closed_at=?,final_report_json=? WHERE session_id=?",
                           (now, encoded, operation["session_id"]))
            elif session["state"] == "OPEN" and session["stop_at"] is not None and session["final_deadline"] is None:
                # An accepted feedback consumed part/all of the provisional
                # grace. Start the full selection-only window after its result.
                grace = json.loads(session["policy_json"])["final_grace_seconds"]
                db.execute("""UPDATE sessions SET final_grace_started_at=?,final_deadline=?
                    WHERE session_id=?""", (now, now + grace, operation["session_id"]))
            operation = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            session = db.execute("SELECT * FROM sessions WHERE session_id=?", (operation["session_id"],)).fetchone()
            session, now = self._refresh(db, session)
            return {**self._operation_view(operation), "session": self._session_view(session, now)}

    def cancel_if_no_submission(self, session_id: str, token: str, *,
                                reason: str = "agent_exited_without_submission") -> bool:
        """Close an unused session without cancelling a concurrently admitted job.

        Return False if any source has been admitted or a worker is pending;
        the caller must then retrieve/wait for its normal final judgment.
        Authentication, admission inspection and closure share one transaction
        with reserve(). Unlike administrator cancellation, this never changes
        operation rows or clears a worker reservation.
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4096:
            raise ValueError("reason must be a nonempty string of at most 4096 characters")
        with self._transaction() as db:
            row, now = self._refresh(db, self._authenticate(db, session_id, token))
            if row["pending_operation_id"] or db.execute(
                    "SELECT 1 FROM operations WHERE session_id=? LIMIT 1", (session_id,)).fetchone():
                return False
            if row["state"] in ("FINISHED", "EXPIRED", "CANCELLED"):
                return True
            # Resource occupancy is derived from OPEN/FINALIZING state and the
            # pending-operation pointer. With no admitted work, this transition
            # releases it atomically and cannot strand a running worker.
            db.execute("UPDATE sessions SET state='CANCELLED',closed_at=?,cancel_reason=? WHERE session_id=?",
                       (now, reason, session_id))
            return True

    def admin_cancel_session(self, session_id: str, *, reason: str) -> Dict[str, Any]:
        """Trusted admin only, after terminating any worker. Irreversible.

        Pending reservations stay spent; the resource lease is released. An
        already EXPIRED session keeps that terminal state while orphaned work
        is cancelled. This state store does not kill a process/NPU task itself.
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4096:
            raise ValueError("reason must be a nonempty string of at most 4096 characters")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                raise BudgetError("SESSION_NOT_FOUND", "Unknown session")
            row, now = self._refresh(db, row)
            if row["state"] == "CANCELLED":
                return self._session_view(row, now)
            if row["state"] == "EXPIRED":
                db.execute("""UPDATE operations SET state='CANCELLED',completed_at=?
                    WHERE session_id=? AND state='RESERVED'""", (now, session_id))
                db.execute("UPDATE sessions SET pending_operation_id=NULL,cancel_reason=COALESCE(cancel_reason,?) WHERE session_id=?",
                           (reason, session_id))
                row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
                return self._session_view(row, now)
            if row["state"] == "FINISHED":
                raise BudgetError("SESSION_CLOSED", "Session is already terminal: " + row["state"])
            db.execute("UPDATE sessions SET state='CANCELLED',closed_at=?,cancel_reason=?,pending_operation_id=NULL WHERE session_id=?",
                       (now, reason, session_id))
            db.execute("UPDATE operations SET state='CANCELLED',completed_at=? WHERE session_id=? AND state='RESERVED'",
                       (now, session_id))
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return self._session_view(row, now)
