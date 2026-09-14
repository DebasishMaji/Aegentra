"""Read a stable journal snapshot without taking the runtime's ownership lock."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import ExitStack
from pathlib import Path
from types import TracebackType
from typing import Self

from ._read import decode_record, get_events, get_record, key_values
from ._serialization import event_dict, record_dict
from .errors import JournalVersionError, RuntimeUsageError
from .models import (
    EventPage,
    JSONValue,
    Knowledge,
    OperationKey,
    OperationPage,
    OperationRecord,
    _nonempty,
)


def _page_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000.")


class Inspection:
    """A short-lived, read-only snapshot. Reopen it to see later writer commits."""

    def __init__(self, journal_path: str | Path) -> None:
        path = Path(journal_path).resolve()
        self._owner = os.getpid(), threading.get_ident()
        self._closed = False
        with ExitStack() as resources:
            self._db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None)
            resources.callback(self._db.close)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA query_only = ON")
            self._db.execute("BEGIN")
            self.schema_version: int = self._db.execute("PRAGMA user_version").fetchone()[0]
            if self.schema_version not in (1, 2):
                raise JournalVersionError(
                    f"Cannot inspect journal version {self.schema_version}; "
                    "inspection never upgrades."
                )
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
            or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a nonnegative integer.")
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
        return {
            "format_version": 1,
            "source_schema_version": self.schema_version,
            "operation": record_dict(record, include_payload=include_payload),
            "events": events,
            "next_sequence": page.next_sequence,
        }
