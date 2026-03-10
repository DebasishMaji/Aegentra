import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from aegentra import (
    BlockedAction,
    BlockedByUnresolvedEffect,
    ConfirmedApplied,
    Knowledge,
    Operation,
    OperationNotFound,
    Runtime,
)
from aegentra.sandbox import RefundLedger, RefundTool, Scenario


async def test_blocked_proposal_is_durable_without_reserving_an_operation(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    path = tmp_path / "agent.sqlite3"
    proposal = replace(
        operation,
        operation_id="replanned-refund",
        approval_ref="replanned-approval",
        arguments={"amount_minor": 87654321, "currency": "EUR"},
    )
    with Runtime(path) as runtime:
        await runtime.perform(operation, tool=RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT))
        before = runtime.inspect(operation.key)
        result = await runtime.perform(proposal, tool=RefundTool(ledger, name="refund_order"))
        assert isinstance(result, BlockedByUnresolvedEffect)
        assert runtime.inspect(operation.key) == before
        with pytest.raises(OperationNotFound):
            runtime.inspect(proposal.key)
        events = runtime.events(operation.key)
        assert [event.kind for event in events] == [
            "dispatch_reserved",
            "observed_unknown",
            "effect_blocked",
        ]
        assert all(event.blocked_action is None for event in events[:-1])
        assert events[-1].blocked_action == BlockedAction(
            proposal.key, "refund_order", "sandbox-refund/1"
        )

    with Runtime(path) as restarted:
        assert restarted.events(operation.key) == events
        assert isinstance(
            await restarted.reconcile(operation.key, tool=RefundTool(ledger)), ConfirmedApplied
        )
        assert restarted.events(operation.key)[2] == events[-1]
        assert restarted.inspect(operation.key).knowledge is Knowledge.APPLIED

    with sqlite3.connect(path) as db:
        persisted_events = repr(db.execute("SELECT * FROM events").fetchall())
        assert "87654321" not in persisted_events
        assert "EUR" not in persisted_events
        assert "replanned-approval" not in persisted_events
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
    assert len(ledger.refunds()) == 1


async def test_each_blocked_proposal_gets_its_own_event(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        tool = RefundTool(ledger, Scenario.UNOBSERVABLE)
        await runtime.perform(operation, tool=tool)
        proposals = [
            replace(operation, operation_id="replan-1"),
            replace(operation, operation_id="replan-2"),
            replace(operation, operation_id="replan-1"),
        ]
        for proposal in proposals:
            result = await runtime.perform(proposal, tool=RefundTool(ledger, name="refund_order"))
            assert isinstance(result, BlockedByUnresolvedEffect)
        blocked = [
            event for event in runtime.events(operation.key) if event.blocked_action is not None
        ]
        assert [event.blocked_action.requested_key for event in blocked] == [
            proposal.key for proposal in proposals
        ]
        assert len({event.sequence for event in blocked}) == 3
    assert len(ledger.refunds()) == 1


async def test_another_tenant_does_not_create_a_blocked_event(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    other_tenant = replace(operation, scope=replace(operation.scope, tenant="tenant-b"))
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=RefundTool(ledger, Scenario.UNOBSERVABLE))
        result = await runtime.perform(other_tenant, tool=RefundTool(ledger))
        assert isinstance(result, ConfirmedApplied)
        assert all(event.blocked_action is None for event in runtime.events(operation.key))
        assert all(event.blocked_action is None for event in runtime.events(other_tenant.key))
    assert len(ledger.refunds()) == 2


async def test_failure_to_persist_a_block_is_not_reported_as_a_recorded_decision(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    path = tmp_path / "agent.sqlite3"
    with Runtime(path) as runtime:
        await runtime.perform(operation, tool=RefundTool(ledger, Scenario.UNOBSERVABLE))
        before = runtime.events(operation.key)
        with sqlite3.connect(path) as db:
            db.executescript(
                """CREATE TRIGGER reject_block_event BEFORE INSERT ON events
                   WHEN NEW.kind = 'effect_blocked'
                   BEGIN SELECT RAISE(ABORT, 'injected event write failure'); END;"""
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected event write failure"):
            await runtime.perform(
                replace(operation, operation_id="replan"),
                tool=RefundTool(ledger, name="refund_order"),
            )
        assert runtime.events(operation.key) == before
        assert runtime.inspect(operation.key).knowledge is Knowledge.UNKNOWN
    assert len(ledger.refunds()) == 1
