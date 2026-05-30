import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from aegentra import (
    BlockedByUnresolvedEffect,
    ConfirmedApplied,
    ConfirmedNotApplied,
    Knowledge,
    Observation,
    Operation,
    OperationRecord,
    Phase,
    RecoveryBudget,
    Runtime,
    Unresolved,
)
from aegentra.sandbox import RefundLedger, RefundTool, Scenario


@pytest.mark.parametrize("allowance", [1, 3, 7])
async def test_exhaustion_obeys_exact_count_and_survives_restart(
    tmp_path: Path, operation: Operation, ledger: RefundLedger, allowance: int
) -> None:
    seen = []

    class CountingTool(RefundTool):
        async def observe(self, record: OperationRecord) -> Observation:
            seen.append(record.key)
            return await super().observe(record)

    path = tmp_path / "agent.sqlite3"
    tool = CountingTool(ledger, Scenario.UNOBSERVABLE)
    with Runtime(path) as runtime:
        await runtime.perform(operation, tool=tool)
        result = await runtime.recover(
            operation.key, tool=tool, budget=RecoveryBudget(max_observations=allowance)
        )
        assert isinstance(result, Unresolved)
        assert result.record.phase is Phase.UNRESOLVED
        assert len(seen) == allowance
        assert runtime.events(operation.key)[-1].kind == "recovery_exhausted"
        assert f"after {allowance} lookup(s)" in result.record.reason
    with Runtime(path) as restarted:
        result = await restarted.perform(
            replace(operation, operation_id="new"), tool=RefundTool(ledger)
        )
        assert isinstance(result, BlockedByUnresolvedEffect)
    assert len(ledger.refunds()) == 1


async def test_recovery_stops_on_evidence_and_replays_without_probes(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    seen = []

    class CountingTool(RefundTool):
        async def observe(self, record: OperationRecord) -> Observation:
            seen.append(record.key)
            return await super().observe(record)

    tool = CountingTool(ledger, Scenario.LOST_ACKNOWLEDGMENT)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=tool)
        result = await runtime.recover(operation.key, tool=tool, budget=RecoveryBudget())
        assert isinstance(result, ConfirmedApplied)
        assert len(seen) == 1
        assert await runtime.recover(operation.key, tool=tool, budget=RecoveryBudget()) == result
        assert len(seen) == 1
        assert "recovery_exhausted" not in [event.kind for event in runtime.events(operation.key)]
    assert len(ledger.refunds()) == 1


async def test_final_rejection_is_replayed_without_probes(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    class NoLookups(RefundTool):
        async def observe(self, record: OperationRecord) -> Observation:
            raise AssertionError("A settled rejection must not need another lookup.")

    tool = NoLookups(ledger, Scenario.REJECTED)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=tool)
        assert isinstance(
            await runtime.recover(operation.key, tool=tool, budget=RecoveryBudget()),
            ConfirmedNotApplied,
        )


async def test_deadline_counts_interrupted_lookup_but_does_not_assert_failure(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    seen = []
    never = asyncio.Event()

    class SlowLookup(RefundTool):
        async def observe(self, record: OperationRecord) -> Observation:
            seen.append(record.key)
            await never.wait()
            raise AssertionError("The deadline should interrupt this lookup.")

    tool = SlowLookup(ledger, Scenario.LOST_ACKNOWLEDGMENT)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=tool)
        result = await asyncio.wait_for(
            runtime.recover(operation.key, tool=tool, budget=RecoveryBudget(timeout_seconds=0.02)),
            timeout=5,
        )
        assert len(seen) == 1
        assert isinstance(result, Unresolved)
        assert result.record.knowledge is Knowledge.UNKNOWN
        assert result.record.phase is Phase.UNRESOLVED
        assert "deadline exhausted after 1 lookup(s)" in result.record.reason
    assert len(ledger.refunds()) == 1
