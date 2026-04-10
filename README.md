# Aegentra

A timeout tells you what the client saw, not what the provider did.

Aegentra keeps an unresolved tool operation from becoming a fresh agent action.
It records the intended operation before dispatch, blocks conflicting writes in
the same declared effect scope, and reconciles through provider evidence.

**Research alpha.** Python 3.11+, macOS and Linux. No runtime dependencies.
This version permits one possible dispatch per operation. It does not retry writes.

## The failure that matters

```text
Agent calls refund_payment()
Provider creates the refund
The acknowledgment is lost
Agent replans and calls refund_order() with a new ID
```

Deduplicating the first call's ID is not enough if the planner invents a second
one. Aegentra can block that second write when the application has mapped both
tools to the same business-effect scope:

```text
tenant / provider account / order / refund
```

Scope mappings come from the application and trusted adapters. They are not
inferred from tool names or model judgments.

## Run the example

```sh
uv sync --locked
uv run python examples/refund_recovery.py
```

The example uses two separate SQLite files: the agent journal and a persistent
sandbox provider. The provider applies a refund but loses the response. A fresh
call through another tool is blocked. After the runtime restarts, a read-only
lookup confirms the original refund. There is still only one provider effect.

No API key, model, or external service is required.

## The API

Create operations in trusted application code, after authorizing their payloads:

```python
from aegentra import EffectScope, Operation, Runtime

operation = Operation(
    operation_id="refund-42",
    scope=EffectScope("tenant-a", "payments", "order-42", "refund"),
    approval_ref="approval-42",
    arguments={"amount_minor": 5000, "currency": "USD"},
)
```

Inside an async function, supply an adapter implementing `Tool`:

```python
with Runtime("agent.sqlite3") as runtime:
    outcome = await runtime.perform(operation, tool=refund_tool)
```

Handle the outcome explicitly:

| Outcome | What the application may conclude |
|---|---|
| `ConfirmedApplied` | The adapter supplied matching applied evidence and a receipt. |
| `ConfirmedNotApplied` | The adapter established that this attempt cannot execute later. |
| `Unresolved` | The available evidence does not establish either conclusion. |
| `BlockedByUnresolvedEffect` | Another operation has an unresolved effect in this scope. |

Calling `perform` again with the same intent returns a settled result or performs
a read-only reconciliation. It never calls `execute` again. Changing that
operation's payload, approval reference, or scope raises `IntentConflict`.
Recovery requires the original tool name and contract version.

`runtime.inspect(operation.key)` returns the durable record.
`runtime.events(operation.key)` returns its ordered event history.
`await runtime.reconcile(operation.key, tool=refund_tool)` performs an explicit
read-only lookup.

Blocked proposals are recorded as `effect_blocked` events on the **blocking**
operation. Their `blocked_action` field identifies the requested operation, tool,
and contract version. The rejected payload and approval reference are not copied
into the event, and no dispatch is reserved for that proposal.

```python
for event in runtime.events(operation.key):
    if event.blocked_action is not None:
        proposal = event.blocked_action
        print(proposal.requested_key.operation_id, proposal.tool_name)
```

These decisions survive restart and remain in the history after the original
operation is reconciled.

## Writing an adapter

An adapter implements four things:

- `name` and `contract_version`: the route and semantics pinned to this operation.
- `validate(operation)`: reject invalid arguments or an inappropriate declared
  scope before anything is reserved.
- `execute(record)`: make one possible write and return an `Observation`.
- `observe(record)`: collect evidence without making another write.

Use `Observation.unknown(...)` for inconclusive responses. Reserve
`Observation.not_applied(...)` for evidence that excludes late execution—not a
missing record from an eventually consistent endpoint.

The runtime checks observation identity and payload binding. It cannot verify
that an adapter's statement about the provider is truthful. Adapters are trusted
integration code, not model-generated assertions.

See [`RefundTool`](src/aegentra/sandbox.py) for a small working implementation.
The sandbox deliberately has no provider-side deduplication, so repeated writes
remain visible rather than hiding a runtime defect.

## What this version does not promise

- **Not exactly-once execution.** A crash before the actual send can leave an
  operation unresolved even when the provider did nothing.
- **Not an authorization service.** `approval_ref` is an application-owned audit
  reference, not a verified permission token.
- **Not semantic deduplication.** Undeclared equivalent effects and writes that
  bypass the boundary are outside the model.
- **Not automatic truthfulness.** The application must gate completion messages;
  the runtime cannot constrain arbitrary model text.
- **Not a transaction across systems.** The local journal and external provider
  do not share a commit.
- **Not a production payment integration.** The refund provider is a scenario
  fixture, not a Stripe adapter or a financial correctness claim.

One process owns a journal at a time. Use local storage, one thread, and one event
loop per runtime. Await or cancel active work before closing. Exceptions and
cancellation are propagated; a reserved operation remains uncertain afterward.
Do not interpret an exception as permission to submit a new write.

The database contains arguments and receipts in plaintext. New files are created
with owner-only permissions, but this is not encryption. Do not put credentials
in arguments. Existing file permissions are not changed.

The execution model, crash boundaries, and limits are in
[`docs/semantics.md`](docs/semantics.md).
Journals from the initial schema are upgraded transactionally on open; unsupported
versions still fail explicitly.

## Research direction

The initial question is narrow: **does an agent preserve an unresolved business
obligation when it replans?**

Durable execution, idempotency, and in-doubt states already exist. Aegentra does
not claim to have invented them. The planned evaluation separates scope
enforcement from recovery-policy quality, with existing durable runtimes and
competent hand-written reconciliation as baselines.

The current sandbox is not yet that benchmark. There are no published comparative
results or established novelty claims. See [`docs/research.md`](docs/research.md).

## Development

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

Behavioral cases include lost acknowledgments, delayed commits, new-ID and
alternate-tool replanning, cancellation, and abrupt subprocess exit on both
sides of the provider commit.

See [CONTRIBUTING.md](CONTRIBUTING.md) before changing dispatch or finality rules.

## License

Licensed under [Apache 2.0](LICENSE). See [NOTICE](NOTICE) for attribution.