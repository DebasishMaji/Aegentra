"""Intent, observations, and outcomes are deliberately different types."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

JSONValue: TypeAlias = None | bool | int | str | list["JSONValue"] | dict[str, "JSONValue"]


def _json_value(value: object) -> JSONValue:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("Argument keys must be strings.")
            result[key] = _json_value(item)
        return result
    raise TypeError(f"Unsupported argument type: {type(value).__name__}. Use exact JSON values.")


def encode_arguments(arguments: Mapping[str, JSONValue]) -> str:
    """Snapshot JSON arguments without floating-point or stringification fallbacks."""
    return json.dumps(
        _json_value(arguments), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def decode_arguments(payload: str) -> dict[str, JSONValue]:
    value = _json_value(json.loads(payload))
    if not isinstance(value, dict):
        raise TypeError("An operation payload must be a JSON object.")
    return value


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")


@dataclass(frozen=True, slots=True)
class EffectScope:
    """A conflict boundary declared by the application, not inferred by the planner."""

    tenant: str
    account: str
    resource: str
    effect: str

    def __post_init__(self) -> None:
        for name in ("tenant", "account", "resource", "effect"):
            _nonempty(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class OperationKey:
    tenant: str
    account: str
    operation_id: str

    def __post_init__(self) -> None:
        for name in ("tenant", "account", "operation_id"):
            _nonempty(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class Operation:
    """An application-approved intent. Arguments are snapshotted before the first await."""

    operation_id: str
    scope: EffectScope
    approval_ref: str
    arguments: Mapping[str, JSONValue] = field(repr=False)

    def __post_init__(self) -> None:
        _nonempty(self.operation_id, "operation_id")
        _nonempty(self.approval_ref, "approval_ref")

    @property
    def key(self) -> OperationKey:
        return OperationKey(self.scope.tenant, self.scope.account, self.operation_id)


class Knowledge(StrEnum):
    UNKNOWN = "unknown"
    APPLIED = "applied"
    NOT_APPLIED_FINAL = "not_applied_final"


class Phase(StrEnum):
    RESERVED = "reserved"
    RECONCILING = "reconciling"
    UNRESOLVED = "unresolved"
    SETTLED = "settled"


@dataclass(frozen=True, slots=True)
class OperationRecord:
    key: OperationKey
    scope: EffectScope
    approval_ref: str
    tool_name: str
    contract_version: str
    payload_json: str = field(repr=False)
    knowledge: Knowledge
    phase: Phase
    reason: str
    receipt_json: str | None = field(repr=False)
    created_at: str
    updated_at: str

    @property
    def arguments(self) -> dict[str, JSONValue]:
        return decode_arguments(self.payload_json)

    @property
    def payload_digest(self) -> str:
        return hashlib.sha256(self.payload_json.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Observation:
    """A trusted adapter assertion, not a model-generated confidence score."""

    key: OperationKey
    payload_digest: str
    knowledge: Knowledge
    reason: str
    receipt_json: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.knowledge, Knowledge):
            raise TypeError("An observation must use an explicit Knowledge value.")
        _nonempty(self.reason, "reason")
        if self.knowledge is Knowledge.APPLIED:
            if self.receipt_json is None:
                raise ValueError("An applied observation requires a receipt.")
            decode_arguments(self.receipt_json)
        elif self.receipt_json is not None:
            raise ValueError("Only an applied observation may carry a receipt.")

    @classmethod
    def applied(
        cls,
        record: OperationRecord,
        *,
        receipt: Mapping[str, JSONValue],
        reason: str,
    ) -> Observation:
        return cls(
            record.key,
            record.payload_digest,
            Knowledge.APPLIED,
            reason,
            encode_arguments(receipt),
        )

    @classmethod
    def not_applied(cls, record: OperationRecord, *, reason: str) -> Observation:
        """Use only when this attempt cannot execute later."""
        return cls(record.key, record.payload_digest, Knowledge.NOT_APPLIED_FINAL, reason)

    @classmethod
    def unknown(cls, record: OperationRecord, *, reason: str) -> Observation:
        return cls(record.key, record.payload_digest, Knowledge.UNKNOWN, reason)


@dataclass(frozen=True, slots=True)
class ConfirmedApplied:
    record: OperationRecord

    @property
    def receipt(self) -> dict[str, JSONValue]:
        if self.record.receipt_json is None:
            raise ValueError("An applied journal record is missing its receipt.")
        return decode_arguments(self.record.receipt_json)


@dataclass(frozen=True, slots=True)
class ConfirmedNotApplied:
    record: OperationRecord


@dataclass(frozen=True, slots=True)
class Unresolved:
    record: OperationRecord


@dataclass(frozen=True, slots=True)
class BlockedByUnresolvedEffect:
    requested_key: OperationKey
    blocking_operation: OperationRecord


Outcome: TypeAlias = ConfirmedApplied | ConfirmedNotApplied | Unresolved | BlockedByUnresolvedEffect


@dataclass(frozen=True, slots=True)
class BlockedAction:
    """A rejected proposal, not a reserved dispatch or an operation record."""

    requested_key: OperationKey
    tool_name: str
    contract_version: str

    def __post_init__(self) -> None:
        _nonempty(self.tool_name, "tool_name")
        _nonempty(self.contract_version, "contract_version")


@dataclass(frozen=True, slots=True)
class JournalEvent:
    sequence: int
    kind: str
    reason: str
    recorded_at: str
    blocked_action: BlockedAction | None = None
