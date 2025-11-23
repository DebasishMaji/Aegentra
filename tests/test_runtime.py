from pathlib import Path

from aegentra import (
    ConfirmedApplied,
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
