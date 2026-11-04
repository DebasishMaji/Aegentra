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
    Unresolved,
    decode_arguments,
    encode_arguments,
)
from aegentra.runtime import Runtime
from aegentra.tool import Tool


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
        result = {"operation_id": approved.operation_id}
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

    return StructuredTool.from_function(
        coroutine=perform,
        name=adapter.name,
        description=description,
        args_schema=_NoArguments.model_json_schema(),
    )
