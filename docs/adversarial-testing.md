# An adversarial testing agent

The fixed laboratory answers known questions. The testing agent searches for a
counterexample, uses execution feedback to choose further inputs, and reduces
the result to a replayable case.

This planner is **deterministic and model-free**, not an LLM integration. It uses
behavioral-transition feedback, not model-generated judgments of correctness.
It is a small domain-specific testing agent, not a general-purpose test-writing
framework or a claim of algorithmic novelty.

## Run the demo

```sh
uv run python examples/adversarial_testing.py --case-out counterexample.json
```

The demo shows the discovered sequence, deletion probes, independent provider
state at every step, and the identical reduced input against both targets.
Safety and completion are separate: a blocked duplicate can leave the original
operation unresolved. An additional recovery sequence is labeled as a different
input, not silently added to the matched comparison.

Omit `--case-out` to leave no replay artifact. Temporary trial databases are
removed in either mode.

## Search and replay from the CLI

```sh
uv run aegentra test-agent --case-out counterexample.json > search.json
uv run aegentra replay-test counterexample.json --target unsafe_retry > unsafe.json
uv run aegentra replay-test counterexample.json --target aegentra > guarded.json
```

Search defaults to the unsafe target and the `duplicate_effect` goal. Set
`--target aegentra` to explore the guarded implementation. Replay always requires
an explicit target. All CLI reports are JSON on stdout; operational errors go
to stderr.

| Exit | Meaning |
|---|---|
| `0` | No invariant violation observed in this bounded search or replay. Not a safety proof. |
| `3` | At least one invariant violation observed, including violations other than the requested goal. |
| `1` | An operational error, including a malformed artifact or an unwritable output file. |
| `2` | An argument error. |

Search supports `--max-trials`, `--max-steps` (0-32), `--max-shrink-trials`, and
`--goal` (`duplicate_effect`, `invalid_effect`, or `false_terminal_claim`).
These goals select which failure ends search and is reduced; every trial still
checks all three invariants.

`--case-out` saves the best reduced case within the shrink allowance, only if the
requested goal is found. A zero allowance can leave the original case unchanged.
The report's `case_file` is null if nothing was saved. Existing paths are never
overwritten; publication is not atomic or guaranteed durable across a host crash.
If writing fails, the CLI reports an error rather than emitting a success report.

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
minimization probes. Defaults are 64 exploration trials, eight actions, and 64
shrink probes. Reports count exploration and shrinking separately.
They include the configured budget, the requested goal, and all invariant types
observed during exploration. If another invariant fails but the requested goal
is not found, the status is `other_failure_found`, not a clean search.
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
