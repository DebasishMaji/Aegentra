from dataclasses import replace
from pathlib import Path

import pytest

from aegentra import (
    AdapterMismatch,
    BlockedByUnresolvedEffect,
    ConfirmedApplied,
    ConfirmedNotApplied,
    EvidenceMismatch,
    IntentConflict,
    Knowledge,
    Observation,
    Operation,
    OperationRecord,
    Phase,
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
