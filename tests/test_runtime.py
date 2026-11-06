import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from aegentra import (
    AdapterMismatch,
    BlockedByUnresolvedEffect,
    ConfirmedApplied,
    ConfirmedNotApplied,
    EffectScope,
    EvidenceMismatch,
    IntentConflict,
    Knowledge,
    Observation,
    Operation,
    OperationRecord,
    Phase,
    Runtime,
    RuntimeUsageError,
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


async def test_find_unresolved_recovers_the_original_key_after_restart(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    path = tmp_path / "agent.sqlite3"
    tool = RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT)
    with Runtime(path) as runtime:
        await runtime.perform(operation, tool=tool)
        original = runtime.inspect(operation.key)
        history = runtime.events(operation.key)
    with Runtime(path, require_existing=True) as restarted:
        found = restarted.find_unresolved(operation.scope)
        assert found == original
        assert restarted.events(operation.key) == history
        assert isinstance(await restarted.reconcile(found.key, tool=tool), ConfirmedApplied)
        assert restarted.find_unresolved(operation.scope) is None
        assert restarted.inspect(operation.key).knowledge is Knowledge.APPLIED
    assert len(ledger.refunds()) == 1


@pytest.mark.parametrize("different", ["tenant", "account", "resource", "effect"])
async def test_find_unresolved_matches_every_scope_dimension(
    tmp_path: Path, operation: Operation, different: str
) -> None:
    class UnknownTool:
        name = "declared-test-tool"
        contract_version = "1"

        def validate(self, candidate: Operation) -> None:
            pass

        async def execute(self, record: OperationRecord) -> Observation:
            return Observation.unknown(record, reason="No final evidence.")

        async def observe(self, record: OperationRecord) -> Observation:
            raise AssertionError("A scope lookup must not observe the provider.")

    other = replace(
        operation,
        operation_id="other",
        scope=replace(operation.scope, **{different: "another' OR 1=1 --"}),
    )
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=UnknownTool())
        assert runtime.find_unresolved(other.scope) is None
        await runtime.perform(other, tool=UnknownTool())
        before = runtime.events(operation.key), runtime.events(other.key)
        assert runtime.find_unresolved(operation.scope).key == operation.key
        assert runtime.find_unresolved(other.scope).key == other.key
        assert (runtime.events(operation.key), runtime.events(other.key)) == before


@pytest.mark.parametrize("scenario", [Scenario.ACKNOWLEDGED, Scenario.REJECTED])
async def test_find_unresolved_does_not_return_settled_history(
    tmp_path: Path, operation: Operation, ledger: RefundLedger, scenario: Scenario
) -> None:
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=RefundTool(ledger, scenario))
        assert runtime.find_unresolved(operation.scope) is None
        assert runtime.inspect(operation.key).phase is Phase.SETTLED


async def test_find_unresolved_includes_a_still_active_reservation(
    tmp_path: Path, operation: Operation
) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()

    class WaitingTool:
        name = "waiting"
        contract_version = "1"

        def validate(self, candidate: Operation) -> None:
            pass

        async def execute(self, record: OperationRecord) -> Observation:
            entered.set()
            await finish.wait()
            return Observation.unknown(record, reason="Still inconclusive.")

        async def observe(self, record: OperationRecord) -> Observation:
            raise AssertionError("A scope lookup must not observe.")

    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        task = asyncio.create_task(runtime.perform(operation, tool=WaitingTool()))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            found = runtime.find_unresolved(operation.scope)
            assert found.key == operation.key
            assert found.phase is Phase.RESERVED
        finally:
            finish.set()
            await asyncio.wait_for(task, timeout=5)
        assert runtime.find_unresolved(operation.scope).phase is Phase.UNRESOLVED


def test_find_unresolved_rejects_untyped_scopes_and_closed_runtime(tmp_path: Path) -> None:
    scope = EffectScope("tenant", "account", "resource", "effect")
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        assert runtime.find_unresolved(scope) is None
        with pytest.raises(TypeError, match="EffectScope"):
            runtime.find_unresolved(("tenant", "account", "resource", "effect"))
    with pytest.raises(RuntimeUsageError, match="closed"):
        runtime.find_unresolved(scope)


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


@pytest.mark.parametrize("field", ["arguments", "scope", "approval_ref"])
async def test_changed_intent_is_rejected_even_after_success(
    field: str, tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    changes = {
        "arguments": replace(operation, arguments={"amount_minor": 6000, "currency": "USD"}),
        "scope": replace(operation, scope=replace(operation.scope, resource="order-99")),
        "approval_ref": replace(operation, approval_ref="another-approval"),
    }
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=RefundTool(ledger))
        with pytest.raises(IntentConflict):
            await runtime.perform(changes[field], tool=RefundTool(ledger))
    assert len(ledger.refunds()) == 1


async def test_adapter_version_and_route_are_pinned(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT))
        for changed in (
            RefundTool(ledger, contract_version="sandbox-refund/2"),
            RefundTool(ledger, name="refund_order"),
        ):
            with pytest.raises(AdapterMismatch):
                await runtime.reconcile(operation.key, tool=changed)


@pytest.mark.parametrize("different", ["tenant", "account", "resource", "effect"])
async def test_only_matching_scope_is_blocked(
    different: str, tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    class DeclaredTool:
        name = "declared-test-tool"
        contract_version = "1"

        def validate(self, candidate: Operation) -> None:
            pass

        async def execute(self, record: OperationRecord) -> Observation:
            return Observation.unknown(record, reason="No outcome available.")

        async def observe(self, record: OperationRecord) -> Observation:
            return Observation.unknown(record, reason="No outcome available.")

    scope = replace(operation.scope, **{different: "different"})
    independent = replace(operation, operation_id="independent", scope=scope)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=RefundTool(ledger, Scenario.UNOBSERVABLE))
        assert isinstance(await runtime.perform(independent, tool=DeclaredTool()), Unresolved)


