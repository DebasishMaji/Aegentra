import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from aegentra import (
    Inspection,
    JournalVersionError,
    Knowledge,
    Operation,
    OperationKey,
    OperationNotFound,
    Runtime,
    RuntimeUsageError,
)
from aegentra.sandbox import RefundLedger, RefundTool, Scenario


async def test_inspection_coexists_with_writer_and_keeps_a_stable_snapshot(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    path = tmp_path / "agent.sqlite3"
    tool = RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT)
    with Runtime(path) as runtime:
        await runtime.perform(operation, tool=tool)
        with Inspection(path) as reader:
            original = reader.inspect(operation.key)
            events = reader.events(operation.key)
            await runtime.reconcile(operation.key, tool=tool)
            assert reader.inspect(operation.key) == original
            assert reader.events(operation.key) == events
            assert original.knowledge is Knowledge.UNKNOWN
        with Inspection(path) as fresh:
            assert fresh.inspect(operation.key).knowledge is Knowledge.APPLIED


def test_missing_journal_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        Inspection(path)
    assert not path.exists()
    assert not Path(f"{path}.lock").exists()


async def test_schema_one_is_read_without_migration(
    tmp_path: Path, legacy_journal: Path, operation: Operation, ledger: RefundLedger
) -> None:
    current = tmp_path / "current.sqlite3"
    with Runtime(current) as runtime:
        await runtime.perform(operation, tool=RefundTool(ledger, Scenario.UNOBSERVABLE))
        original = runtime.inspect(operation.key)
        events = runtime.events(operation.key)
    with sqlite3.connect(legacy_journal) as db:
        db.execute("ATTACH DATABASE ? AS current", (str(current),))
        db.execute("INSERT INTO operations SELECT * FROM current.operations")
        db.execute(
            """INSERT INTO events
               SELECT sequence, tenant, account, operation_id, kind, reason, recorded_at
               FROM current.events"""
        )
    before = await asyncio.to_thread(legacy_journal.read_bytes)
    with Inspection(legacy_journal) as reader:
        assert reader.schema_version == 1
        assert reader.inspect(operation.key) == original
        assert reader.events(operation.key).events == events
    assert await asyncio.to_thread(legacy_journal.read_bytes) == before
    with sqlite3.connect(legacy_journal) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


async def test_keyset_pages_are_ordered_and_filters_are_parameterized(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    path = tmp_path / "agent.sqlite3"
    operations = [
        replace(
            operation,
            operation_id=identifier,
            scope=replace(operation.scope, tenant=tenant, resource=f"order-{tenant}-{identifier}"),
        )
        for tenant, identifier in [
            ("tenant-b", "op-a"),
            ("tenant-a", "op-c"),
            ("tenant-a", "op-a"),
            ("tenant-a", "op-b"),
        ]
    ]
    with Runtime(path) as runtime:
        for item in operations:
            await runtime.perform(item, tool=RefundTool(ledger, Scenario.UNOBSERVABLE))
        settled = replace(operation, operation_id="settled")
        await runtime.perform(settled, tool=RefundTool(ledger))

    with Inspection(path) as reader:
        first = reader.list_operations(limit=2)
        assert [item.key.operation_id for item in first.records] == ["op-a", "op-b"]
        assert first.next_cursor == first.records[-1].key
        second = reader.list_operations(after=first.next_cursor, limit=2)
        assert [(item.key.tenant, item.key.operation_id) for item in second.records] == [
            ("tenant-a", "op-c"),
            ("tenant-b", "op-a"),
        ]
        assert second.next_cursor is None
        assert len(reader.list_operations(knowledge=None).records) == 5
        assert len(reader.list_operations(tenant="tenant-b").records) == 1
        assert reader.list_operations(tenant="tenant-a' OR 1=1 --").records == ()
        assert reader.list_operations(account="another-account").records == ()


async def test_trace_is_paginated_and_payloads_require_opt_in(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    path = tmp_path / "agent.sqlite3"
    with Runtime(path) as runtime:
        await runtime.perform(operation, tool=RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT))
        await runtime.perform(replace(operation, operation_id="replanned"), tool=RefundTool(ledger))
    with Inspection(path) as reader:
        first = reader.events(operation.key, limit=1)
        assert first.next_sequence == first.events[0].sequence
        second = reader.events(operation.key, after_sequence=first.next_sequence, limit=2)
        assert [event.kind for event in second.events] == ["observed_unknown", "effect_blocked"]
        assert second.next_sequence is None
        assert second.events[-1].blocked_action.requested_key.operation_id == "replanned"
        trace = reader.export_trace(operation.key, limit=1)
        assert trace["format_version"] == 1
        assert "arguments" not in trace["operation"]
        assert "receipt" not in trace["operation"]
        full = reader.export_trace(operation.key, include_payload=True)
        assert full["operation"]["arguments"] == operation.arguments


async def test_applied_receipts_require_payload_opt_in(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    path = tmp_path / "journal ?# snapshot.sqlite3"
    with Runtime(path) as runtime:
        outcome = await runtime.perform(operation, tool=RefundTool(ledger))
    with Inspection(path) as reader:
        default = reader.export_trace(operation.key)
        assert "receipt" not in default["operation"]
        explicit = reader.export_trace(operation.key, include_payload=True)
        assert explicit["operation"]["receipt"] == outcome.receipt


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5])
def test_page_limits_are_bounded(tmp_path: Path, limit: int) -> None:
    path = tmp_path / "agent.sqlite3"
    with Runtime(path):
        pass
    with Inspection(path) as reader:
        with pytest.raises(ValueError, match="limit"):
            reader.list_operations(limit=limit)
        with pytest.raises(ValueError, match="limit"):
            reader.events(OperationKey("t", "a", "id"), limit=limit)


def test_inspection_connection_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite3"
    with Runtime(path):
        pass
    with Inspection(path) as reader:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader._db.execute("DELETE FROM operations")


def test_closed_inspection_and_unknown_versions_fail_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite3"
    with Runtime(path):
        pass
    reader = Inspection(path)
    reader.close()
    reader.close()
    with pytest.raises(RuntimeUsageError, match="closed"):
        reader.list_operations()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 999")
    with pytest.raises(JournalVersionError, match="999"):
        Inspection(path)


def test_missing_operation_does_not_return_an_empty_success(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite3"
    with Runtime(path):
        pass
    with Inspection(path) as reader:
        with pytest.raises(OperationNotFound):
            reader.export_trace(OperationKey("t", "a", "absent"))
