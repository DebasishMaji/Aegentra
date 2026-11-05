"""Async LangChain tools bound to application-approved Aegentra operations."""

from __future__ import annotations

import json
from typing import assert_never

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict

from aegentra.models import (
    BlockedByUnresolvedEffect,
    ConfirmedApplied,
    ConfirmedNotApplied,
    Operation,
    OperationKey,
    Outcome,
    Unresolved,
    _nonempty,
    decode_arguments,
    encode_arguments,
)
from aegentra.recovery import RecoveryBudget
from aegentra.runtime import Runtime
from aegentra.tool import Tool


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _outcome_json(operation_id: str, outcome: Outcome) -> str:
    result = {"operation_id": operation_id}
    match outcome:
        case ConfirmedApplied():
            result["status"] = "confirmed_applied"
        case ConfirmedNotApplied():
            result["status"] = "confirmed_not_applied"
        case Unresolved():
            result["status"] = "unresolved"
        case BlockedByUnresolvedEffect():
            result["status"] = "blocked_by_unresolved_effect"
            result["blocking_operation_id"] = outcome.blocking_operation.key.operation_id
        case unexpected:
            assert_never(unexpected)
    return json.dumps(result, sort_keys=True)


def operation_tool(
    runtime: Runtime,
    operation: Operation,
    *,
    adapter: Tool,
    description: str,
) -> StructuredTool:
    """Expose one snapshotted intent, without giving the model control of its identity."""
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description must be a non-empty string.")
    if not isinstance(adapter.name, str) or not adapter.name.strip():
        raise ValueError("The adapter must declare a non-empty name.")
    approved = Operation(
        operation.operation_id,
        operation.scope,
        operation.approval_ref,
        decode_arguments(encode_arguments(operation.arguments)),
    )

    async def perform(**arguments: object) -> str:
        # LangChain bypasses Pydantic validation for models with zero fields.
        _NoArguments.model_validate(arguments)
        outcome = await runtime.perform(approved, tool=adapter)
        return _outcome_json(approved.operation_id, outcome)

    return StructuredTool.from_function(
        coroutine=perform,
        name=adapter.name,
        description=description,
        args_schema=_NoArguments.model_json_schema(),
    )


def recovery_tool(
    runtime: Runtime,
    key: OperationKey,
    *,
    adapter: Tool,
    budget: RecoveryBudget,
    name: str,
    description: str,
) -> StructuredTool:
    """Expose bounded observation of an existing operation, never reservation or dispatch."""
    if not isinstance(key, OperationKey):
        raise TypeError("key must be an OperationKey.")
    if not isinstance(budget, RecoveryBudget):
        raise TypeError("budget must be a RecoveryBudget.")
    _nonempty(name, "name")
    _nonempty(description, "description")

    async def recover(**arguments: object) -> str:
        _NoArguments.model_validate(arguments)
        outcome = await runtime.recover(key, tool=adapter, budget=budget)
        return _outcome_json(key.operation_id, outcome)

    return StructuredTool.from_function(
        coroutine=recover,
        name=name,
        description=description,
        args_schema=_NoArguments.model_json_schema(),
    )
