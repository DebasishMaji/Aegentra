# Execution model

This document describes the current alpha, not a future recovery system.

## The invariant

For a namespaced operation identity, the runtime reserves a possible dispatch
once. Every later `perform` either returns the settled outcome or calls
`Tool.observe`. It never calls `Tool.execute` again.

The namespace is `(tenant, provider account, operation ID)`. The conflict scope is
`(tenant, provider account, resource, effect)`. They serve different purposes.

An unresolved scope prevents a fresh operation from dispatching through the same
boundary, even if its tool name or approval reference differs. Unrelated scopes
can proceed. Once a scope is settled, the application's business rules decide
whether a later, independently authorized action is legitimate.

This is not global semantic deduplication. An application that invents another
resource or effect name can bypass its own conflict mapping.

## The write boundary

```text
validate and snapshot arguments
BEGIN IMMEDIATE
  check existing identity and intent binding
  check unresolved effect scope
    if blocked: append effect_blocked event to the blocking operation and COMMIT
                return without reserving or invoking the proposed write
  insert UNKNOWN / RESERVED
  append dispatch_reserved event
COMMIT
invoke adapter.execute
BEGIN IMMEDIATE
  bind observation to operation and payload
  record knowledge, receipt, and event
COMMIT
```

No provider call occurs while a journal transaction is held.

Validation happens before reservation. Arguments are copied through deterministic
JSON encoding before the first await; the provider receives the recorded snapshot.
Mapping key order does not affect identity. Missing keys, explicit nulls, and
different strings remain different. Floats, custom objects, and non-string keys
are rejected rather than normalized by guesswork.

`approval_ref` is part of intent binding. It is not cryptographic authorization.
The application must authenticate and authorize the exact payload before
constructing an operation. Do not let the planner allocate approvals or scopes.

## Knowledge is not lifecycle

| Knowledge | Meaning |
|---|---|
| `UNKNOWN` | Neither applied nor finally excluded. |
| `APPLIED` | Trusted adapter evidence confirms the intended historical effect. |
| `NOT_APPLIED_FINAL` | Trusted adapter evidence excludes execution, including a late commit. |

| Phase | What the runtime has done |
|---|---|
| `RESERVED` | Committed a dispatch reservation; execution may or may not have begun. |
| `RECONCILING` | Started a read-only observation. |
| `UNRESOLVED` | Recorded an inconclusive observation or exhausted a recovery budget. |
| `SETTLED` | Recorded applied or final non-applied evidence. |

An abrupt exit or unexpected adapter exception may leave `RESERVED` or
`RECONCILING` behind. That is not evidence that the provider stopped.

Provider rejection is final only if the adapter's contract excludes later
execution. A timeout, cancellation, transport error, or negative status lookup
does not establish that by itself.

Settled operations are immutable through this API. A later refund reversal or
other business action is not represented by rewriting the historical result.

## Evidence boundary

An `Observation` binds an operation key and payload digest to a knowledge claim,
a reason, and an applied receipt where appropriate. The journal checks that
binding. A different identity or payload raises `EvidenceMismatch` and leaves
the original reservation uncertain.

This binding catches integration mistakes; it does not authenticate a provider.
The adapter is responsible for authoritative sources and request correlation.
The sandbox has no webhook ingestion. A future callback adapter must authenticate
callbacks and handle stale or reordered delivery explicitly.

The event history records transitions and their reasons. It is not a signed,
tamper-proof audit log and does not retain arbitrary raw provider responses.

## Blocked proposals

An `effect_blocked` event belongs to the operation retaining the unresolved scope,
not to the rejected proposal. Its `BlockedAction` metadata carries the proposed
operation key, tool name, and contract version. It does not store the rejected
arguments or approval reference.

The scope check and event write share a transaction. The runtime returns
`BlockedByUnresolvedEffect` only after that decision commits. A journal write
failure propagates; it does not dispatch the proposed tool or claim the decision
was durably recorded.

Blocking does not change the original operation's knowledge, phase, or timestamps.
Repeated proposals produce separate ordered events, including repeated proposals
with the same new ID. A blocked proposal is not an execution attempt and has no
operation row. Its history is available through the blocking operation's key.

When reconciliation settles the original operation, the recorded proposals remain
in its history. They are not automatically executed afterward. Cross-tenant or
unrelated-scope operations do not produce blocked events on this operation.

## Locate an unresolved operation by scope

If a caller retained the business scope but lost the operation ID, it need not
propose a new write just to discover the blocker:

```python
from aegentra import EffectScope

scope = EffectScope("tenant-a", "payments", "order-42", "refund")
pending = runtime.find_unresolved(scope)
if pending is not None:
    outcome = await runtime.reconcile(pending.key, tool=refund_tool)
```

