"""Feedback-guided fault search with an independent oracle and deletion-based shrinking.

The planner is deterministic and model-free. All writes stay inside disposable
sandbox databases; this module cannot load or exercise production adapters.
"""

from __future__ import annotations

import json
import os
from collections import deque
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import assert_never

from ._testing import RefundEvaluation, evaluate_refund, unmanaged_record
from .errors import AegentraError
from .models import (
    BlockedByUnresolvedEffect,
    EffectScope,
    JSONValue,
    Knowledge,
    Observation,
    Operation,
    OperationRecord,
    Outcome,
)
from .runtime import Runtime
from .sandbox import RefundLedger, RefundTool, Scenario

HARNESS = "refund-adversary/1"
MAX_ACTIONS = 32
_MAX_CASE_BYTES = 16_384


class Action(StrEnum):
    OBSERVE = "observe"
    ADVANCE = "advance"
    RESTART = "restart"
    REPLAN = "replan"


class Target(StrEnum):
    UNSAFE_RETRY = "unsafe_retry"
    AEGENTRA = "aegentra"


class Invariant(StrEnum):
    DUPLICATE_EFFECT = "duplicate_effect"
    INVALID_EFFECT = "invalid_effect"
    FALSE_TERMINAL_CLAIM = "false_terminal_claim"


class TestCaseError(AegentraError):
    """A replay artifact is malformed or belongs to an unsupported harness."""


def _count(value: int, name: str, *, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}.")


@dataclass(frozen=True, slots=True)
class FaultCase:
    scenario: Scenario
    actions: tuple[Action, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, Scenario):
            raise TestCaseError("scenario must be a Scenario value.")
        if not isinstance(self.actions, tuple) or not all(
            isinstance(action, Action) for action in self.actions
        ):
            raise TestCaseError("actions must be a tuple of Action values.")
        if len(self.actions) > MAX_ACTIONS:
            raise TestCaseError(f"A case may contain at most {MAX_ACTIONS} actions.")

    def as_dict(self) -> dict[str, JSONValue]:
        actions: list[JSONValue] = [action.value for action in self.actions]
        return {
            "format_version": 1,
            "harness": HARNESS,
            "scenario": self.scenario.value,
            "actions": actions,
        }

    @classmethod
    def from_dict(cls, value: object) -> FaultCase:
        if not isinstance(value, dict) or set(value) != {
            "format_version",
            "harness",
            "scenario",
            "actions",
        }:
            raise TestCaseError("Expected exactly format_version, harness, scenario, and actions.")
        if type(value["format_version"]) is not int or value["format_version"] != 1:
            raise TestCaseError("Unsupported test-case format_version.")
        if value["harness"] != HARNESS:
            raise TestCaseError("Unsupported test-case harness.")
        scenario, actions = value["scenario"], value["actions"]
        if (
            not isinstance(scenario, str)
            or not isinstance(actions, list)
            or not all(isinstance(action, str) for action in actions)
            or len(actions) > MAX_ACTIONS
        ):
            raise TestCaseError("Expected a scenario string and a bounded list of action strings.")
        try:
            return cls(Scenario(scenario), tuple(Action(action) for action in actions))
        except ValueError as exc:
            raise TestCaseError(f"Unknown scenario or action: {exc}") from exc


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TestCaseError(f"Duplicate JSON field: {key!r}.")
        result[key] = value
    return result


def load_case(path: str | Path) -> FaultCase:
    with Path(path).open("rb") as stream:
        payload = stream.read(_MAX_CASE_BYTES + 1)
    if len(payload) > _MAX_CASE_BYTES:
        raise TestCaseError(f"A test-case artifact may not exceed {_MAX_CASE_BYTES} bytes.")
    try:
        value: object = json.loads(payload, object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError) as exc:
        raise TestCaseError(f"Cannot decode test-case JSON: {exc}") from exc
    return FaultCase.from_dict(value)


