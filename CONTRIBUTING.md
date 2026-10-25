# Contributing

Contributions are licensed under Apache 2.0, as described in [LICENSE](LICENSE).

Start with a failure trace, not another abstraction.

Changes to dispatch, intent binding, or finality should include a small scenario
showing the old behavior and the expected external effects. A timeout is not
proof of failure; a fresh planner call ID is not necessarily a fresh business
operation.

Set up the development environment with `uv sync --locked`, then run:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run aegentra lab
uv build
```

Keep provider writes separate from the journal transaction. Do not add generic
retry wrappers, convert unknown outcomes into final failures, or swallow adapter
exceptions to make a scenario pass.

An adapter needs documented endpoint/version semantics and counterexamples for
its finality claims. A library name, annotation, or successful sandbox request
does not establish a provider guarantee.

Use exact values in operation payloads. If normalization changes business meaning,
reject the input instead. Keep credentials out of arguments, receipts, fixtures,
and screenshots.

Read-only inspection must stay read-only, including for legacy journals.
Arguments and receipts must remain opt-in in exported traces. Exercise the
installed wheel as well as the editable checkout when changing packaging or CLI
entry points.

The laboratory's `refund-replanning/1` schedule is a versioned experimental
contract. Give every policy the same identity and budgets. Do not move a late
effect outside the evaluation horizon, count a rejection as a completed refund,
or remove the competent baseline because it matches the boundary.

The testing agent's `refund-adversary/1` harness has its own replay contract.
Preserve deterministic case interpretation, check actual provider effects
independently of client claims, and replay every shrink candidate from fresh
state. A reducer must preserve the requested invariant, not merely any failure.
Do not imply that an exhausted search proves safety or hide a recovery lookup
inside a supposedly identical comparison trace.
