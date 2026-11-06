"""Local journal. Its transactions do not include the external provider."""

from __future__ import annotations

import errno
import fcntl
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ._format import read_format
from ._read import decode_record, find_unresolved_record, get_events, get_record, key_values
from .errors import (
    AdapterMismatch,
    EvidenceMismatch,
    IntentConflict,
    JournalInUse,
    JournalVersionError,
)
from .models import (
    BlockedAction,
    BlockedByUnresolvedEffect,
    EffectScope,
    JournalEvent,
    Knowledge,
    Observation,
    Operation,
    OperationKey,
    OperationRecord,
    Phase,
)

_SCHEMA_VERSION = 2
_SCHEMA_V1 = (
    """
    CREATE TABLE operations (
        tenant TEXT NOT NULL,
        account TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        resource TEXT NOT NULL,
        effect TEXT NOT NULL,
        approval_ref TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        contract_version TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        knowledge TEXT NOT NULL CHECK (knowledge IN ('unknown', 'applied', 'not_applied_final')),
        phase TEXT NOT NULL CHECK (phase IN ('reserved', 'reconciling', 'unresolved', 'settled')),
        reason TEXT NOT NULL,
        receipt_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (tenant, account, operation_id),
        CHECK ((knowledge = 'applied') = (receipt_json IS NOT NULL)),
        CHECK ((knowledge = 'unknown') = (phase != 'settled'))
    )
    """,
    """
    CREATE UNIQUE INDEX unresolved_scope
    ON operations (tenant, account, resource, effect) WHERE knowledge = 'unknown'
    """,
    """
    CREATE TABLE events (
        sequence INTEGER PRIMARY KEY,
        tenant TEXT NOT NULL,
        account TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        reason TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        FOREIGN KEY (tenant, account, operation_id)
            REFERENCES operations (tenant, account, operation_id)
    )
    """,
    "CREATE INDEX operation_events ON events (tenant, account, operation_id, sequence)",
)
_UPGRADE_V1_TO_V2 = (
    "ALTER TABLE events ADD COLUMN requested_operation_id TEXT",
    "ALTER TABLE events ADD COLUMN requested_tool_name TEXT",
    """
    ALTER TABLE events ADD COLUMN requested_contract_version TEXT CHECK (
        (kind = 'effect_blocked'
            AND requested_operation_id IS NOT NULL
            AND requested_tool_name IS NOT NULL
            AND requested_contract_version IS NOT NULL)
        OR
        (kind != 'effect_blocked'
            AND requested_operation_id IS NULL
            AND requested_tool_name IS NULL
            AND requested_contract_version IS NULL)
    )
    """,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _provision(path: Path, *, require_existing: bool) -> None:
    # Opening and closing an existing file outside SQLite can drop another connection's locks.
    try:
        path.stat()
    except FileNotFoundError:
        if require_existing:
            raise
        descriptor, name = tempfile.mkstemp(prefix=".aegentra-provision-", dir=path.parent)
        temporary = Path(name)
        try:
            os.close(descriptor)
            try:
                os.link(temporary, path)
            except FileExistsError:
                # A concurrent provisioner won; SQLite will validate its file without replacing it.
                pass
        finally:
            temporary.unlink()
    if not os.access(path, os.R_OK | os.W_OK, effective_ids=True):
        raise PermissionError(errno.EACCES, "Read/write database access is required.", path)


class Journal:
    def __init__(self, path: Path, *, require_existing: bool = False) -> None:
        if not isinstance(require_existing, bool):
            raise TypeError("require_existing must be a boolean.")
        self.path = path.resolve()
        self._require_existing = require_existing
        with ExitStack() as resources:
            lock_fd = os.open(f"{self.path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            resources.callback(os.close, lock_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise JournalInUse(f"Another runtime owns {self.path}.") from exc

            # Keep the lock inode: unlinking it lets a third process lock a different file.
            resources.callback(fcntl.flock, lock_fd, fcntl.LOCK_UN)
            _provision(self.path, require_existing=require_existing)
            self._db = sqlite3.connect(
                f"{self.path.as_uri()}?mode=rw", uri=True, isolation_level=None
            )
            resources.callback(self._db.close)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._db.execute("PRAGMA synchronous = FULL")
            self._initialize()
            mode = self._db.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode != "wal":
                raise JournalVersionError("Aegentra requires SQLite write-ahead logging.")
            self._resources = resources.pop_all()

    def close(self) -> None:
        self._resources.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        finally:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")

    def _initialize(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        application = self._db.execute("PRAGMA application_id").fetchone()[0]
        if version != 0 or application != 0:
            read_format(self._db)
        if version == _SCHEMA_VERSION:
            return
        if version not in (0, 1):
            raise JournalVersionError(f"Unsupported journal version {version}.")
        with self._transaction():
            if version == 0:
                if self._require_existing:
                    raise JournalVersionError("An initialized existing journal is required.")
                tables = self._db.execute(
                    """SELECT name FROM sqlite_master
                       WHERE name NOT LIKE 'sqlite_%'"""
                ).fetchall()
                if tables:
                    raise JournalVersionError("Refusing to initialize an unrecognized database.")
                for statement in _SCHEMA_V1:
                    self._db.execute(statement)
            for statement in _UPGRADE_V1_TO_V2:
                self._db.execute(statement)
            self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def get(self, key: OperationKey) -> OperationRecord:
        return get_record(self._db, key)

    def find_unresolved(self, scope: EffectScope) -> OperationRecord | None:
        return find_unresolved_record(self._db, scope)

    def reserve(
        self, operation: Operation, payload: str, tool_name: str, contract_version: str
    ) -> tuple[OperationRecord, bool] | BlockedByUnresolvedEffect:
        key = operation.key
        with self._transaction():
            existing = self._db.execute(
                "SELECT * FROM operations WHERE tenant = ? AND account = ? AND operation_id = ?",
                key_values(key),
            ).fetchone()
            if existing is not None:
                record = decode_record(existing)
                if (
                    record.scope != operation.scope
                    or record.approval_ref != operation.approval_ref
                    or record.payload_json != payload
                ):
                    raise IntentConflict(
                        f"Operation {key!r} is bound to a different approved intent."
                    )
                self.check_adapter(record, tool_name, contract_version)
                return record, False

            scope = operation.scope
            blocking_operation = self.find_unresolved(scope)
            if blocking_operation is not None:
                self._event(
                    blocking_operation.key,
                    "effect_blocked",
                    "A proposed write conflicts with this unresolved effect scope.",
                    _now(),
                    blocked_action=BlockedAction(key, tool_name, contract_version),
                )
                return BlockedByUnresolvedEffect(key, blocking_operation)

            now = _now()
            reason = "Dispatch reserved; no provider outcome has been established."
            self._db.execute(
                """INSERT INTO operations VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', 'reserved', ?, NULL, ?, ?
                )""",
                (
                    *key_values(key),
                    scope.resource,
                    scope.effect,
                    operation.approval_ref,
                    tool_name,
                    contract_version,
                    payload,
                    reason,
                    now,
                    now,
                ),
            )
            self._event(key, "dispatch_reserved", reason, now)
            return self.get(key), True

    @staticmethod
    def check_adapter(record: OperationRecord, tool_name: str, contract_version: str) -> None:
        if record.tool_name != tool_name or record.contract_version != contract_version:
            raise AdapterMismatch(
                f"Operation {record.key!r} requires "
                f"{record.tool_name!r} contract {record.contract_version!r}."
            )

    def begin_reconciliation(self, key: OperationKey) -> OperationRecord:
        with self._transaction():
            now = _now()
            self._db.execute(
                """UPDATE operations SET phase = 'reconciling', updated_at = ?
                   WHERE tenant = ? AND account = ? AND operation_id = ?
                     AND knowledge = 'unknown'""",
                (now, *key_values(key)),
            )
            self._event(key, "reconciliation_started", "Read-only provider lookup started.", now)
            return self.get(key)

    def observe(self, record: OperationRecord, observation: Observation) -> OperationRecord:
        if observation.key != record.key or observation.payload_digest != record.payload_digest:
            raise EvidenceMismatch(f"Observation does not match operation {record.key!r}.")
        with self._transaction():
            current = self.get(record.key)
            if current.knowledge is not Knowledge.UNKNOWN:
                raise EvidenceMismatch(
                    "A settled outcome cannot be replaced by another observation."
                )
            now = _now()
            phase = (
                Phase.UNRESOLVED if observation.knowledge is Knowledge.UNKNOWN else Phase.SETTLED
            )
            self._db.execute(
                """UPDATE operations
                   SET knowledge = ?, phase = ?, reason = ?, receipt_json = ?, updated_at = ?
                   WHERE tenant = ? AND account = ? AND operation_id = ?""",
                (
                    observation.knowledge,
                    phase,
                    observation.reason,
                    observation.receipt_json,
                    now,
                    *key_values(record.key),
                ),
            )
            self._event(record.key, f"observed_{observation.knowledge}", observation.reason, now)
            return self.get(record.key)

    def exhaust_recovery(self, key: OperationKey, reason: str) -> OperationRecord:
        with self._transaction():
            current = self.get(key)
            if current.knowledge is not Knowledge.UNKNOWN:
                raise EvidenceMismatch("Recovery exhaustion cannot replace a settled outcome.")
            now = _now()
            self._db.execute(
                """UPDATE operations SET phase = 'unresolved', reason = ?, updated_at = ?
                   WHERE tenant = ? AND account = ? AND operation_id = ?""",
                (reason, now, *key_values(key)),
            )
            self._event(key, "recovery_exhausted", reason, now)
            return self.get(key)

    def _event(
        self,
        key: OperationKey,
        kind: str,
        reason: str,
        now: str,
        *,
        blocked_action: BlockedAction | None = None,
    ) -> None:
        self._db.execute(
            """INSERT INTO events
               (tenant, account, operation_id, kind, reason, recorded_at,
                requested_operation_id, requested_tool_name, requested_contract_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                *key_values(key),
                kind,
                reason,
                now,
                blocked_action.requested_key.operation_id if blocked_action else None,
                blocked_action.tool_name if blocked_action else None,
                blocked_action.contract_version if blocked_action else None,
            ),
        )

    def events(self, key: OperationKey) -> tuple[JournalEvent, ...]:
        return get_events(self._db, key)