async def test_observation_from_another_operation_is_rejected(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    class WrongEvidence(RefundTool):
        async def execute(self, record: OperationRecord) -> Observation:
            return replace(
                await super().execute(record),
                key=replace(record.key, operation_id="different"),
            )

    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        with pytest.raises(EvidenceMismatch):
            await runtime.perform(operation, tool=WrongEvidence(ledger))
        assert runtime.inspect(operation.key).knowledge is Knowledge.UNKNOWN
    assert len(ledger.refunds()) == 1


async def test_wrong_payload_evidence_is_rejected(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    class WrongPayload(RefundTool):
        async def execute(self, record: OperationRecord) -> Observation:
            return replace(await super().execute(record), payload_digest="incorrect")

    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        with pytest.raises(EvidenceMismatch):
            await runtime.perform(operation, tool=WrongPayload(ledger))
        assert runtime.inspect(operation.key).phase is Phase.RESERVED


async def test_unexpected_exception_does_not_mean_no_effect(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    class BrokenResponse(RefundTool):
        async def execute(self, record: OperationRecord) -> Observation:
            await super().execute(record)
            raise OSError("Connection lost after provider commit")

    tool = BrokenResponse(ledger)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        with pytest.raises(OSError, match="after provider commit"):
            await runtime.perform(operation, tool=tool)
        assert runtime.inspect(operation.key).knowledge is Knowledge.UNKNOWN
        assert isinstance(await runtime.perform(operation, tool=tool), ConfirmedApplied)
    assert len(ledger.refunds()) == 1


async def test_observation_failure_preserves_scope_and_surfaces_the_error(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    class UnavailableLookup(RefundTool):
        async def observe(self, record: OperationRecord) -> Observation:
            raise ConnectionError("Provider status endpoint is unavailable")

    tool = UnavailableLookup(ledger, Scenario.LOST_ACKNOWLEDGMENT)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        assert isinstance(await runtime.perform(operation, tool=tool), Unresolved)
        with pytest.raises(ConnectionError, match="status endpoint"):
            await runtime.reconcile(operation.key, tool=tool)
        record = runtime.inspect(operation.key)
        assert record.knowledge is Knowledge.UNKNOWN
        assert record.phase is Phase.RECONCILING
        blocked = await runtime.perform(
            replace(operation, operation_id="another-attempt"), tool=RefundTool(ledger)
        )
        assert isinstance(blocked, BlockedByUnresolvedEffect)
    assert len(ledger.refunds()) == 1


async def test_cancellation_preserves_the_possibly_dispatched_operation(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    committed = asyncio.Event()
    never = asyncio.Event()

    class SlowResponse(RefundTool):
        async def execute(self, record: OperationRecord) -> Observation:
            observation = await super().execute(record)
            committed.set()
            await never.wait()
            return observation

    tool = SlowResponse(ledger)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        task = asyncio.create_task(runtime.perform(operation, tool=tool))
        await asyncio.wait_for(committed.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.inspect(operation.key).knowledge is Knowledge.UNKNOWN
        assert isinstance(await runtime.perform(operation, tool=tool), ConfirmedApplied)
    assert len(ledger.refunds()) == 1


async def test_live_concurrent_calls_do_not_dispatch_again(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    committed = asyncio.Event()
    release = asyncio.Event()

    class SlowResponse(RefundTool):
        async def execute(self, record: OperationRecord) -> Observation:
            observation = await super().execute(record)
            committed.set()
            await release.wait()
            return observation

    tool = SlowResponse(ledger)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        task = asyncio.create_task(runtime.perform(operation, tool=tool))
        try:
            await asyncio.wait_for(committed.wait(), timeout=5)
            assert isinstance(await runtime.perform(operation, tool=tool), Unresolved)
            blocked = await runtime.perform(
                replace(operation, operation_id="concurrent"), tool=tool
            )
            assert isinstance(blocked, BlockedByUnresolvedEffect)
            with pytest.raises(RuntimeUsageError, match="active operations"):
                runtime.close()
        finally:
            release.set()
            await task
    assert len(ledger.refunds()) == 1


async def test_arguments_are_snapshotted_before_execution(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    arguments = {"amount_minor": 5000, "currency": "USD"}
    mutable = replace(operation, arguments=arguments)

    class PausedTool(RefundTool):
        async def execute(self, record: OperationRecord) -> Observation:
            entered.set()
            await release.wait()
            return await super().execute(record)

    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        task = asyncio.create_task(runtime.perform(mutable, tool=PausedTool(ledger)))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            arguments["amount_minor"] = 9999
        finally:
            release.set()
        result = await task
        assert isinstance(result, ConfirmedApplied)
        assert result.receipt["amount_minor"] == 5000


async def test_invalid_adapter_arguments_do_not_reserve_scope(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        invalid = replace(operation, arguments={"amount_minor": True, "currency": "USD"})
        with pytest.raises(ValueError, match="amount_minor"):
            await runtime.perform(invalid, tool=RefundTool(ledger))
        assert isinstance(
            await runtime.perform(operation, tool=RefundTool(ledger)), ConfirmedApplied
        )


async def test_canonical_mapping_order_does_not_change_intent(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=RefundTool(ledger))
        reordered = replace(operation, arguments={"currency": "USD", "amount_minor": 5000})
        assert isinstance(
            await runtime.perform(reordered, tool=RefundTool(ledger)), ConfirmedApplied
        )
    assert len(ledger.refunds()) == 1


def test_scope_requires_explicit_nonempty_fields() -> None:
    with pytest.raises(ValueError, match="tenant"):
        EffectScope("", "payments", "order-42", "refund")
