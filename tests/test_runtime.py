from dataclasses import replace
from pathlib import Path

from aegentra import (
    BlockedByUnresolvedEffect,
    ConfirmedApplied,
    ConfirmedNotApplied,
    Knowledge,
    Operation,
    Runtime,
    Unresolved,
)
from aegentra.sandbox import RefundLedger, RefundTool, Scenario


async def test_confirmation_is_replayed_without_another_write(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    tool = RefundTool(ledger)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        first = await runtime.perform(operation, tool=tool)
        second = await runtime.perform(operation, tool=tool)
        assert isinstance(first, ConfirmedApplied)
        assert second == first
        assert second.receipt["amount_minor"] == 5000
        assert len(ledger.refunds()) == 1
        assert [event.kind for event in runtime.events(operation.key)] == [
            "dispatch_reserved",
            "observed_applied",
        ]


async def test_restart_reconciles_lost_acknowledgment(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    path = tmp_path / "agent.sqlite3"
    tool = RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT)
    with Runtime(path) as runtime:
        assert isinstance(await runtime.perform(operation, tool=tool), Unresolved)
    with Runtime(path) as restarted:
        assert isinstance(await restarted.perform(operation, tool=tool), ConfirmedApplied)
    assert len(ledger.refunds()) == 1


async def test_new_operation_and_alternate_tool_cannot_bypass_unresolved_scope(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        first = await runtime.perform(
            operation, tool=RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT)
        )
        assert isinstance(first, Unresolved)
        alternate = replace(operation, operation_id="fresh-id", approval_ref="fresh-approval")
        result = await runtime.perform(alternate, tool=RefundTool(ledger, name="refund_order"))
        assert isinstance(result, BlockedByUnresolvedEffect)
        assert result.blocking_operation.key == operation.key
        assert len(ledger.refunds()) == 1


async def test_late_commit_remains_unknown_until_provider_confirms(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    tool = RefundTool(ledger, Scenario.DELAYED_COMMIT)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        assert isinstance(await runtime.perform(operation, tool=tool), Unresolved)
        assert isinstance(await runtime.reconcile(operation.key, tool=tool), Unresolved)
        assert ledger.refunds() == ()
        blocked = await runtime.perform(
            replace(operation, operation_id="retry"), tool=RefundTool(ledger)
        )
        assert isinstance(blocked, BlockedByUnresolvedEffect)
        ledger.commit_pending(operation.key)
        assert isinstance(await runtime.reconcile(operation.key, tool=tool), ConfirmedApplied)
        assert len(ledger.refunds()) == 1


async def test_unobservable_operation_stays_unresolved_without_reexecution(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    tool = RefundTool(ledger, Scenario.UNOBSERVABLE)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        for _ in range(3):
            assert isinstance(await runtime.perform(operation, tool=tool), Unresolved)
        assert runtime.inspect(operation.key).knowledge is Knowledge.UNKNOWN
    assert len(ledger.refunds()) == 1


async def test_final_rejection_releases_scope_but_does_not_retry_existing_operation(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        tool = RefundTool(ledger, Scenario.REJECTED)
        first = await runtime.perform(operation, tool=tool)
        assert isinstance(first, ConfirmedNotApplied)
        assert isinstance(await runtime.perform(operation, tool=tool), ConfirmedNotApplied)
        assert ledger.refunds() == ()
        replacement = replace(operation, operation_id="new-authorized-attempt", approval_ref="new")
        assert isinstance(
            await runtime.perform(replacement, tool=RefundTool(ledger)), ConfirmedApplied
        )
        assert len(ledger.refunds()) == 1
