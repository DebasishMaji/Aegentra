"""Run with: uv run --extra langgraph python examples/langgraph_refund.py"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from aegentra import EffectScope, Operation, Runtime
from aegentra.integrations.langgraph import operation_tool
from aegentra.sandbox import RefundLedger, RefundTool, Scenario


def primary_request(state: MessagesState) -> MessagesState:
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"id": "primary", "name": "refund_payment", "args": {}}],
            )
        ]
    }


def alternate_request(state: MessagesState) -> MessagesState:
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"id": "alternate", "name": "refund_order", "args": {}}],
            )
        ]
    }


async def demonstrate(directory: Path) -> None:
    ledger = RefundLedger(directory / "provider.sqlite3")
    operation = Operation(
        "approved-refund-42",
        EffectScope("shop", "payments", "order-42", "refund"),
        "approval-42",
        {"amount_minor": 5000, "currency": "USD"},
    )
    adapter = RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT)
    path = directory / "agent.sqlite3"
    with Runtime(path) as runtime:
        primary = operation_tool(
            runtime, operation, adapter=adapter, description="Refund the approved order."
        )
        alternate = operation_tool(
            runtime,
            replace(operation, operation_id="alternate-approved-proposal"),
            adapter=RefundTool(ledger, name="refund_order"),
            description="Propose the approved refund through the alternate route.",
        )
        tools = ToolNode([primary, alternate], handle_tool_errors=False)
        builder = StateGraph(MessagesState)
        builder.add_node("request_primary", primary_request)
        builder.add_node("primary_tools", tools)
        builder.add_node("request_alternate", alternate_request)
        builder.add_node("alternate_tools", tools)
        builder.add_edge(START, "request_primary")
        builder.add_edge("request_primary", "primary_tools")
        builder.add_edge("primary_tools", "request_alternate")
        builder.add_edge("request_alternate", "alternate_tools")
        builder.add_edge("alternate_tools", END)
        result = await builder.compile().ainvoke(
            {"messages": [HumanMessage(content="Process the approved refund.")]}
        )
        statuses: list[str] = []
        for message in result["messages"]:
            if isinstance(message, ToolMessage):
                if not isinstance(message.content, str):
                    raise TypeError("Expected a JSON tool response.")
                payload = json.loads(message.content)
                statuses.append(payload["status"])
                print(f"{message.name}: {message.content}")
        if statuses != ["unresolved", "blocked_by_unresolved_effect"]:
            raise RuntimeError("The LangGraph trace did not preserve the unresolved boundary.")

    with Runtime(path) as restarted:
        recover = operation_tool(
            restarted, operation, adapter=adapter, description="Reconcile the approved refund."
        )
        recovery = StateGraph(MessagesState)
        recovery.add_node("tools", ToolNode([recover], handle_tool_errors=False))
        recovery.add_edge(START, "tools")
        recovery.add_edge("tools", END)
        result = await recovery.compile().ainvoke(primary_request({"messages": []}))
        message = result["messages"][-1]
        if not isinstance(message.content, str):
            raise TypeError("Expected a JSON tool response.")
        if json.loads(message.content)["status"] != "confirmed_applied":
            raise RuntimeError("Restart did not reconcile the existing refund.")
        print(f"After restart: {message.content}")
    if len(ledger.refunds()) != 1:
        raise RuntimeError("The graph created an unexpected number of refunds.")
    print("Provider refunds: 1")
    print("Real LangGraph execution; scripted tool choices; no model or external provider.")


async def main() -> None:
    with TemporaryDirectory(prefix="aegentra-langgraph-") as directory:
        await demonstrate(Path(directory))


if __name__ == "__main__":
    asyncio.run(main())
