import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from aegentra import Knowledge, Observation, Operation, OperationRecord, Runtime, RuntimeUsageError
from aegentra.sandbox import RefundLedger, RefundTool, Scenario

prebuilt = pytest.importorskip("langgraph.prebuilt")
tool_nodes = pytest.importorskip("langgraph.prebuilt.tool_node")
graphs = pytest.importorskip("langgraph.graph")
messages = pytest.importorskip("langchain_core.messages")
integration = pytest.importorskip("aegentra.integrations.langgraph")
pydantic = pytest.importorskip("pydantic")
function_calling = pytest.importorskip("langchain_core.utils.function_calling")
checkpoints = pytest.importorskip("langgraph.checkpoint.memory")


async def invoke(tool, arguments=None):
    builder = graphs.StateGraph(graphs.MessagesState)
    builder.add_node("tools", prebuilt.ToolNode([tool], handle_tool_errors=False))
    builder.add_edge(graphs.START, "tools")
    builder.add_edge("tools", graphs.END)
    result = await builder.compile().ainvoke(
        {
            "messages": [
                messages.AIMessage(
                    content="",
                    tool_calls=[{"id": "call", "name": tool.name, "args": arguments or {}}],
                )
            ]
        }
    )
    message = result["messages"][-1]
    assert isinstance(message, messages.ToolMessage)
    assert message.status == "success"
    return json.loads(message.content)


async def test_real_tool_node_blocks_alternate_intent_and_reconciles_after_restart(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
):
    path = tmp_path / "agent.sqlite3"
    adapter = RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT)
    with Runtime(path) as runtime:
        tool = integration.operation_tool(
            runtime, operation, adapter=adapter, description="Refund."
        )
        first = await invoke(tool)
        assert first == {"status": "unresolved", "operation_id": operation.operation_id}
        alternate = integration.operation_tool(
            runtime,
            replace(operation, operation_id="alternate"),
            adapter=RefundTool(ledger, name="refund_order"),
            description="Alternate route.",
        )
        assert await invoke(alternate) == {
            "status": "blocked_by_unresolved_effect",
            "operation_id": "alternate",
            "blocking_operation_id": operation.operation_id,
        }
    with Runtime(path) as restarted:
        tool = integration.operation_tool(
            restarted, operation, adapter=adapter, description="Check."
        )
        assert await invoke(tool) == {
            "status": "confirmed_applied",
            "operation_id": operation.operation_id,
        }
    assert len(ledger.refunds()) == 1


@pytest.mark.parametrize(
    ("scenario", "status", "refunds"),
    [
        (Scenario.ACKNOWLEDGED, "confirmed_applied", 1),
        (Scenario.REJECTED, "confirmed_not_applied", 0),
        (Scenario.UNOBSERVABLE, "unresolved", 1),
    ],
)
async def test_outcomes_are_explicit_and_never_redispatch(
    tmp_path: Path, operation: Operation, ledger: RefundLedger, scenario, status, refunds
):
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        tool = integration.operation_tool(
            runtime, operation, adapter=RefundTool(ledger, scenario), description="Refund."
        )
        for _ in range(2):
            result = await invoke(tool)
            assert result == {"status": status, "operation_id": operation.operation_id}
            assert not {"arguments", "receipt", "reason", "approval_ref"} & result.keys()
        assert len(ledger.refunds()) == refunds


@pytest.mark.parametrize(
    "arguments",
    [
        {"operation_id": "invented"},
        {"amount_minor": 1},
        {"approval_ref": "invented"},
        {"scope": {"resource": "other-order"}},
    ],
)
async def test_model_cannot_override_bound_intent(
    tmp_path: Path, operation: Operation, ledger: RefundLedger, arguments
):
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        tool = integration.operation_tool(
            runtime, operation, adapter=RefundTool(ledger), description="Refund."
        )
        assert tool.args_schema["additionalProperties"] is False
        with pytest.raises(tool_nodes.ToolInvocationError) as error:
            await invoke(tool, arguments)
        assert isinstance(error.value.__cause__, pydantic.ValidationError)
        assert ledger.refunds() == ()


async def test_model_facing_schema_exposes_no_intent_fields(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
):
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        tool = integration.operation_tool(
            runtime, operation, adapter=RefundTool(ledger), description="Refund."
        )
        schema = function_calling.convert_to_openai_tool(tool)["function"]["parameters"]
        assert schema["type"] == "object"
        assert schema["properties"] == {}
        assert schema["additionalProperties"] is False


