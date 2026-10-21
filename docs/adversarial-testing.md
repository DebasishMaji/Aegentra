# An adversarial testing agent

The fixed laboratory answers known questions. The testing agent searches for a
counterexample, uses execution feedback to choose further inputs, and reduces
the result to a replayable case.

This planner is **deterministic and model-free**, not an LLM integration. It uses
behavioral-transition feedback, not model-generated judgments of correctness.
It is a small domain-specific testing agent, not a general-purpose test-writing
framework or a claim of algorithmic novelty.

## Python API

Inside an async function:

```python
from aegentra.adversary import FaultSearchAgent, Target, run_trial, save_case

report = await FaultSearchAgent().search()
if report.reduction is not None:
    case = report.reduction.result.case
    save_case(case, "counterexample.json")  # Refuses to overwrite an existing file.
    defended = await run_trial(case, target=Target.AEGENTRA)
    print(defended.violations)
```

`SearchBudget` sets three independent limits: `max_trials` for exploration,
`max_steps` for actions after the initial dispatch, and `max_shrink_trials` for
minimization probes. Reports count exploration and shrinking separately.
They include the configured budget, the requested goal, and all observed
invariant types. If another invariant fails but the requested goal is not found,
the status is `other_failure_found`, not a clean search.
There is no cost hidden outside those counts; a caller's additional comparative
replay is a separate trial.

## The loop

The control case starts with an acknowledged request and benign observation/time
advance actions, not a preloaded failing trace.

The agent runs that case, records new behavioral transitions, and queues mutations
of interesting cases. Mutations change the fault scenario, replace or delete an
action, or insert an action at a different position. Cases are deduplicated.
Uninteresting cases do not expand the frontier. These signatures describe
observable behavior, **not source-code coverage**.

When the requested invariant fails, a reducer removes individual actions and
replays each candidate from fresh state. It accepts a deletion only if the
**same invariant** still fails. After an accepted deletion it restarts the scan,
because earlier actions may have become unnecessary.

`one_minimal=True` means no single remaining action can be deleted while
preserving that failure, under the same scenario and target. It does not mean
globally shortest, minimal fault configuration, or that no alternative failing
trace exists. If the shrink allowance runs out, minimality is not asserted.

## Versioned harness: `refund-adversary/1`

Each trial creates a fresh refund provider and, for the Aegentra target, a fresh
journal in temporary local storage. The business intent is one USD 50.00 refund
for the fixed synthetic order. Receipt identifiers and wall-clock timestamps are
excluded from reports.

The initial dispatch uses one of the five existing sandbox scenarios.
Subsequent actions are:

| Action | Meaning |
|---|---|
| `observe` | Look up the original request if its outcome is still unknown. |
| `advance` | Let a delayed original request commit, without giving the client evidence. |
| `restart` | Reopen Aegentra's journal, or clear the unsafe client's memory-only outcome. |
| `replan` | If the client still considers the task unresolved, propose the same effect through `refund_order` with a new operation ID and approval reference. |

Both targets stop proposing writes once their client state is settled. This
application-level completion gate matters: Aegentra does not prohibit a new,
independently authorized write after the previous scope has settled.

The unsafe client submits the fresh write. Aegentra may block it through its
durable effect boundary. Both receive the same business identity, payload, tool
contracts and action sequence. The unsafe client is intentionally defective; it
is not a strong research baseline. The existing fixed laboratory retains the
competent observe-or-stop comparison.

Restart is a cooperative close/reopen, **not process termination**. Existing
subprocess crash tests cover that different boundary.

After the last client action, the driver drains the pending original request and
records a final horizon event. Removing an `advance` action cannot hide a late
effect from the evaluator. This is a finite, closed-world provider model, not a
claim that real requests always settle within a known horizon.

## Independent oracle

Only the evaluator reads actual refunds and pending provider requests. The
client's acknowledgments and knowledge assertions do not establish correctness.
The same evaluator is used by the fixed laboratory.

It checks every prefix and the horizon for:

- More than one external effect for the one authorized business intent.
- A refund with the wrong tenant, account, resource, amount, or currency.
- An applied or final non-applied claim inconsistent with actual and pending effects.

A failure witness records the first step violating each invariant. Later
uncertainty does not erase an earlier false claim. Unknown outcomes are not
violations, but they are not successful completions either.

## Replay and limits

Artifacts contain only a version, the harness ID, a scenario, and up to 32
whitelisted actions. Parsing rejects unknown fields, unknown actions, duplicate
JSON keys, unsupported versions, and oversized files. Artifacts cannot name
provider credentials, load adapters, or execute arbitrary code. Saved files use
owner-only permissions.

All trial databases are disposable. There is no production target, LLM API,
learned policy, concurrency scheduler, partial-effect model, or generic protocol
inference in this version. Unexpected adapter errors propagate; they are not
reported as clean trials.

`no_failure_found` means only that this bounded search found no counterexample.
The frontier is heuristic and can discard uninteresting paths. Neither a budget
limit nor an exhausted behavioral frontier proves the runtime safe.
