# What would count as evidence

The working question is whether an agent preserves a business obligation when a
tool outcome is unknown. Tool-call success alone is the wrong unit: a task can
look complete while an earlier request is still capable of creating another
effect.

## Candidate contribution

Measure new-ID and alternate-tool replanning after ambiguous external writes.
Then distinguish the effect of an explicit scope boundary from the quality of
the recovery policy inside it.

This is a research direction, not an established novelty claim. A state machine
and a SQLite journal are not, by themselves, a new research result.

## Current artifact

The repository contains a conservative reference runtime and a persistent
scenario provider. It has no LLM integration, comparative benchmark results,
learned recovery policy, or production provider adapter.

The provider can acknowledge, lose the acknowledgment, commit after a timeout,
reject before execution, or offer no usable outcome lookup. Its evaluator view
is separate from the agent-facing `Tool` protocol. It deliberately does not
deduplicate writes.

That is enough to make important counterexamples executable. It is not enough
to substantiate a broad reliability claim.

## Evaluation plan

Compare correctly configured durable execution and provider idempotency, a pinned
SafeAct integration, competent domain reconciliation, fixed observation policies,
and conservative escalation. Give each method the same metadata and permitted
interfaces. Do not make a baseline lose because it lacks the stable business
identity supplied to the proposed method.

Where adaptive verification is introduced, compare classical policies before
assuming an LLM is necessary. Keep the deterministic write boundary identical
when measuring policy quality.

The primary metrics are valid task completion, invalid/duplicate effects, false
success claims, unresolved operations, human interventions, cost, and false
blocking. Report costs per assigned task, not only per successful task.

Held-out scenarios must be frozen before policy tuning. Account for ordinary
model variation with repeated fault-free runs. Observe late effects through a
declared horizon; a still-pending request is censored or unresolved, not safe by
default. Report uncertainty at the workflow/fault-family level. Zero observed
violations is not a universal proof.

## Decision gates

Before claiming a paper contribution, complete a methods-level related-work
comparison and reproduce strong baselines.

Before adding more integrations, show that an independent engineer needs the
boundary and can use it without author assistance.

If an existing project can express these scenarios adequately, contributing the
cases or adapter contracts upstream is preferable to maintaining another runtime.

## Related work

- [Temporal activities and idempotency](https://docs.temporal.io/activity-definition)
- [Restate](https://github.com/restatedev/restate)
- [DBOS](https://github.com/dbos-inc/dbos-transact-py)
- [SafeAct](https://github.com/tarunagarwal1981/safeact/tree/6305ac4fb7817d684f9eedcf262f707110e0723a)
- [AgentChaos](https://arxiv.org/abs/2608.06790)
- [Loud Failures, Quiet Failures](https://arxiv.org/abs/2610.10062)
- [KnowNo](https://arxiv.org/abs/2307.01928)

These are starting points, not an exhaustive review or a claim that each system
has been evaluated in this repository.
