"""Explicit trace fields; arguments and receipts require opt-in."""

from .models import JournalEvent, JSONValue, OperationKey, OperationRecord, decode_arguments


def key_dict(key: OperationKey) -> dict[str, JSONValue]:
    return {"tenant": key.tenant, "account": key.account, "operation_id": key.operation_id}


def record_dict(record: OperationRecord, *, include_payload: bool = False) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {
        "key": key_dict(record.key),
        "scope": {
            "tenant": record.scope.tenant,
            "account": record.scope.account,
            "resource": record.scope.resource,
            "effect": record.scope.effect,
        },
        "approval_ref": record.approval_ref,
        "tool_name": record.tool_name,
        "contract_version": record.contract_version,
        "payload_digest": record.payload_digest,
        "knowledge": record.knowledge.value,
        "phase": record.phase.value,
        "reason": record.reason,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    if include_payload:
        result["arguments"] = record.arguments
        result["receipt"] = (
            decode_arguments(record.receipt_json) if record.receipt_json is not None else None
        )
    return result


def event_dict(event: JournalEvent) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {
        "sequence": event.sequence,
        "kind": event.kind,
        "reason": event.reason,
        "recorded_at": event.recorded_at,
        "blocked_action": None,
    }
    if event.blocked_action is not None:
        blocked = event.blocked_action
        result["blocked_action"] = {
            "requested_key": key_dict(blocked.requested_key),
            "tool_name": blocked.tool_name,
            "contract_version": blocked.contract_version,
        }
    return result
