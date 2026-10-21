"""Request envelopes and evaluator-only ground truth shared by local experiments."""

from dataclasses import dataclass
from datetime import UTC, datetime

from .models import Knowledge, Operation, OperationRecord, Phase, encode_arguments
from .sandbox import RefundLedger
from .tool import Tool


def unmanaged_record(operation: Operation, tool: Tool) -> OperationRecord:
    tool.validate(operation)
    now = datetime.now(UTC).isoformat()
    return OperationRecord(
        key=operation.key,
        scope=operation.scope,
        approval_ref=operation.approval_ref,
        tool_name=tool.name,
        contract_version=tool.contract_version,
        payload_json=encode_arguments(operation.arguments),
        knowledge=Knowledge.UNKNOWN,
        phase=Phase.RESERVED,
        reason="Unpersisted baseline request envelope.",
        receipt_json=None,
        created_at=now,
        updated_at=now,
    )


@dataclass(frozen=True, slots=True)
class RefundEvaluation:
    actual_effects: int
    invalid_effects: int
    duplicate_effects: int
    pending_requests: int
    valid_completion: bool
    correct_resolution: bool
    false_terminal_claims: int


def evaluate_refund(
    ledger: RefundLedger, operation: Operation, knowledge: Knowledge
) -> RefundEvaluation:
    refunds = ledger.refunds()
    effects = len(refunds)
    matching = sum(
        refund.key.tenant == operation.scope.tenant
        and refund.key.account == operation.scope.account
        and refund.resource == operation.scope.resource
        and refund.amount_minor == operation.arguments["amount_minor"]
        and refund.currency == operation.arguments["currency"]
        for refund in refunds
    )
    pending = ledger.pending_count()
    completed = knowledge is Knowledge.APPLIED and effects == matching == 1 and pending == 0
    excluded = knowledge is Knowledge.NOT_APPLIED_FINAL and effects == 0 and pending == 0
    return RefundEvaluation(
        actual_effects=effects,
        invalid_effects=effects - matching,
        duplicate_effects=max(0, effects - 1),
        pending_requests=pending,
        valid_completion=completed,
        correct_resolution=completed or excluded,
        false_terminal_claims=int(
            knowledge is not Knowledge.UNKNOWN and not (completed or excluded)
        ),
    )