async def test_parallel_graph_tool_calls_share_the_unresolved_boundary(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
):
    class YieldingResponse(RefundTool):
        async def execute(self, record: OperationRecord):
            self.ledger.submit(record, state="applied")
            await asyncio.sleep(0)
            return Observation.unknown(record, reason="Response lost.")

    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        tools = [
            integration.operation_tool(
                runtime,
                replace(operation, operation_id=f"proposal-{index}"),
                adapter=YieldingResponse(ledger, name=name),
                description="Refund.",
            )
            for index, name in enumerate(("refund_payment", "refund_order"))
        ]
        builder = graphs.StateGraph(graphs.MessagesState)
        builder.add_node("tools", prebuilt.ToolNode(tools, handle_tool_errors=False))
        builder.add_edge(graphs.START, "tools")
        builder.add_edge("tools", graphs.END)
        result = await builder.compile().ainvoke(
            {
                "messages": [
                    messages.AIMessage(
                        content="",
                        tool_calls=[
                            {"id": str(index), "name": tool.name, "args": {}}
                            for index, tool in enumerate(tools)
                        ],
                    )
                ]
            }
        )
        assert sorted(
            json.loads(message.content)["status"]
            for message in result["messages"]
            if isinstance(message, messages.ToolMessage)
        ) == ["blocked_by_unresolved_effect", "unresolved"]
    assert len(ledger.refunds()) == 1


async def test_binding_snapshots_the_authorized_payload(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
):
    arguments = dict(operation.arguments)
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        tool = integration.operation_tool(
            runtime,
            replace(operation, arguments=arguments),
            adapter=RefundTool(ledger),
            description="Refund.",
        )
        arguments["amount_minor"] = 1
        await invoke(tool)
        assert ledger.refunds()[0].amount_minor == 5000


async def test_provider_errors_propagate_without_losing_the_reservation(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
):
    class LostResponse(RefundTool):
        async def execute(self, record: OperationRecord):
            self.ledger.submit(record, state="applied")
            raise TimeoutError("Response lost.")

    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        tool = integration.operation_tool(
            runtime, operation, adapter=LostResponse(ledger), description="Refund."
        )
        with pytest.raises(TimeoutError, match="Response lost"):
            await invoke(tool)
        assert runtime.inspect(operation.key).knowledge is Knowledge.UNKNOWN
        assert (await invoke(tool))["status"] == "confirmed_applied"
    assert len(ledger.refunds()) == 1


async def test_checkpoint_replay_reconciles_after_runtime_reopens(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
):
    class LostResponse(RefundTool):
        async def execute(self, record: OperationRecord):
            self.ledger.submit(record, state="applied")
            raise TimeoutError("Response lost after the provider committed.")

    checkpointer = checkpoints.InMemorySaver()
    config = {"configurable": {"thread_id": "approved-refund"}}
    path = tmp_path / "agent.sqlite3"
    adapter = LostResponse(ledger)

    def graph(runtime):
        tool = integration.operation_tool(
            runtime, operation, adapter=adapter, description="Refund the approved order."
        )
        builder = graphs.StateGraph(graphs.MessagesState)
        builder.add_node("tools", prebuilt.ToolNode([tool], handle_tool_errors=False))
        builder.add_edge(graphs.START, "tools")
        builder.add_edge("tools", graphs.END)
        return builder.compile(checkpointer=checkpointer)

    with Runtime(path) as runtime:
        with pytest.raises(TimeoutError, match="provider committed"):
            await graph(runtime).ainvoke(
                {
                    "messages": [
                        messages.AIMessage(
                            content="",
                            tool_calls=[{"id": "original-call", "name": adapter.name, "args": {}}],
                        )
                    ]
                },
                config,
            )
        assert runtime.inspect(operation.key).knowledge is Knowledge.UNKNOWN

    with Runtime(path, require_existing=True) as restarted:
        result = await graph(restarted).ainvoke(None, config)
        response = result["messages"][-1]
        assert isinstance(response, messages.ToolMessage)
        assert response.tool_call_id == "original-call"
        assert json.loads(response.content) == {
            "status": "confirmed_applied",
            "operation_id": operation.operation_id,
        }
    assert len(ledger.refunds()) == 1


async def test_tool_cancellation_keeps_the_in_doubt_record(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
):
    submitted = asyncio.Event()

    class PendingResponse(RefundTool):
        async def execute(self, record: OperationRecord):
            self.ledger.submit(record, state="applied")
            submitted.set()
            await asyncio.Event().wait()
            raise AssertionError("Cancellation should prevent this line.")

    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        tool = integration.operation_tool(
            runtime, operation, adapter=PendingResponse(ledger), description="Refund."
        )
        pending = asyncio.create_task(invoke(tool))
        await asyncio.wait_for(submitted.wait(), timeout=5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert runtime.inspect(operation.key).knowledge is Knowledge.UNKNOWN
        assert (await invoke(tool))["status"] == "confirmed_applied"
    assert len(ledger.refunds()) == 1


async def test_closed_runtime_is_not_reported_as_a_successful_tool_call(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
):
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        tool = integration.operation_tool(
            runtime, operation, adapter=RefundTool(ledger), description="Refund."
        )
    with pytest.raises(RuntimeUsageError, match="closed"):
        await invoke(tool)
    assert ledger.refunds() == ()


def test_synchronous_invocation_cannot_move_runtime_work_to_a_worker(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
):
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        tool = integration.operation_tool(
            runtime, operation, adapter=RefundTool(ledger), description="Refund."
        )
        with pytest.raises(NotImplementedError):
            tool.invoke({})
    assert ledger.refunds() == ()
