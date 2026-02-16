# Contributing

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
