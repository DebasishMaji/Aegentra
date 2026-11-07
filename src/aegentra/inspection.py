"""Read a stable journal snapshot without taking the runtime's ownership lock."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import ExitStack
from pathlib import Path
from types import TracebackType
from typing import Self

from ._format import check_database_path, read_format
from ._read import decode_record, find_unresolved_record, get_events, get_record, key_values
from ._serialization import event_dict, record_dict
from .errors import RuntimeUsageError, SnapshotUsageError
from .models import (
    EffectScope,
    EventPage,
    JSONValue,
    Knowledge,
    OperationKey,
    OperationPage,
    OperationRecord,
    _nonempty,
)
from .snapshot import (
    DatabaseSnapshot,
    database_issues,
    deadline_for,
    publish_snapshot,
    sqlite_deadline,
)


def _page_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000.")


class Inspection:
    """A short-lived, read-only snapshot. Reopen it to see later writer commits."""

    def __init__(self, journal_path: str | Path) -> None:
        path = Path(journal_path).resolve()
        self._path = path
        self._owner = os.getpid(), threading.get_ident()
        self._closed = False
        check_database_path(path)
        with ExitStack() as resources:
            self._db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None)
            resources.callback(self._db.close)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA query_only = ON")
            self._db.execute("BEGIN")
            self.schema_version, self.snapshot_info = read_format(self._db, allow_snapshot=True)
            self._resources = resources.pop_all()

    def __enter__(self) -> Self:
        self._check_owner()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _check_owner(self) -> None:
        if self._closed:
            raise RuntimeUsageError("The inspection is closed.")
        if self._owner != (os.getpid(), threading.get_ident()):
            raise RuntimeUsageError("Use an inspection only in its owning process and thread.")

    def close(self) -> None:
        if self._closed:
            return
        self._check_owner()
        self._resources.close()
        self._closed = True

    def inspect(self, key: OperationKey) -> OperationRecord:
        self._check_owner()
        return get_record(self._db, key)

    def find_unresolved(self, scope: EffectScope) -> OperationRecord | None:
        """Audit the operation holding this scope in the inspection's pinned view."""
        self._check_owner()
        return find_unresolved_record(self._db, scope)

    def snapshot(
        self, destination: str | Path, *, timeout_seconds: float = 30.0
    ) -> DatabaseSnapshot:
        self._check_owner()
        if self.snapshot_info is not None:
            raise SnapshotUsageError("Cannot relabel a historical snapshot as a new capture.")
        return publish_snapshot(
            self._db,
            self._path,
            destination,
            schema_version=self.schema_version,
            timeout_seconds=timeout_seconds,
        )

    def status(self, *, timeout_seconds: float = 5.0) -> dict[str, JSONValue]:
        self._check_owner()
        with sqlite_deadline(self._db, deadline_for(timeout_seconds)):
            issues = database_issues(self._db)
            counts: dict[str, JSONValue] = {
                row[0]: row[1]
                for row in self._db.execute(
                    "SELECT knowledge, COUNT(*) FROM operations GROUP BY knowledge"
                )
            }
            events = self._db.execute(
                "SELECT COUNT(*), COALESCE(MAX(sequence), 0) FROM events"
            ).fetchone()
            return {
                "format_version": 1,
                "source_schema_version": self.schema_version,
                "healthy": not issues,
                "issues": issues,
                "operations_by_knowledge": counts,
                "event_count": events[0],
                "last_event_sequence": events[1],
                "snapshot": self.snapshot_info.as_dict()
                if self.snapshot_info is not None
                else None,
            }

    def list_operations(
        self,
        *,
        knowledge: Knowledge | None = Knowledge.UNKNOWN,
        tenant: str | None = None,
        account: str | None = None,
        after: OperationKey | None = None,
        limit: int = 100,
    ) -> OperationPage:
        self._check_owner()
        _page_limit(limit)
        if knowledge is not None and not isinstance(knowledge, Knowledge):
            raise TypeError("knowledge must be a Knowledge value or None.")
        conditions: list[str] = []
        parameters: list[str | int] = []
        if knowledge is not None:
            conditions.append("knowledge = ?")
            parameters.append(knowledge.value)
        for column, value in (("tenant", tenant), ("account", account)):
            if value is not None:
                _nonempty(value, column)
                conditions.append(f"{column} = ?")
                parameters.append(value)
        if after is not None:
            conditions.append("(tenant, account, operation_id) > (?, ?, ?)")
            parameters.extend(key_values(after))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(limit + 1)
        rows = self._db.execute(
            f"SELECT * FROM operations{where} ORDER BY tenant, account, operation_id LIMIT ?",
            parameters,
        ).fetchall()
        records = tuple(decode_record(row) for row in rows[:limit])
        return OperationPage(records, records[-1].key if len(rows) > limit else None)

    def events(self, key: OperationKey, *, after_sequence: int = 0, limit: int = 100) -> EventPage:
        self._check_owner()
        _page_limit(limit)
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or not 0 <= after_sequence <= 2**63 - 1
        ):
            raise ValueError("after_sequence must be a nonnegative SQLite integer.")
        events = get_events(
            self._db,
            key,
            schema_version=self.schema_version,
            after_sequence=after_sequence,
            limit=limit + 1,
        )
        page = events[:limit]
        return EventPage(page, page[-1].sequence if len(events) > limit else None)

    def export_trace(
        self,
        key: OperationKey,
        *,
        include_payload: bool = False,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, JSONValue]:
        record = self.inspect(key)
        page = self.events(key, after_sequence=after_sequence, limit=limit)
        events: list[JSONValue] = [event_dict(event) for event in page.events]
        result: dict[str, JSONValue] = {
            "format_version": 1,
            "source_schema_version": self.schema_version,
            "operation": record_dict(record, include_payload=include_payload),
            "events": events,
            "next_sequence": page.next_sequence,
        }
        if self.snapshot_info is not None:
            result["snapshot"] = self.snapshot_info.as_dict()
        return result
