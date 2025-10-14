"""Local journal. Its transactions do not include the external provider."""

from __future__ import annotations

import fcntl
import os
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .errors import (
    AdapterMismatch,
    EvidenceMismatch,
    IntentConflict,
    JournalInUse,
    JournalVersionError,
    OperationNotFound,
)
from .models import (
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

_SCHEMA_VERSION = 1
_SCHEMA = (
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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _key_values(key: OperationKey) -> tuple[str, str, str]:
    return key.tenant, key.account, key.operation_id


def _record(row: sqlite3.Row) -> OperationRecord:
    return OperationRecord(
        key=OperationKey(row["tenant"], row["account"], row["operation_id"]),
        scope=EffectScope(row["tenant"], row["account"], row["resource"], row["effect"]),
        approval_ref=row["approval_ref"],
        tool_name=row["tool_name"],
        contract_version=row["contract_version"],
        payload_json=row["payload_json"],
        knowledge=Knowledge(row["knowledge"]),
        phase=Phase(row["phase"]),
        reason=row["reason"],
        receipt_json=row["receipt_json"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        with ExitStack() as resources:
            lock_fd = os.open(f"{self.path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            resources.callback(os.close, lock_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise JournalInUse(f"Another runtime owns {self.path}.") from exc

            # Keep the lock inode: unlinking it lets a third process lock a different file.
            resources.callback(fcntl.flock, lock_fd, fcntl.LOCK_UN)
            db_fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(db_fd)
            self._db = sqlite3.connect(self.path, isolation_level=None)
            resources.callback(self._db.close)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._db.execute("PRAGMA synchronous = FULL")
            self._initialize()
            self._db.execute("PRAGMA journal_mode = WAL")
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
        if version == _SCHEMA_VERSION:
            return
        if version != 0:
            raise JournalVersionError(f"Unsupported journal version {version}.")
        tables = self._db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if tables:
            raise JournalVersionError("Refusing to initialize an unrecognized database.")
        with self._transaction():
            for statement in _SCHEMA:
                self._db.execute(statement)
            self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def get(self, key: OperationKey) -> OperationRecord:
        row = self._db.execute(
            "SELECT * FROM operations WHERE tenant = ? AND account = ? AND operation_id = ?",
            _key_values(key),
        ).fetchone()
        if row is None:
            raise OperationNotFound(f"No operation {key!r}.")
        return _record(row)

    def reserve(
        self, operation: Operation, payload: str, tool_name: str, contract_version: str
    ) -> tuple[OperationRecord, bool] | BlockedByUnresolvedEffect:
        key = operation.key
        with self._transaction():
            existing = self._db.execute(
                "SELECT * FROM operations WHERE tenant = ? AND account = ? AND operation_id = ?",
                _key_values(key),
            ).fetchone()
            if existing is not None:
                record = _record(existing)
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
            blocker = self._db.execute(
                """SELECT * FROM operations
                   WHERE tenant = ? AND account = ? AND resource = ? AND effect = ?
                     AND knowledge = 'unknown'""",
                (scope.tenant, scope.account, scope.resource, scope.effect),
            ).fetchone()
            if blocker is not None:
                return BlockedByUnresolvedEffect(key, _record(blocker))

            now = _now()
            reason = "Dispatch reserved; no provider outcome has been established."
            self._db.execute(
                """INSERT INTO operations VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', 'reserved', ?, NULL, ?, ?
                )""",
                (
                    *_key_values(key),
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
                (now, *_key_values(key)),
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
                    *_key_values(record.key),
                ),
            )
            self._event(record.key, f"observed_{observation.knowledge}", observation.reason, now)
            return self.get(record.key)

    def _event(self, key: OperationKey, kind: str, reason: str, now: str) -> None:
        self._db.execute(
            """INSERT INTO events
               (tenant, account, operation_id, kind, reason, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (*_key_values(key), kind, reason, now),
        )

    def events(self, key: OperationKey) -> tuple[JournalEvent, ...]:
        self.get(key)
        rows = self._db.execute(
            """SELECT sequence, kind, reason, recorded_at FROM events
               WHERE tenant = ? AND account = ? AND operation_id = ? ORDER BY sequence""",
            _key_values(key),
        ).fetchall()
        return tuple(JournalEvent(*row) for row in rows)
