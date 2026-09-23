"""Private execution accounting for the trusted controller, not an Agent API.

Transactions reserve attempts, concurrency and maximum cost before dispatch.
An ambiguous dispatch is never retried. This module does not execute commands.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid


class LedgerError(RuntimeError):
    pass


class LimitExceeded(LedgerError):
    pass


class Conflict(LedgerError):
    pass


ACTIVE = ("prepared", "dispatching", "unknown", "queued", "running", "cancelling", "reconcile_required")
TERMINAL = ("rejected", "cancelled_before_dispatch", "completed", "failed", "cancelled", "timeout")
JOB_TERMINAL = ("completed", "failed", "cancelled", "timeout")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", value):
        raise ValueError("Invalid identifier")
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Expected SHA-256 digest")
    return value


def _positive(value):
    if type(value) is not int or value <= 0:
        raise ValueError("Expected positive integer")
    return value


@dataclass(frozen=True)
class Resources:
    cores: int
    wall_seconds: int
    memory_bytes: int
    storage_bytes: int

    def __post_init__(self):
        for value in asdict(self).values():
            _positive(value)

    @property
    def core_seconds(self):
        return self.cores * self.wall_seconds


@dataclass(frozen=True)
class Policy:
    total_core_seconds: int
    max_cores: int
    max_wall_seconds: int
    max_memory_bytes: int
    total_storage_bytes: int
    concurrency: int
    approval_sha256: str

    def __post_init__(self):
        _digest(self.approval_sha256)
        for key, value in asdict(self).items():
            if key != "approval_sha256":
                _positive(value)


SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
 id TEXT PRIMARY KEY, policy TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
 id TEXT PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id),
 identity TEXT NOT NULL, max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 2),
 UNIQUE(campaign, identity)
);
CREATE TABLE IF NOT EXISTS requests (
 id TEXT PRIMARY KEY, evaluation TEXT NOT NULL REFERENCES evaluations(id),
 idempotency_key TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 resources TEXT NOT NULL, state TEXT NOT NULL, job_id TEXT,
 dispatch_claimed INTEGER NOT NULL DEFAULT 0 CHECK(dispatch_claimed IN (0,1)),
 charge_core_seconds INTEGER NOT NULL, charge_storage_bytes INTEGER NOT NULL,
 accounted INTEGER NOT NULL DEFAULT 0, actual_core_seconds INTEGER,
 UNIQUE(evaluation, idempotency_key), UNIQUE(job_id)
);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT REFERENCES requests(id),
 kind TEXT NOT NULL, at REAL NOT NULL, payload TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events
 BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_campaign_update BEFORE UPDATE ON campaigns
 BEGIN SELECT RAISE(ABORT, 'policy is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_evaluation_update BEFORE UPDATE ON evaluations
 BEGIN SELECT RAISE(ABORT, 'evaluation identity is immutable'); END;
"""


