# Approved operations in LangGraph

`aegentra.integrations.langgraph.operation_tool` exposes one application-approved
operation as an async LangChain `StructuredTool`. It fits an existing LangGraph
`ToolNode`; it does not replace the graph, model, provider adapter, or authorization
layer.

## Install and run

From the checkout:

```sh
uv sync --locked --extra langgraph
uv run --extra langgraph python examples/langgraph_refund.py
```

For an existing local Python environment, `python -m pip install '.[langgraph]'`
installs the checkout with the extra. No public package release is implied.
The core package has no third-party runtime dependencies; importing `aegentra`
does not import LangGraph, LangChain, or Pydantic.

The example runs a compiled graph with scripted tool choices and a sandbox
provider, not an LLM or external payment API. The provider creates one refund
but loses the acknowledgment. An alternate route is blocked. After the runtime
reopens, a recovery-only tool checks the original operation without another write.

## Bind an operation, not a model-controlled authorization request

Create `operation` and `adapter` in trusted application code as described in the
[README](../README.md#put-a-boundary-around-a-write). Inside the runtime's owning
async session:

```python
from aegentra.integrations.langgraph import operation_tool
from langgraph.prebuilt import ToolNode

tool = operation_tool(
    runtime,
    operation,
    adapter=adapter,
    description="Refund the application-approved order.",
)
tools = ToolNode([tool], handle_tool_errors=False)
```

Add `tools` as a node in your compiled graph and call the graph with `ainvoke`.
See the [complete graph example](../examples/langgraph_refund.py) for executable
wiring. A bare `ToolNode.ainvoke` can lack the runtime context supplied by a
compiled graph.

The exposed tool name is `adapter.name`. Its JSON input is `{}`. Unknown fields
are rejected before `Runtime.perform` is called; they are not silently ignored.
The operation's JSON payload is snapshotted at binding time, not deferred until
the graph runs. Later mutation of the caller's payload does not change it.

The model cannot supply an operation ID, scope, approval reference, or amount.
To authorize a different operation, application code must construct another
binding. This does not verify that the application actually performed authorization.
Business scope and provider finality remain the adapter's responsibilities.

Prefer one bound operation ID per approved intent. The example's alternate ID
is a stress case for the unresolved-scope guard, not an authorization pattern
to copy for automatic fallbacks. If application code exposes multiple approved
IDs for the same intent, the scope guard does not permanently deduplicate them
after settlement.

## Read the business outcome

The tool returns a JSON string containing `operation_id` and one of:

| `status` | Meaning |
|---|---|
| `confirmed_applied` | Matching provider evidence confirms the effect. |
| `confirmed_not_applied` | The adapter excludes later execution of this attempt. |
| `unresolved` | Neither conclusion is established. |
| `blocked_by_unresolved_effect` | The scope is held by another unresolved operation; includes `blocking_operation_id`. |

Arguments, receipts, approval references, and adapter reasons are not copied into
the model-facing response. Application-owned operation IDs remain visible.
Inspect receipts and event history through the runtime or operator APIs.
LangChain/LangGraph tracing and callbacks are application-configured and may
record tool names, IDs, and responses. These bindings do not manage or redact
external tracing exports.

A LangChain `ToolMessage` can have framework status `success` while its JSON
business status is `unresolved` or `blocked_by_unresolved_effect`. Successful tool
execution is not a completed refund. Gate application behavior on the JSON
outcome, not the framework status or the model's completion text.

Repeated invocation of the same bound operation follows normal Aegentra rules:
read-only reconciliation or a settled result, never another dispatch. A settled
scope is not a permanent ban on independently authorized future operations.

## Expose recovery without dispatch authority

When a graph should only inspect a previously reserved operation, bind
`recovery_tool` instead of `operation_tool`:

```python
from aegentra import RecoveryBudget
from aegentra.integrations.langgraph import recovery_tool

check = recovery_tool(
    runtime,
    operation.key,
    adapter=adapter,
    budget=RecoveryBudget(max_observations=3, timeout_seconds=5.0, interval_seconds=0.25),
    name="check_refund_status",
    description="Check the original refund without submitting another request.",
)
tools = ToolNode([check], handle_tool_errors=False)
```

Its strict input is `{}`. The application binds the full operation key, adapter,
and recovery budget; the model cannot override them. Invocation calls
`Runtime.recover`, never reservation, dispatch validation, or `adapter.execute`.
The original adapter name and contract version must still match. As always,
`adapter.observe` must itself be read-only.

An unrecorded key raises `OperationNotFound`; it is not implicitly authorized or
created. Settled operations return their recorded result without another lookup.
Unresolved operations use at most the configured observations per invocation.
Budget exhaustion returns `unresolved`, records `recovery_exhausted`, and leaves
the business scope blocked. Deadlines are cooperative, not protection against
blocking adapters. Provider errors propagate.

The JSON response uses the same operation ID, status, and redaction rules above.
Only `confirmed_applied`, `confirmed_not_applied`, and `unresolved` can result
from recovery. This is a per-call budget, not a graph-wide rate or cost limit:
applications must separately bound how often the model can request recovery.

## Ownership and errors

Keep the runtime open until all graph work is awaited or cancelled. Use one
owning process, thread, and event loop. The tool is async-only; synchronous
`invoke` is deliberately unsupported rather than moving SQLite work to a worker.
The async tool does not offload a blocking provider adapter; adapters must still
cooperate with the event loop.

`handle_tool_errors=False` keeps unexpected provider and usage errors visible
to the application. LangGraph may wrap argument-validation errors in
`ToolInvocationError`, preserving the validation error as its cause. Do not turn
these errors into presumed business failures or authorize a fresh write.
Cancellation and exceptions leave an existing reservation in doubt; a later
call can reconcile it.

These are bindings for trusted adapters, not a generic fault injector or a
production connector. They do not infer equivalent business intents, prevent
out-of-band writes, or constrain arbitrary model text.

## Checkpoint replay is not a new business operation

LangGraph can replay an unfinished tool node after a provider committed but its
response was lost. Rebind the **same approved operation** against the original
live journal before resuming the checkpoint. Aegentra then reconciles the
existing reservation rather than dispatching another write. Do not generate a
fresh operation ID from a new graph attempt or tool-call ID.

The integration regression recreates the graph after closing and reopening the
runtime, resumes its retained checkpoint, and checks that only one sandbox refund
exists. Its in-memory checkpointer is a test fixture, not durable process-crash
storage. Applications must configure and preserve their own durable LangGraph
checkpointer as well as the live Aegentra journal. These stores and the provider
do not share an atomic transaction.
