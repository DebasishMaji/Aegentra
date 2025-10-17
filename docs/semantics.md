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
| `UNRESOLVED` | Recorded an inconclusive observation. |
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

## Storage assumptions

The journal uses SQLite WAL mode and `synchronous=FULL` on a local filesystem.
These are not claims about arbitrary disks, network filesystems, or hardware
that does not honor synchronization.

The journal schema is versioned. An unknown version or an unrelated database
fails explicitly. Existing operations pin the tool name and contract version;
new adapter code must not silently reinterpret an old unresolved record.

New database and lock files use mode `0600`. Existing permissions are preserved.
Arguments and receipts are plaintext. File permissions do not provide encryption
or tenant-level access control. Use deployment-level protections and keep
credentials out of payloads.

No automatic retention deletes unresolved operations. The alpha has no pruning
API, backup coordination, or supported schema migration path.

## Deliberate limits

There are no automatic write retries, provider idempotency-window calculations,
compensation, operator override API, distributed workers, or generic semantic
scope inference in this version.

Adding retries is not a backoff feature. It would require attempt-level evidence,
provider-key scope, finality rules, expiry/acceptance-time assumptions, and
additional counterexamples before it could be enabled.