The read matches every scope field and returns the one `UNKNOWN` operation, or
`None` if no unresolved holder exists. It includes reserved and reconciling
operations as well as the unresolved phase. It neither reserves nor validates
a dispatch, observes the provider, nor changes records or audit history.
Reconciliation still requires the original adapter name and contract version.

This is a point-in-time lookup, not authorization or permanent deduplication.
`None` does not establish that the business effect never happened: settled
operations remain in history but are not returned. Every proposed write must
still pass `perform`'s transactional boundary and application authorization.
Scope selection remains trusted configuration, not tenant-level access control.

`Inspection.find_unresolved` performs the same query in its pinned audit view,
including on legacy journals. Refresh that reader before interpreting later
writer changes; historical inspection cannot authorize a new effect.

## Crash boundaries

| Interruption | Recovery |
|---|---|
| Before the reservation commits | No dispatch is authorized by the runtime. |
| After reservation, before actual send | Conservatively uncertain; never execute again. |
| During the provider request | Read-only observation or remain unresolved. |
| After provider commit, before journal confirmation | Confirm through provider evidence; do not repeat the write. |
| After journal confirmation | Return the stored outcome. |

The pre-send case deliberately sacrifices liveness. Without final evidence, this
alpha cannot distinguish "reserved but never sent" from "sent without a response."

Cancellation and adapter exceptions propagate unchanged. The durable reservation
already protects the boundary; there is no broad exception handler turning an
error into a success-shaped result. Callers must inspect or reconcile the original
operation instead of creating a new write.

## Ownership and concurrency

One OS process owns a journal via a nonblocking advisory file lock. The lock file
is retained after close to avoid an inode-replacement race. Closing releases the
lock. A second runtime, including one in the same process, is rejected.

SQLite transactions and a partial unique index enforce one unresolved operation
per effect scope. An in-process active set prevents parallel reconciliation from
overwriting the outcome of a still-running adapter call. A concurrent call for
that same live operation returns `Unresolved`; another operation in its scope
returns `BlockedByUnresolvedEffect`.

There are no TTL leases or automatic ownership takeovers. Losing a local owner
does not cancel a remote request.

The runtime is confined to one process, thread, and event loop. It refuses to
close while adapter calls are active. Cancel and await tasks before shutdown.

`recover` keeps an operation active across its bounded read-only polling sequence,
including waits. Exhaustion preserves `UNKNOWN` and appends `recovery_exhausted`;
it never permits another dispatch. See [recovery budgets](recovery.md).

Operators can use a separate [read-only inspection snapshot](inspection.md) without
taking ownership. That connection cannot dispatch, migrate, or settle operations.

## Storage assumptions

The journal uses SQLite WAL mode and `synchronous=FULL` on a local filesystem.
These are not claims about arbitrary disks, network filesystems, or hardware
that does not honor synchronization.

The journal schema is versioned. Schema 1 journals are upgraded to schema 2 on
open, adding structured blocked-proposal metadata to events. The upgrade and
version marker commit in one transaction. A failure rolls back the schema changes
and leaves the old version intact. Existing outcomes, payloads, timestamps, and
event sequence numbers are preserved; old events have no blocked-action metadata.

An unknown version or an unrelated database fails explicitly. Existing operations
pin the tool name and contract version; a journal upgrade does not change those
contracts or make an unresolved operation retryable. An older runtime cannot
open the upgraded schema. Capture an inspection archive before changing versions;
it is evidence for operators, not automatically safe rollback state.

New database and lock files use mode `0600`. Existing permissions are preserved.
Arguments and receipts are plaintext. File permissions do not provide encryption
or tenant-level access control. Use deployment-level protections and keep
credentials out of payloads.

No automatic retention deletes unresolved operations. The alpha has no pruning
API, snapshot promotion, or downgrade path. `Runtime(..., require_existing=True)`
rejects missing or uninitialized journals rather than creating fresh authority.
The default still permits deliberate first-time provisioning.

Read-only inspection validates supported layouts without migrating. Consistent
full-database snapshots use SQLite backup and an inspection-only application/version
marker that old and current writers reject. WAL activation is checked explicitly.
See [operations](operations.md) for atomic publication, data protection,
cooperative time budgets, and the hazards of restoring old provider obligations.

## Deliberate limits

There are no automatic write retries, provider idempotency-window calculations,
compensation, operator override API, distributed workers, or generic semantic
scope inference in this version.

Adding retries is not a backoff feature. It would require attempt-level evidence,
provider-key scope, finality rules, expiry/acceptance-time assumptions, and
additional counterexamples before it could be enabled.
