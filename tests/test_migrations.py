import sqlite3
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
    Runtime,
)
from aegentra.models import encode_arguments
from aegentra.sandbox import RefundLedger, RefundTool


def _seed_operation(
    path: Path, operation: Operation, knowledge: Knowledge = Knowledge.UNKNOWN
) -> OperationRecord:
    created_at = "2026-10-23T07:00:00+00:00"
    payload = encode_arguments(operation.arguments)
    receipt = (
        encode_arguments({"provider_id": "legacy-refund"})
        if knowledge is Knowledge.APPLIED
        else None
    )
    phase = Phase.RESERVED if knowledge is Knowledge.UNKNOWN else Phase.SETTLED
    reason = "Outcome recorded by the first alpha."
    with sqlite3.connect(path) as db:
        db.execute(
            """INSERT INTO operations
               (tenant, account, operation_id, resource, effect, approval_ref,
                tool_name, contract_version, payload_json, knowledge, phase,
                reason, receipt_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                operation.key.tenant,
                operation.key.account,
                operation.operation_id,
                operation.scope.resource,
                operation.scope.effect,
                operation.approval_ref,
                "refund_payment",
                "sandbox-refund/1",
                payload,
                knowledge,
                phase,
                reason,
                receipt,
                created_at,
                created_at,
            ),
        )
        db.execute(
            """INSERT INTO events
               (sequence, tenant, account, operation_id, kind, reason, recorded_at)
               VALUES (41, ?, ?, ?, 'dispatch_reserved', ?, ?)""",
            (
                operation.key.tenant,
                operation.key.account,
                operation.operation_id,
                reason,
                created_at,
            ),
        )
    return OperationRecord(
        operation.key,
        operation.scope,
        operation.approval_ref,
        "refund_payment",
        "sandbox-refund/1",
        payload,
        knowledge,
        phase,
        reason,
        receipt,
        created_at,
        created_at,
    )


async def test_v1_upgrade_preserves_an_uncertain_effect_and_event_sequence(
    legacy_journal: Path, operation: Operation, ledger: RefundLedger
) -> None:
    original = _seed_operation(legacy_journal, operation)
    ledger.submit(original, state="applied")
    with Runtime(legacy_journal) as runtime:
        assert runtime.inspect(operation.key) == original
        old_event = runtime.events(operation.key)[0]
        assert old_event.sequence == 41
        assert old_event.blocked_action is None
        blocked = await runtime.perform(
            replace(operation, operation_id="new-id"),
            tool=RefundTool(ledger, name="refund_order"),
        )
        assert isinstance(blocked, BlockedByUnresolvedEffect)
        assert runtime.events(operation.key)[-1].sequence == 42
        assert isinstance(
            await runtime.perform(operation, tool=RefundTool(ledger)), ConfirmedApplied
        )
    with sqlite3.connect(legacy_journal) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert len(ledger.refunds()) == 1


@pytest.mark.parametrize("knowledge", [Knowledge.APPLIED, Knowledge.NOT_APPLIED_FINAL])
async def test_v1_settled_results_are_preserved_without_provider_calls(
    legacy_journal: Path, operation: Operation, ledger: RefundLedger, knowledge: Knowledge
) -> None:
    original = _seed_operation(legacy_journal, operation, knowledge)

    class MustNotCallProvider(RefundTool):
        async def execute(self, record: OperationRecord) -> Observation:
            raise AssertionError("A settled operation must not execute.")

        async def observe(self, record: OperationRecord) -> Observation:
            raise AssertionError("A settled operation must not observe.")

    with Runtime(legacy_journal) as runtime:
        assert runtime.inspect(operation.key) == original
        result = await runtime.perform(operation, tool=MustNotCallProvider(ledger))
        if knowledge is Knowledge.APPLIED:
            assert isinstance(result, ConfirmedApplied)
            assert result.receipt == {"provider_id": "legacy-refund"}
        else:
            assert isinstance(result, ConfirmedNotApplied)
        assert len(runtime.events(operation.key)) == 1