def save_case(case: FaultCase, path: str | Path) -> None:
    """Create a replay artifact without replacing an existing file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(case.as_dict(), stream, indent=2, sort_keys=True)
        stream.write("\n")


@dataclass(frozen=True, slots=True)
class Violation:
    invariant: Invariant
    step: int

    def as_dict(self) -> dict[str, JSONValue]:
        return {"invariant": self.invariant.value, "step": self.step}


@dataclass(frozen=True, slots=True)
class TrialEvent:
    step: int
    action: str
    knowledge: Knowledge
    actual_effects: int
    pending_requests: int

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "step": self.step,
            "action": self.action,
            "knowledge": self.knowledge.value,
            "actual_effects": self.actual_effects,
            "pending_requests": self.pending_requests,
        }


@dataclass(frozen=True, slots=True)
class TrialResult:
    case: FaultCase
    target: Target
    trace: tuple[TrialEvent, ...]
    violations: tuple[Violation, ...]
    coverage: tuple[str, ...]
    write_calls: int
    observation_calls: int
    blocked_proposals: int
    evaluation: RefundEvaluation

    def violates(self, invariant: Invariant) -> bool:
        return any(item.invariant is invariant for item in self.violations)

    def as_dict(self) -> dict[str, JSONValue]:
        trace: list[JSONValue] = [event.as_dict() for event in self.trace]
        violations: list[JSONValue] = [item.as_dict() for item in self.violations]
        coverage: list[JSONValue] = list(self.coverage)
        return {
            "format_version": 1,
            "kind": "adversarial_trial",
            "case": self.case.as_dict(),
            "target": self.target.value,
            "trace": trace,
            "violations": violations,
            "behavioral_signatures": coverage,
            "write_calls": self.write_calls,
            "observation_calls": self.observation_calls,
            "blocked_proposals": self.blocked_proposals,
            "actual_effects": self.evaluation.actual_effects,
            "pending_at_horizon": self.evaluation.pending_requests,
            "valid_completion": self.evaluation.valid_completion,
            "correct_resolution": self.evaluation.correct_resolution,
        }


@dataclass(slots=True)
class _Calls:
    writes: int = 0
    observations: int = 0


@dataclass(frozen=True, slots=True)
class _CountedTool:
    delegate: RefundTool
    calls: _Calls

    @property
    def name(self) -> str:
        return self.delegate.name

    @property
    def contract_version(self) -> str:
        return self.delegate.contract_version

    def validate(self, operation: Operation) -> None:
        self.delegate.validate(operation)

    async def execute(self, record: OperationRecord) -> Observation:
        self.calls.writes += 1
        return await self.delegate.execute(record)

    async def observe(self, record: OperationRecord) -> Observation:
        self.calls.observations += 1
        return await self.delegate.observe(record)


class _Client:
    def __init__(
        self,
        operation: Operation,
        original: _CountedTool,
        alternate: _CountedTool,
        target: Target,
        journal: Path,
        resources: ExitStack,
    ) -> None:
        self.operation, self.original, self.alternate = operation, original, alternate
        self.journal, self.resources = journal, resources
        self.runtime = (
            resources.enter_context(Runtime(journal)) if target is Target.AEGENTRA else None
        )
        self.record = unmanaged_record(operation, original)
        self.knowledge = Knowledge.UNKNOWN
        self.proposals = 0
        self.blocked = 0

    def _accept(self, outcome: Outcome) -> bool:
        if isinstance(outcome, BlockedByUnresolvedEffect):
            self.knowledge = outcome.blocking_operation.knowledge
            return True
        self.knowledge = outcome.record.knowledge
        return False

    async def start(self) -> None:
        if self.runtime is None:
            self.knowledge = (await self.original.execute(self.record)).knowledge
        elif self._accept(await self.runtime.perform(self.operation, tool=self.original)):
            raise RuntimeError("The isolated initial test operation was unexpectedly blocked.")

    async def observe(self) -> str:
        if self.knowledge is not Knowledge.UNKNOWN:
            return "observe_skipped"
        if self.runtime is None:
            self.knowledge = (await self.original.observe(self.record)).knowledge
        else:
            self._accept(await self.runtime.reconcile(self.operation.key, tool=self.original))
        return "observe"

    async def replan(self) -> str:
        self.proposals += 1
        if self.knowledge is not Knowledge.UNKNOWN:
            return "replan_skipped"
        proposal = replace(
            self.operation,
            operation_id=f"replanned-{self.proposals}",
            approval_ref=f"approval-replan-{self.proposals}",
        )
        if self.runtime is None:
            self.knowledge = (
                await self.alternate.execute(unmanaged_record(proposal, self.alternate))
            ).knowledge
        elif self._accept(await self.runtime.perform(proposal, tool=self.alternate)):
            self.blocked += 1
            return "replan_blocked"
        return "replan"

    def restart(self) -> None:
        if self.runtime is None:
            self.knowledge = Knowledge.UNKNOWN
        else:
            self.runtime.close()
            self.runtime = self.resources.enter_context(Runtime(self.journal))
            self.knowledge = self.runtime.inspect(self.operation.key).knowledge


def _violations(evaluation: RefundEvaluation) -> tuple[Invariant, ...]:
    return tuple(
        invariant
        for invariant, count in (
            (Invariant.DUPLICATE_EFFECT, evaluation.duplicate_effects),
            (Invariant.INVALID_EFFECT, evaluation.invalid_effects),
            (Invariant.FALSE_TERMINAL_CLAIM, evaluation.false_terminal_claims),
        )
        if count
    )


async def run_trial(case: FaultCase, *, target: Target) -> TrialResult:
    """Execute a case from fresh provider and journal state, then drain late work."""
    if not isinstance(target, Target):
        raise TypeError("target must be a Target value.")
    with TemporaryDirectory(prefix="aegentra-adversary-") as directory, ExitStack() as resources:
        root = Path(directory)
        ledger = RefundLedger(root / "provider.sqlite3")
        operation = Operation(
            "refund-42",
            EffectScope("tenant-a", "payments", "order-42", "refund"),
            "approval-42",
            {"amount_minor": 5000, "currency": "USD"},
        )
        calls = _Calls()
        client = _Client(
            operation,
            _CountedTool(RefundTool(ledger, case.scenario), calls),
            _CountedTool(RefundTool(ledger, name="refund_order"), calls),
            target,
            root / "journal.sqlite3",
            resources,
        )
        pending = case.scenario is Scenario.DELAYED_COMMIT
        trace: list[TrialEvent] = []
        witnesses: dict[Invariant, int] = {}

        def record(step: int, action: str) -> RefundEvaluation:
            evaluation = evaluate_refund(ledger, operation, client.knowledge)
            trace.append(
                TrialEvent(
                    step,
                    action,
                    client.knowledge,
                    evaluation.actual_effects,
                    evaluation.pending_requests,
                )
            )
            for invariant in _violations(evaluation):
                witnesses.setdefault(invariant, step)
            return evaluation

        await client.start()
        record(0, "dispatch")
        for step, action in enumerate(case.actions, 1):
            match action:
                case Action.OBSERVE:
                    label = await client.observe()
                case Action.REPLAN:
                    label = await client.replan()
                case Action.RESTART:
                    client.restart()
                    label = "restart"
                case Action.ADVANCE:
                    if pending:
                        ledger.commit_pending(operation.key)
                        pending = False
                    label = "advance"
                case _:
                    assert_never(action)
            record(step, label)
        if pending:
            ledger.commit_pending(operation.key)
        evaluation = record(len(case.actions) + 1, "horizon")

    signatures: set[str] = set()
    previous = "start"
    for event in trace:
        current = f"{event.action}:{event.knowledge.value}"
        signatures.add(f"{case.scenario.value}/{previous}->{current}")
        previous = current
    return TrialResult(
        case=case,
        target=target,
        trace=tuple(trace),
        violations=tuple(Violation(kind, step) for kind, step in witnesses.items()),
        coverage=tuple(sorted(signatures)),
        write_calls=calls.writes,
        observation_calls=calls.observations,
        blocked_proposals=client.blocked,
        evaluation=evaluation,
    )


@dataclass(frozen=True, slots=True)
class SearchBudget:
    max_trials: int = 64
    max_steps: int = 8
    max_shrink_trials: int = 64

    def __post_init__(self) -> None:
        _count(self.max_trials, "max_trials", minimum=1)
        _count(self.max_steps, "max_steps")
        _count(self.max_shrink_trials, "max_shrink_trials")
        if self.max_steps > MAX_ACTIONS:
            raise ValueError(f"max_steps may not exceed {MAX_ACTIONS}.")


@dataclass(frozen=True, slots=True)
class Reduction:
    result: TrialResult
    trials: int
    one_minimal: bool

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "result": self.result.as_dict(),
            "trials": self.trials,
            "one_minimal": self.one_minimal,
        }


async def minimize_failure(
    failure: TrialResult, *, goal: Invariant, max_trials: int = 64
) -> Reduction:
    """Reach a single-deletion fixpoint for the same invariant, or exhaust the budget."""
    _count(max_trials, "max_trials")
    if not failure.violates(goal):
        raise ValueError("The supplied trial does not violate the requested invariant.")
    current, attempts, index = failure, 0, 0
    while index < len(current.case.actions) and attempts < max_trials:
        actions = current.case.actions
        candidate = replace(current.case, actions=actions[:index] + actions[index + 1 :])
        result = await run_trial(candidate, target=current.target)
        attempts += 1
        if result.violates(goal):
            current, index = result, 0
        else:
            index += 1
    return Reduction(current, attempts, index == len(current.case.actions))


def _mutations(case: FaultCase, max_steps: int) -> Iterator[FaultCase]:
    for scenario in Scenario:
        if scenario is not case.scenario:
            yield replace(case, scenario=scenario)
    for index in range(len(case.actions)):
        for action in Action:
            if action is not case.actions[index]:
                yield replace(
                    case, actions=case.actions[:index] + (action,) + case.actions[index + 1 :]
                )
        yield replace(case, actions=case.actions[:index] + case.actions[index + 1 :])
    if len(case.actions) < max_steps:
        for index in range(len(case.actions) + 1):
            for action in Action:
                yield replace(case, actions=case.actions[:index] + (action,) + case.actions[index:])


@dataclass(frozen=True, slots=True)
class SearchAttempt:
    case: FaultCase
    new_signatures: int
    violations: tuple[Invariant, ...]

    def as_dict(self) -> dict[str, JSONValue]:
        violations: list[JSONValue] = [item.value for item in self.violations]
        return {
            "case": self.case.as_dict(),
            "new_signatures": self.new_signatures,
            "violations": violations,
        }


@dataclass(frozen=True, slots=True)
class SearchReport:
    target: Target
    goal: Invariant
    budget: SearchBudget
    stop_reason: str
    attempts: tuple[SearchAttempt, ...]
    signatures: int
    failure: TrialResult | None
    reduction: Reduction | None

    @property
    def found(self) -> bool:
        return self.failure is not None

    @property
    def observed_invariants(self) -> tuple[Invariant, ...]:
        return tuple(
            kind
            for kind in Invariant
            if any(kind in attempt.violations for attempt in self.attempts)
        )

    def as_dict(self) -> dict[str, JSONValue]:
        attempts: list[JSONValue] = [attempt.as_dict() for attempt in self.attempts]
        shrink_trials = self.reduction.trials if self.reduction is not None else 0
        observed = self.observed_invariants
        status = "failure_found" if self.found else "no_failure_found"
        if not self.found and observed:
            status = "other_failure_found"
        invariants: list[JSONValue] = [kind.value for kind in observed]
        return {
            "format_version": 1,
            "kind": "adversarial_search",
            "planner": "deterministic_behavioral_frontier",
            "target": self.target.value,
            "goal": self.goal.value,
            "budget": {
                "max_trials": self.budget.max_trials,
                "max_steps": self.budget.max_steps,
                "max_shrink_trials": self.budget.max_shrink_trials,
            },
            "status": status,
            "goal_found": self.found,
            "observed_invariants": invariants,
            "stop_reason": self.stop_reason,
            "search_trials": len(self.attempts),
            "shrink_trials": shrink_trials,
            "total_trials": len(self.attempts) + shrink_trials,
            "behavioral_signatures": self.signatures,
            "attempts": attempts,
            "failure": self.failure.as_dict() if self.failure is not None else None,
            "reduction": self.reduction.as_dict() if self.reduction is not None else None,
        }


@dataclass(frozen=True, slots=True)
class FaultSearchAgent:
    """Retain new behavioral transitions, explore mutations, and shrink counterexamples."""

    budget: SearchBudget = field(default_factory=SearchBudget)

    def __post_init__(self) -> None:
        if not isinstance(self.budget, SearchBudget):
            raise TypeError("budget must be a SearchBudget.")

    async def search(
        self,
        *,
        target: Target = Target.UNSAFE_RETRY,
        goal: Invariant = Invariant.DUPLICATE_EFFECT,
    ) -> SearchReport:
        if not isinstance(target, Target) or not isinstance(goal, Invariant):
            raise TypeError("Use explicit Target and Invariant values.")
        control = FaultCase(
            Scenario.ACKNOWLEDGED,
            (Action.OBSERVE, Action.ADVANCE, Action.OBSERVE)[: self.budget.max_steps],
        )
        frontier, seen = deque([control]), {control}
        signatures: set[str] = set()
        attempts: list[SearchAttempt] = []
        while frontier and len(attempts) < self.budget.max_trials:
            case = frontier.popleft()
            result = await run_trial(case, target=target)
            novel = set(result.coverage) - signatures
            signatures.update(result.coverage)
            attempts.append(
                SearchAttempt(case, len(novel), tuple(item.invariant for item in result.violations))
            )
            if result.violates(goal):
                reduction = await minimize_failure(
                    result, goal=goal, max_trials=self.budget.max_shrink_trials
                )
                return SearchReport(
                    target,
                    goal,
                    self.budget,
                    "failure_found",
                    tuple(attempts),
                    len(signatures),
                    result,
                    reduction,
                )
            if novel:
                for candidate in _mutations(case, self.budget.max_steps):
                    if candidate not in seen:
                        seen.add(candidate)
                        frontier.append(candidate)
        reason = "trial_budget_exhausted" if frontier else "behavioral_frontier_exhausted"
        return SearchReport(
            target, goal, self.budget, reason, tuple(attempts), len(signatures), None, None
        )
