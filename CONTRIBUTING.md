# Contributing

Contributions are licensed under Apache 2.0, as described in [LICENSE](LICENSE).

## Find a task and open a pull request

First-time contributors are welcome. Browse
[good first issues](https://github.com/DebasishMaji/Aegentra/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22)
for small documentation and test tasks, or
[help wanted](https://github.com/DebasishMaji/Aegentra/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22help%20wanted%22)
for deeper work. Read the issue's acceptance criteria and comment with your
intended approach so others can coordinate. A comment is not an exclusive
reservation or a promise of immediate maintainer review.

Fork the repository, clone your fork, and create a branch with
`git switch -c your-change`. Make a small, focused change, run the applicable
checks below, push the branch to your fork, and open a pull request against
`DebasishMaji/Aegentra:main`. Link the issue and include a minimal failure trace
or documentation rationale. Draft pull requests are welcome for early feedback.

Discuss public API, schema, adapter-contract, dependency, and benchmark changes
in an issue before expanding the implementation. Bug reports should use
synthetic data and the issue template; never upload credentials, private
journals, real receipts, or personal data.

## Development and contract checks

Start with a failure trace, not another abstraction.

Changes to dispatch, intent binding, or finality should include a small scenario
showing the old behavior and the expected external effects. A timeout is not
proof of failure; a fresh planner call ID is not necessarily a fresh business
operation.

Set up the development environment with `uv sync --locked --extra langgraph`, then run:

```sh
uv run --extra langgraph pytest
uv run --extra langgraph ruff check .
uv run --extra langgraph ruff format --check .
uv run --extra langgraph mypy
uv run --extra langgraph aegentra lab
uv run --extra langgraph python examples/langgraph_refund.py
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
Never open and close an existing database file outside SQLite: POSIX close can
release other connections' locks in the same process. Provision only a closed
private file, and preserve the child-process lock-retention regression.
Arguments and receipts must remain opt-in in exported traces. Exercise the
installed wheel as well as the editable checkout when changing packaging or CLI
entry points.

Optional integrations must not enter the core import path. Exercise their tools
inside a compiled graph, including invalid arguments, cancellation, and provider
exceptions. A framework-level successful tool response is not a confirmed
business effect. Never let a model choose the operation ID, approval reference,
scope, or approved payload through the bound-operation tool.

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
