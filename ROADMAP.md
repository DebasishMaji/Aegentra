# Contributor roadmap

The goal is a small, auditable boundary for uncertain external effects, not an
all-purpose agent framework. Current functionality and evidence are described
in the [README](README.md); the work below is proposed or in progress, **not
already delivered**.

Issue threads are the source of truth for status, assignees, design decisions,
and linked pull requests. These stages express priorities, not release dates,
mandatory dependencies, or promises that every proposal will merge.

## 1. Make safe use and contract coverage easier

| Opportunity | Starting point |
|---|---|
| [CLI diagnostic automation](https://github.com/DebasishMaji/Aegentra/issues/1) | A bounded documentation task for a first contribution. |
| [Literal-percent/Unicode path coverage](https://github.com/DebasishMaji/Aegentra/issues/2) | Check the assigned volunteer and [PR #5](https://github.com/DebasishMaji/Aegentra/pull/5) before starting overlapping work. |
| [Adapter observation decision table](https://github.com/DebasishMaji/Aegentra/issues/6) | Document existing methods and trusted finality assertions; good first issue. |
| [Operator triage runbook](https://github.com/DebasishMaji/Aegentra/issues/7) | Evidence-preserving actions for uncertainty and storage failures. |
| [Focused macOS storage CI](https://github.com/DebasishMaji/Aegentra/issues/8) | Continuous coverage beyond the existing Linux matrix. |

Evidence gate: runnable examples and deterministic regressions must preserve
intent binding, unresolved obligations, read-only recovery, and snapshot
quarantine. A format or filesystem error must not become permission to start
fresh business state.

## 2. Establish measured adapter and scaling evidence

| Opportunity | Evidence required |
|---|---|
| [Unresolved-scope lookup benchmark](https://github.com/DebasishMaji/Aegentra/issues/3) | Correct hit/miss/scoping results, reproducible environment and timings, seeding reported separately. |
| [Adapter conformance scaffold](https://github.com/DebasishMaji/Aegentra/issues/4) | Independent provider-effect counts and deliberately incorrect adapters that the scaffold detects. |

Benchmarks are opt-in engineering measurements, not fixed wall-clock CI gates
or research novelty. Adapter scenarios must retain ambiguous/late effects and
show their observation limits; passing a fake provider does not certify a
real endpoint. Design discussion precedes reusable public interfaces.

## 3. Demonstrate honest application composition

[Compose evidence-gated plans with unresolved-effect protection](https://github.com/DebasishMaji/Aegentra/issues/9)
is an opt-in application example with Revisyn. It must expose the gap between a
memory read and an external write rather than claiming cross-store atomicity.
The issue is the single tracking thread for the shared example.

Evidence gate: scripted, independently counted outcomes; restart against the
same live journal; an explicit counterexample for evidence changing after a
read; no new core dependency or combined exactly-once/freshness guarantee.

## What is deliberately not promised

There is no planned date for production certification, a stable 1.0 API,
automatic journal restoration, generic write retries, Windows support,
distributed coordination, or provider connectors. Proposals require evidence
and explicit scope agreement, not a roadmap checkbox.

Research publication is a separate process governed by the
[research evidence gates](docs/research.md). Community PRs, issue count, or a
matching local baseline do not establish novelty or adoption.

To start, read [CONTRIBUTING.md](CONTRIBUTING.md), the
[governance policy](GOVERNANCE.md), and the
[code of conduct](CODE_OF_CONDUCT.md). Report sensitive findings through
[SECURITY.md](SECURITY.md).
