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
    RuntimeUsageError,
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


async def test_polling_wait_holds_the_active_boundary(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    entered = asyncio.Event()
    seen = []

    class CountedLookup(RefundTool):
        async def observe(self, record: OperationRecord) -> Observation:
            seen.append(record.key)
            entered.set()
            return await super().observe(record)

    tool = CountedLookup(ledger, Scenario.UNOBSERVABLE)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=tool)
        task = asyncio.create_task(
            runtime.recover(
                operation.key,
                tool=tool,
                budget=RecoveryBudget(interval_seconds=60),
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert isinstance(await runtime.reconcile(operation.key, tool=tool), Unresolved)
            assert isinstance(
                await runtime.recover(operation.key, tool=tool, budget=RecoveryBudget()),
                Unresolved,
            )
            with pytest.raises(RuntimeUsageError, match="active operations"):
                runtime.close()
            assert isinstance(
                await runtime.perform(
                    replace(operation, operation_id="fresh"), tool=RefundTool(ledger)
                ),
                BlockedByUnresolvedEffect,
            )
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(seen) == 1


async def test_deadline_includes_polling_waits(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    tool = RefundTool(ledger, Scenario.UNOBSERVABLE)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=tool)
        result = await asyncio.wait_for(
            runtime.recover(
                operation.key,
                tool=tool,
                budget=RecoveryBudget(timeout_seconds=0.02, interval_seconds=60),
            ),
            timeout=5,
        )
        assert isinstance(result, Unresolved)
        assert "deadline exhausted after 1 lookup(s)" in result.record.reason


@pytest.mark.parametrize("error_type", [TimeoutError, ConnectionError])
async def test_adapter_errors_are_not_budget_exhaustion(
    tmp_path: Path, operation: Operation, ledger: RefundLedger, error_type: type[Exception]
) -> None:
    class BrokenLookup(RefundTool):
        async def observe(self, record: OperationRecord) -> Observation:
            raise error_type("Provider lookup failed")

    tool = BrokenLookup(ledger, Scenario.LOST_ACKNOWLEDGMENT)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=tool)
        with pytest.raises(error_type, match="Provider lookup failed"):
            await runtime.recover(
                operation.key, tool=tool, budget=RecoveryBudget(timeout_seconds=5)
            )
        assert runtime.inspect(operation.key).phase is Phase.RECONCILING
        assert runtime.events(operation.key)[-1].kind == "reconciliation_started"
        assert isinstance(
            await runtime.recover(operation.key, tool=RefundTool(ledger), budget=RecoveryBudget()),
            ConfirmedApplied,
        )


async def test_external_cancellation_is_not_budget_exhaustion(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    entered = asyncio.Event()
    never = asyncio.Event()

    class WaitingLookup(RefundTool):
        async def observe(self, record: OperationRecord) -> Observation:
            entered.set()
            await never.wait()
            raise AssertionError("The caller should cancel this lookup.")

    tool = WaitingLookup(ledger, Scenario.LOST_ACKNOWLEDGMENT)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=tool)
        task = asyncio.create_task(
            runtime.recover(operation.key, tool=tool, budget=RecoveryBudget(timeout_seconds=60))
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.inspect(operation.key).knowledge is Knowledge.UNKNOWN
        assert runtime.events(operation.key)[-1].kind == "reconciliation_started"
        assert isinstance(
            await runtime.recover(operation.key, tool=RefundTool(ledger), budget=RecoveryBudget()),
            ConfirmedApplied,
        )


async def test_cancellation_suppressing_adapter_is_not_probed_again(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    seen = []

    class CancellationSuppressingLookup(RefundTool):
        async def observe(self, record: OperationRecord) -> Observation:
            seen.append(record.key)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return Observation.unknown(record, reason="No answer after interrupted lookup.")
            raise AssertionError("Expected a deadline.")

    tool = CancellationSuppressingLookup(ledger, Scenario.UNOBSERVABLE)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=tool)
        result = await asyncio.wait_for(
            runtime.recover(operation.key, tool=tool, budget=RecoveryBudget(timeout_seconds=0.02)),
            timeout=5,
        )
        assert isinstance(result, Unresolved)
        assert len(seen) == 1
        assert "deadline" in result.record.reason


async def test_definitive_evidence_returned_during_cancellation_is_retained(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    class LateAnswer(RefundTool):
        async def observe(self, record: OperationRecord) -> Observation:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return await super().observe(record)
            raise AssertionError("Expected the deadline to interrupt the wait.")

    tool = LateAnswer(ledger, Scenario.LOST_ACKNOWLEDGMENT)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=tool)
        result = await asyncio.wait_for(
            runtime.recover(operation.key, tool=tool, budget=RecoveryBudget(timeout_seconds=0.02)),
            timeout=5,
        )
        assert isinstance(result, ConfirmedApplied)
        assert runtime.events(operation.key)[-1].kind == "observed_applied"


async def test_invalid_budget_does_not_leave_an_active_operation(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    tool = RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=tool)
        with pytest.raises(TypeError, match="RecoveryBudget"):
            await runtime.recover(operation.key, tool=tool, budget=None)
        assert isinstance(
            await runtime.recover(operation.key, tool=tool, budget=RecoveryBudget()),
            ConfirmedApplied,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_observations": True},
        {"max_observations": 0},
        {"max_observations": -1},
        {"max_observations": 1.5},
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": True},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"interval_seconds": -1},
        {"interval_seconds": True},
        {"interval_seconds": float("nan")},
        {"interval_seconds": float("inf")},
        {"interval_seconds": "1"},
    ],
)
def test_invalid_budget_values_fail_before_use(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        RecoveryBudget(**kwargs)