class Ledger:
    def __init__(self, path: Path):
        path = Path(path).expanduser()
        if path.is_symlink():
            raise ValueError("Ledger must not be a symlink")
        path = path.resolve()
        if any((p / ".git").exists() for p in [path.parent, *path.parent.parents]):
            raise ValueError("Ledger must be outside Git working trees")
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            fd = None
        if fd is not None:
            os.close(fd)
        if path.stat().st_mode & 0o077:
            raise ValueError("Ledger must be accessible only to its owner")
        self.path = path
        with self._transaction() as db:
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app_id not in {0, 0x414C4D50} or (tables and app_id == 0):
                raise LedgerError("Refusing to modify a database owned by another application")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise LedgerError("Unsupported ledger schema")
            statement = ""
            for line in SCHEMA.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            db.execute("PRAGMA application_id=1095519568")
            db.execute("PRAGMA user_version=1")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _transaction(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, request_id, kind, payload):
        db.execute("INSERT INTO events(request_id,kind,at,payload) VALUES (?,?,?,?)",
                   (request_id, kind, time.time(), _json(payload)))

    @staticmethod
    def _request(db, request_id):
        row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise LedgerError("Unknown request")
        return dict(row)

    def create_campaign(self, campaign: str, policy: Policy):
        _identifier(campaign)
        encoded = _json(asdict(policy))
        with self._transaction() as db:
            old = db.execute("SELECT policy FROM campaigns WHERE id=?", (campaign,)).fetchone()
            if old:
                if old["policy"] != encoded:
                    raise Conflict("Cannot replace an approved policy")
                return
            db.execute("INSERT INTO campaigns VALUES (?,?)", (campaign, encoded))
            self._event(db, None, "campaign_created", {"campaign": campaign, "policy": asdict(policy)})

    def register_evaluation(self, campaign: str, *, task_sha256: str, repetition: int,
                            role: str, system_sha256: str, max_attempts: int = 2):
        """Trusted registry fixes identity before generation; display names are absent."""
        _identifier(campaign)
        _digest(task_sha256)
        _digest(system_sha256)
        if type(repetition) is not int or repetition < 0:
            raise ValueError("Invalid repetition")
        if role not in {"agent", "reference", "development", "analysis"}:
            raise ValueError("Invalid accounting role")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 2:
            raise ValueError("At most two attempts per evaluation")
        identity = _json(dict(task=task_sha256, repetition=repetition, role=role, system=system_sha256))
        evaluation = hashlib.sha256((campaign + ":" + identity).encode()).hexdigest()
        with self._transaction() as db:
            if not db.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign,)).fetchone():
                raise LedgerError("Campaign has no approved policy")
            old = db.execute("SELECT max_attempts FROM evaluations WHERE id=?", (evaluation,)).fetchone()
            if old:
                if old["max_attempts"] != max_attempts:
                    raise Conflict("Evaluation attempt limit is frozen")
                return evaluation
            db.execute("INSERT INTO evaluations VALUES (?,?,?,?)", (evaluation, campaign, identity, max_attempts))
            self._event(db, None, "evaluation_registered", {"evaluation": evaluation, "identity": json.loads(identity)})
        return evaluation

    def reserve(self, evaluation: str, idempotency_key: str, manifest_sha256: str, resources: Resources):
        _identifier(idempotency_key)
        _digest(manifest_sha256)
        encoded = _json(asdict(resources))
        with self._transaction() as db:
            old = db.execute("SELECT * FROM requests WHERE evaluation=? AND idempotency_key=?",
                             (evaluation, idempotency_key)).fetchone()
            if old:
                if old["manifest_sha256"] != manifest_sha256 or old["resources"] != encoded:
                    raise Conflict("Idempotency key was already bound to different inputs")
                return dict(old)
            ev = db.execute("SELECT * FROM evaluations WHERE id=?", (evaluation,)).fetchone()
            if not ev:
                raise LedgerError("Unregistered evaluation")
            policy = json.loads(db.execute("SELECT policy FROM campaigns WHERE id=?", (ev["campaign"],)).fetchone()[0])
            if (resources.cores > policy["max_cores"] or resources.wall_seconds > policy["max_wall_seconds"]
                    or resources.memory_bytes > policy["max_memory_bytes"]):
                raise LimitExceeded("Per-job resources exceed approved limits")
            all_rows = db.execute("SELECT r.* FROM requests r JOIN evaluations e ON e.id=r.evaluation WHERE e.campaign=?",
                                  (ev["campaign"],)).fetchall()
            if any(r["state"] == "reconcile_required" for r in all_rows):
                raise Conflict("Campaign contains an unresolved scheduler conflict")
            own = [r for r in all_rows if r["evaluation"] == evaluation]
            if any(r["state"] in ACTIVE for r in own):
                raise Conflict("Existing evaluation request must finish or be reconciled first")
            if sum(r["dispatch_claimed"] or r["state"] == "prepared" for r in own) >= ev["max_attempts"]:
                raise LimitExceeded("Evaluation submission allowance exhausted")
            if sum(r["state"] in ACTIVE for r in all_rows) >= policy["concurrency"]:
                raise LimitExceeded("Campaign concurrency exhausted")
            if sum(r["charge_core_seconds"] for r in all_rows) + resources.core_seconds > policy["total_core_seconds"]:
                raise LimitExceeded("Campaign compute budget exhausted")
            if sum(r["charge_storage_bytes"] for r in all_rows) + resources.storage_bytes > policy["total_storage_bytes"]:
                raise LimitExceeded("Campaign storage budget exhausted")
            request_id = uuid.uuid4().hex
            db.execute("""INSERT INTO requests(id,evaluation,idempotency_key,manifest_sha256,resources,state,
                       charge_core_seconds,charge_storage_bytes) VALUES (?,?,?,?,?,'prepared',?,?)""",
                       (request_id, evaluation, idempotency_key, manifest_sha256, encoded,
                        resources.core_seconds, resources.storage_bytes))
            self._event(db, request_id, "reserved", {"manifest_sha256": manifest_sha256, "resources": asdict(resources)})
            return self._request(db, request_id)

    def begin_dispatch(self, request_id: str) -> bool:
        """Commit intent before I/O; at most one worker can claim a request."""
        with self._transaction() as db:
            row = self._request(db, request_id)
            if row["state"] != "prepared":
                return False
            # Accounting or an unexpected scheduler restart may have exhausted a
            # shared budget since reservation. Never dispatch a stale reservation.
            campaign = db.execute("SELECT campaign FROM evaluations WHERE id=?", (row["evaluation"],)).fetchone()[0]
            policy = json.loads(db.execute("SELECT policy FROM campaigns WHERE id=?", (campaign,)).fetchone()[0])
            rows = db.execute("SELECT r.* FROM requests r JOIN evaluations e ON r.evaluation=e.id WHERE e.campaign=?",
                              (campaign,)).fetchall()
            if any(r["state"] == "reconcile_required" for r in rows):
                raise Conflict("Campaign contains an unresolved scheduler conflict")
            if (sum(r["charge_core_seconds"] for r in rows) > policy["total_core_seconds"]
                    or sum(r["charge_storage_bytes"] for r in rows) > policy["total_storage_bytes"]
                    or sum(r["state"] in ACTIVE for r in rows) > policy["concurrency"]):
                raise LimitExceeded("Campaign limits changed through accounting; dispatch blocked")
            db.execute("UPDATE requests SET state='dispatching',dispatch_claimed=1 WHERE id=?", (request_id,))
            self._event(db, request_id, "dispatch_intent", {"note": "Request may reach scheduler; reconcile after any interruption"})
            return True

    def begin_staging(self, request_id: str) -> bool:
        """Claim one upload only after resources have been reserved."""
        with self._transaction() as db:
            row = self._request(db, request_id)
            if db.execute("SELECT 1 FROM events WHERE request_id=? AND kind='upload_intent'", (request_id,)).fetchone():
                return False
            if row['state'] != 'prepared':
                raise Conflict('Only a reserved, undispatched request may upload')
            self._event(db, request_id, 'upload_intent', {'manifest_sha256': row['manifest_sha256']})
            return True

    def staging_result(self, request_id: str, *, evidence_sha256: str | None = None, error_type: str | None = None):
        if (evidence_sha256 is None) == (error_type is None):
            raise ValueError('Record exactly one upload receipt or failure category')
        if evidence_sha256 is not None:
            _digest(evidence_sha256)
        elif not isinstance(error_type, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,99}', error_type):
            raise ValueError('Upload requires a receipt or error category')
        with self._transaction() as db:
            self._request(db, request_id)
            if not db.execute("SELECT 1 FROM events WHERE request_id=? AND kind='upload_intent'", (request_id,)).fetchone():
                raise Conflict('Missing upload intent')
            if db.execute("SELECT 1 FROM events WHERE request_id=? AND kind IN ('inputs_staged','upload_failed')", (request_id,)).fetchone():
                raise Conflict('Upload outcome is already recorded')
            self._event(db, request_id, 'inputs_staged' if evidence_sha256 else 'upload_failed',
                        {'evidence_sha256': evidence_sha256, 'error_type': error_type})

    def uncertain(self, request_id: str, evidence: dict):
        with self._transaction() as db:
            row = self._request(db, request_id)
            if row["state"] not in {"dispatching", "unknown"}:
                raise Conflict("Cannot mark this request uncertain")
            db.execute("UPDATE requests SET state='unknown' WHERE id=?", (request_id,))
            self._event(db, request_id, "dispatch_unknown", evidence)

    def accepted(self, request_id: str, job_id: str, evidence: dict):
        if not isinstance(job_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", job_id):
            raise ValueError("Only a single scheduler job is supported")
        with self._transaction() as db:
            row = self._request(db, request_id)
            if row["job_id"] == job_id:
                return
            if row["job_id"] or row["state"] not in {"dispatching", "unknown"}:
                raise Conflict("Cannot bind this request to a scheduler job")
            db.execute("UPDATE requests SET state='queued',job_id=? WHERE id=?", (job_id, request_id))
            self._event(db, request_id, "scheduler_accepted", {"job_id": job_id, "evidence": evidence})

    def rejected(self, request_id: str, evidence: dict):
        """Only positive evidence of scheduler rejection; absence from squeue is not proof."""
        with self._transaction() as db:
            row = self._request(db, request_id)
            if row["state"] not in {"dispatching", "unknown"} or row["job_id"]:
                raise Conflict("Cannot reject an accepted request")
            db.execute("UPDATE requests SET state='rejected',charge_core_seconds=0 WHERE id=?", (request_id,))
            # Staged files may remain, so rejection does not release storage.
            self._event(db, request_id, "scheduler_rejected", evidence)

    def cancel_intent(self, request_id: str):
        """Cancelling an accepted job retains reservations until scheduler confirmation."""
        with self._transaction() as db:
            row = self._request(db, request_id)
            if row["state"] in TERMINAL or row["state"] == "cancelling":
                return row
            if row["state"] == "prepared":
                db.execute("UPDATE requests SET state='cancelled_before_dispatch',charge_core_seconds=0 WHERE id=?", (request_id,))
                self._event(db, request_id, "cancelled_before_dispatch", {})
            elif row["job_id"]:
                # Requesting cancellation cannot clear a scheduler/accounting
                # conflict or reopen the campaign for further submissions.
                if row['state'] != 'reconcile_required':
                    db.execute("UPDATE requests SET state='cancelling' WHERE id=?", (request_id,))
                self._event(db, request_id, "cancel_intent", {'conflict_retained': row['state'] == 'reconcile_required'})
            else:
                raise Conflict("Reconcile unknown dispatch before cancellation")
            return self._request(db, request_id)

    def observe(self, request_id: str, job_id: str, state: str, evidence: dict):
        if state not in {"queued", "running", *JOB_TERMINAL}:
            raise ValueError("Unrecognized scheduler state")
        with self._transaction() as db:
            row = self._request(db, request_id)
            if row["job_id"] != job_id or not row["job_id"]:
                raise Conflict("Observation belongs to a different job")
            if row["state"] == "reconcile_required" or (row["state"] in JOB_TERMINAL and state != row["state"]):
                # Retain conflicting raw observations and block new submissions.
                cap = Resources(**json.loads(row["resources"])).core_seconds
                charge = row["charge_core_seconds"] + (cap if row["state"] != "reconcile_required" else 0)
                db.execute("UPDATE requests SET state='reconcile_required',charge_core_seconds=? WHERE id=?",
                           (charge, request_id))
                self._event(db, request_id, "scheduler_observation_conflict",
                            {"job_id": job_id, "state": state, "evidence": evidence})
                return
            # A cancel request is not confirmation, even if squeue still says running.
            effective = row["state"] if row["state"] == "cancelling" and state not in JOB_TERMINAL else state
            db.execute("UPDATE requests SET state=? WHERE id=?", (effective, request_id))
            self._event(db, request_id, "scheduler_observed", {"job_id": job_id, "state": state, "evidence": evidence})

    def account(self, request_id: str, core_seconds: int, evidence_sha256: str):
        """Trusted, final scheduler accounting only; retain worst-case charge until known.

        Record actual overruns rather than clamping them away. A resulting budget
        overrun blocks later reservations. Requeue/array workflows are not supported.
        """
        if type(core_seconds) is not int or core_seconds < 0:
            raise ValueError("Invalid accounting usage")
        _digest(evidence_sha256)
        with self._transaction() as db:
            row = self._request(db, request_id)
            if row["accounted"]:
                previous = db.execute("SELECT payload FROM events WHERE request_id=? AND kind='accounting_final' ORDER BY seq DESC LIMIT 1",
                                      (request_id,)).fetchone()
                if previous and json.loads(previous[0]) == {"core_seconds": core_seconds, "evidence_sha256": evidence_sha256}:
                    return
                raise Conflict("Cannot change final accounting")
            if row["state"] not in JOB_TERMINAL:
                raise Conflict("Final accounting requires a terminal, unaccounted job")
            db.execute("UPDATE requests SET charge_core_seconds=?,actual_core_seconds=?,accounted=1 WHERE id=?",
                       (core_seconds, core_seconds, request_id))
            self._event(db, request_id, "accounting_final", {"core_seconds": core_seconds, "evidence_sha256": evidence_sha256})

    def get(self, request_id: str):
        with self._transaction() as db:
            return self._request(db, request_id)

    def events(self, request_id: str):
        with self._transaction() as db:
            return [dict(r) for r in db.execute("SELECT * FROM events WHERE request_id=? ORDER BY seq", (request_id,))]

    def recoverable(self):
        with self._transaction() as db:
            placeholders = ",".join("?" for _ in ACTIVE)
            terminals = ",".join("?" for _ in JOB_TERMINAL)
            return [dict(r) for r in db.execute(
                f"SELECT * FROM requests WHERE state IN ({placeholders}) "
                f"OR (state IN ({terminals}) AND accounted=0)", (*ACTIVE, *JOB_TERMINAL))]

    def begin_reconciliation(self, request_id: str):
        """Persist a query ticket and derive identity/time from the original intent."""
        with self._transaction() as db:
            row = self._request(db, request_id)
            if not row['dispatch_claimed'] or row['state'] in {'rejected', 'cancelled_before_dispatch'}:
                raise Conflict('No dispatched job to reconcile')
            intent = db.execute("SELECT at FROM events WHERE request_id=? AND kind='dispatch_intent' ORDER BY seq LIMIT 1",
                                (request_id,)).fetchone()
            if intent is None:
                raise Conflict('Missing original dispatch intent')
            self._event(db, request_id, 'reconciliation_started', {'manifest_sha256': row['manifest_sha256']})
            ticket = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            return dict(ticket=ticket, request_id=request_id, manifest_sha256=row['manifest_sha256'],
                        dispatch_at=intent['at'])

    def apply_reconciliation(self, request_id: str, ticket: int, *, state: str, job_id: str | None,
                             core_seconds: int | None, reason: str, evidence_sha256: str):
        """Atomically bind identity, observe and account a trusted scheduler receipt.

        A query ticket is single-use. Later queries or state changes supersede it.
        Evidence is produced by the trusted reader, never supplied by the Agent.
        """
        if type(ticket) is not int or ticket <= 0:
            raise ValueError('Invalid query ticket')
        if state not in {'unknown', 'queued', 'running', *JOB_TERMINAL}:
            raise ValueError('Invalid scheduler observation')
        if job_id is not None and (not isinstance(job_id, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', job_id)):
            raise ValueError('Invalid scheduler job')
        if state != 'unknown' and job_id is None:
            raise ValueError('Verified observations require a job identity')
        if core_seconds is not None and (type(core_seconds) is not int or core_seconds < 0 or state not in JOB_TERMINAL):
            raise ValueError('Invalid final allocation usage')
        if not isinstance(reason, str) or len(reason) > 120:
            raise ValueError('Invalid observation reason')
        _digest(evidence_sha256)
        evidence = dict(ticket=ticket, state=state, job_id=job_id, core_seconds=core_seconds,
                        reason=reason, evidence_sha256=evidence_sha256)
        with self._transaction() as db:
            row = self._request(db, request_id)
            started = db.execute("SELECT * FROM events WHERE seq=? AND request_id=? AND kind='reconciliation_started'",
                                 (ticket, request_id)).fetchone()
            if started is None:
                raise Conflict('Query ticket belongs to another request or does not exist')
            later = db.execute('SELECT kind,payload FROM events WHERE request_id=? AND seq>? ORDER BY seq',
                               (request_id, ticket)).fetchall()
            for event in later:
                if event['kind'] == 'reconciliation_finished' and json.loads(event['payload'])['ticket'] == ticket:
                    if json.loads(event['payload'])['observation'] != evidence:
                        raise Conflict('Cannot replace a recorded query receipt')
                    return row

            def finish(outcome):
                self._event(db, request_id, 'reconciliation_finished',
                            {'ticket': ticket, 'outcome': outcome, 'observation': evidence})
                return self._request(db, request_id)

            # A newer query intent or any intervening state mutation makes this
            # response stale. Audit-only query outcomes do not mutate state.
            if any(event['kind'] != 'reconciliation_finished' for event in later):
                return finish('stale')

            def quarantine(detail):
                if row['state'] != 'reconcile_required':
                    cap = Resources(**json.loads(row['resources'])).core_seconds
                    db.execute("UPDATE requests SET state='reconcile_required',charge_core_seconds=charge_core_seconds+? WHERE id=?",
                               (cap, request_id))
                self._event(db, request_id, 'reconciliation_conflict', {'reason': detail, 'evidence': evidence})
                return finish('conflict')

            if row['state'] == 'reconcile_required':
                return finish('requires_review')
            if job_id is not None and row['job_id'] is not None and job_id != row['job_id']:
                return quarantine('different_job')
            if state == 'unknown':
                if reason in {'duplicate_allocations_or_restarts', 'restarted_allocation', 'queue_accounting_conflict'}:
                    return quarantine(reason)
                if row['state'] == 'dispatching':
                    db.execute("UPDATE requests SET state='unknown' WHERE id=?", (request_id,))
                return finish('unresolved')
            if row['job_id'] is None:
                if db.execute('SELECT 1 FROM requests WHERE job_id=? AND id<>?', (job_id, request_id)).fetchone():
                    return quarantine('job_already_bound')
                db.execute('UPDATE requests SET job_id=? WHERE id=?', (job_id, request_id))
                self._event(db, request_id, 'scheduler_accepted', {'job_id': job_id, 'evidence': evidence})
            if row['state'] in JOB_TERMINAL and state != row['state']:
                return quarantine('terminal_state_changed')
            if row['accounted'] and core_seconds is not None and row['actual_core_seconds'] != core_seconds:
                return quarantine('final_usage_changed')
            effective = row['state'] if row['state'] == 'cancelling' and state not in JOB_TERMINAL else state
            db.execute('UPDATE requests SET state=? WHERE id=?', (effective, request_id))
            self._event(db, request_id, 'scheduler_observed', {'job_id': job_id, 'state': state, 'evidence': evidence})
            if core_seconds is not None and not row['accounted']:
                db.execute('UPDATE requests SET charge_core_seconds=?,actual_core_seconds=?,accounted=1 WHERE id=?',
                           (core_seconds, core_seconds, request_id))
                self._event(db, request_id, 'accounting_final', {'core_seconds': core_seconds, 'evidence_sha256': evidence_sha256})
            return finish('applied')

    def summary(self, campaign: str):
        with self._transaction() as db:
            found = db.execute("SELECT policy FROM campaigns WHERE id=?", (campaign,)).fetchone()
            if not found:
                raise LedgerError("Unknown campaign")
            policy = json.loads(found[0])
            rows = db.execute("SELECT r.* FROM requests r JOIN evaluations e ON r.evaluation=e.id WHERE e.campaign=?",
                              (campaign,)).fetchall()
            charged = sum(r["charge_core_seconds"] for r in rows)
            return {"dispatch_intents": sum(r["dispatch_claimed"] for r in rows),
                    "accepted_jobs": sum(r["job_id"] is not None for r in rows),
                    "explicit_rejections": sum(r["state"] == "rejected" for r in rows),
                    "unresolved_dispatches": sum(r["state"] in {"dispatching", "unknown"} for r in rows),
                    "active_reservations": sum(r["state"] in ACTIVE for r in rows),
                    "accounted_core_seconds": sum(r["actual_core_seconds"] for r in rows if r["accounted"]),
                    "charged_or_reserved_core_seconds": charged,
                    "remaining_core_seconds": max(0, policy["total_core_seconds"] - charged),
                    "overrun_core_seconds": max(0, charged - policy["total_core_seconds"]),
                    "reserved_storage_bytes": sum(r["charge_storage_bytes"] for r in rows)}
