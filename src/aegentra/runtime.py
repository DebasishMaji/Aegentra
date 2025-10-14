"""One possible dispatch per operation; subsequent calls reconcile, not re-execute."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from types import TracebackType
from typing import Self

from .errors import RuntimeUsageError
from .journal import Journal
from .models import (
    BlockedByUnresolvedEffect,
    ConfirmedApplied,
    ConfirmedNotApplied,
    JournalEvent,
    Knowledge,
    Operation,
    OperationKey,
    OperationRecord,
    Outcome,
    Unresolved,
    decode_arguments,
    encode_arguments,
)
from .tool import Tool


def _outcome(record: OperationRecord) -> ConfirmedApplied | ConfirmedNotApplied | Unresolved:
    match record.knowledge:
        case Knowledge.APPLIED:
            return ConfirmedApplied(record)
        case Knowledge.NOT_APPLIED_FINAL:
            return ConfirmedNotApplied(record)
        case Knowledge.UNKNOWN:
            return Unresolved(record)


class Runtime:
    def __init__(self, journal_path: str | Path) -> None:
        self._journal = Journal(Path(journal_path))
        self._owner = os.getpid(), threading.get_ident()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active: set[OperationKey] = set()
        self._closed = False

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
            raise RuntimeUsageError("The runtime is closed.")
        if self._owner != (os.getpid(), threading.get_ident()):
            raise RuntimeUsageError("Use the runtime only in its owning process and thread.")

    def _check_loop(self) -> None:
        self._check_owner()
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeUsageError("Use one event loop per runtime.")

    def close(self) -> None:
        if self._closed:
            return
        self._check_owner()
        if self._active:
            raise RuntimeUsageError("Await or cancel active operations before closing the runtime.")
        self._journal.close()
        self._closed = True

    async def perform(self, operation: Operation, *, tool: Tool) -> Outcome:
        self._check_loop()
        # Snapshot before any await; adapters never execute against the caller's mutable dictionary.
        payload = encode_arguments(operation.arguments)
        snapshot = Operation(
            operation.operation_id,
            operation.scope,
            operation.approval_ref,
            decode_arguments(payload),
        )
        tool.validate(snapshot)
        if not tool.name.strip() or not tool.contract_version.strip():
            raise ValueError("Tools must declare a name and contract version.")
        reservation = self._journal.reserve(snapshot, payload, tool.name, tool.contract_version)
        if isinstance(reservation, BlockedByUnresolvedEffect):
            return reservation
        record, created = reservation
        if not created:
            return await self.reconcile(record.key, tool=tool)

        self._active.add(record.key)
        try:
            observation = await tool.execute(record)
            return _outcome(self._journal.observe(record, observation))
        finally:
            # Exceptions and cancellation leave the durable reservation intact, never "not applied".
            self._active.remove(record.key)

    async def reconcile(
        self, key: OperationKey, *, tool: Tool
    ) -> ConfirmedApplied | ConfirmedNotApplied | Unresolved:
        self._check_loop()
        record = self._journal.get(key)
        self._journal.check_adapter(record, tool.name, tool.contract_version)
        if record.knowledge is not Knowledge.UNKNOWN or key in self._active:
            return _outcome(record)
        self._active.add(key)
        try:
            record = self._journal.begin_reconciliation(key)
            observation = await tool.observe(record)
            return _outcome(self._journal.observe(record, observation))
        finally:
            self._active.remove(key)

    def inspect(self, key: OperationKey) -> OperationRecord:
        self._check_owner()
        return self._journal.get(key)

    def events(self, key: OperationKey) -> tuple[JournalEvent, ...]:
        self._check_owner()
        return self._journal.events(key)
