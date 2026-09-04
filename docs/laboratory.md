# A small failure laboratory

```sh
uv run aegentra lab > report.json
uv run aegentra lab --max-observations 1 > one-lookup.json
```

The laboratory runs five refund scenarios against three explicit policies. It
uses disposable local SQLite files, requires no credentials, and removes those
files before returning. JSON includes per-case logical traces and totals over
**all assigned cases**, including rejected and unresolved cases.

This is a set of executable counterexamples, not a model benchmark, latency test,
or statistical reliability estimate. Repeated runs produce the same report.
Provider receipt IDs are real sandbox artifacts but are excluded from this report.

## Policies and matched inputs

| Policy | Decision after an ambiguous write |
|---|---|
| `fresh_retry` | Submit the scripted alternate-tool proposal with a new ID; trust its acknowledgment. This is deliberately unsafe, not a strong baseline. |
| `observe_or_stop` | Preserve the original business obligation, decline the conflicting proposal, observe the original request, and stop if evidence remains insufficient. |
| `effect_boundary` | Submit that same proposal through Aegentra, then use bounded read-only recovery of the original request. |

All three receive the same business identity, trusted scope mapping, payload,
provider contracts, alternate proposal, lookup allowance, and evaluation horizon.
The two baselines use the same adapter envelope without a durable runtime.
No method is deprived of the stable identity given to Aegentra.

The competent baseline is expected to match Aegentra on these single-process
cases. The distinction is where the obligation is enforced: handwritten policy
versus a boundary that rejects a conflicting planner proposal. This experiment
does **not** compare restart durability or establish superiority to durable
workflow engines, native provider idempotency, or other recovery libraries.

## Frozen scenario set: `refund-replanning/1`

| Scenario | First dispatch | Read-only lookup |
|---|---|---|
| `acknowledged` | Applies and acknowledges. | Confirms applied. |
| `lost_acknowledgment` | Applies; response is unknown. | Confirms applied. |
| `delayed_commit` | Becomes pending; response is unknown. | Pending until logical step 3, then confirms applied. |
| `rejected` | Permanently rejects before applying. | Confirms final rejection. |
| `unobservable` | Applies; response is unknown. | Always inconclusive. |

Step 0 is the initial dispatch. At step 1, an unresolved outcome receives the
same `refund_order` proposal under a fresh ID and approval reference. Its scope
still denotes the original order's refund. If allowed to dispatch, that alternate
call applies and acknowledges.

Lookups begin at step 2 and advance one logical step each. A separate provider
driver commits the delayed original request at step 3, **before** a lookup at that
step. It also advances after a policy stops: an earlier client decision does not
cancel a pending provider action. All policies are evaluated at the common horizon
`max(4, max_observations + 2)`. There is no wall-clock fault scheduling.

With one lookup, a careful policy stops unresolved at step 2 in the delayed case;
the original refund still commits at step 3. With two lookups it can confirm that
refund. The unsafe policy's earlier acknowledgment does not hide the late duplicate.

## What is counted

`write_calls` and `observation_calls` count adapter invocations, not SQL statements.
Acknowledgment processing inside a write is not another lookup. A proposal is
not a write: `prevented_writes` counts policy declines and boundary blocks, while
`boundary_blocks` counts only the latter.

The evaluator reads actual refunds and pending requests only after the policy has
stopped and the provider has advanced to the horizon. It is not an agent tool.
`duplicate_effects` is excess refunds beyond the one authorized business effect.
`invalid_effects` counts refunds with a wrong tenant, account, resource, amount,
or currency. These counts can overlap; they are not disjoint error categories.

`valid_completion` requires an applied claim, exactly one matching refund, no
invalid refund, and no pending request. `correct_resolution` also includes a
truthful final non-applied claim.
A rejection is therefore a correct resolution, **not** a completed refund.
`false_terminal_claims` counts an applied or final non-applied claim inconsistent
with horizon ground truth. Unknown outcomes make no terminal effect claim; they
are neither successful completions nor false claims. Pending requests prevent
either definitive conclusion.

These cases do not cover partial effects, multiple resources, dishonest adapters,
scope mistakes, or arbitrary planner text. Add new scenario sets rather than
silently changing this one's contract. Any paper needs held-out tasks, strong
external baselines, methods-level related work, and uncertainty estimates beyond
these deterministic cases.
