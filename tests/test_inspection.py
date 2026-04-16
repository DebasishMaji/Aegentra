from pathlib import Path

from aegentra import (
    Inspection,
    Knowledge,
    Operation,
    Runtime,
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
