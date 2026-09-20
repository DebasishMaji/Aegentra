"""Scripted counterexamples, not an LLM benchmark or a statistical experiment."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory

from .models import (
    BlockedByUnresolvedEffect,
    EffectScope,
    JSONValue,
    Knowledge,
    Observation,
    Operation,
    OperationKey,
    OperationRecord,
    Phase,
    encode_arguments,
)
from .recovery import RecoveryBudget
from .runtime import Runtime
from .sandbox import RefundLedger, RefundTool, Scenario


class LabPolicy(StrEnum):
    FRESH_RETRY = "fresh_retry"
    OBSERVE_OR_STOP = "observe_or_stop"
    EFFECT_BOUNDARY = "effect_boundary"


@dataclass(frozen=True, slots=True)
class TraceStep:
    step: int
    action: str
    operation_id: str | None = None
    tool_name: str | None = None
    knowledge: Knowledge | None = None

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "step": self.step,
            "action": self.action,
            "operation_id": self.operation_id,
            "tool_name": self.tool_name,
            "knowledge": self.knowledge.value if self.knowledge is not None else None,
        }


@dataclass(frozen=True, slots=True)
class LabCaseResult:
    scenario: Scenario
    policy: LabPolicy
    write_calls: int
    observation_calls: int
    replan_proposals: int
    prevented_writes: int
    boundary_blocks: int
    actual_effects: int
    invalid_effects: int
    duplicate_effects: int
    pending_at_horizon: int
    outcome: Knowledge
    false_terminal_claims: int
    valid_completion: bool
    correct_resolution: bool
    trace: tuple[TraceStep, ...]

    def as_dict(self) -> dict[str, JSONValue]:
        trace: list[JSONValue] = [step.as_dict() for step in self.trace]
        return {
            "scenario": self.scenario.value,
            "policy": self.policy.value,
            "write_calls": self.write_calls,
            "observation_calls": self.observation_calls,
            "replan_proposals": self.replan_proposals,
            "prevented_writes": self.prevented_writes,
            "boundary_blocks": self.boundary_blocks,
            "actual_effects": self.actual_effects,
            "invalid_effects": self.invalid_effects,
            "duplicate_effects": self.duplicate_effects,
            "pending_at_horizon": self.pending_at_horizon,
            "outcome": self.outcome.value,
            "false_terminal_claims": self.false_terminal_claims,
            "valid_completion": self.valid_completion,
            "correct_resolution": self.correct_resolution,
            "trace": trace,
        }


@dataclass(frozen=True, slots=True)
class LabReport:
    max_observations: int
    horizon_step: int
    results: tuple[LabCaseResult, ...]

    def as_dict(self) -> dict[str, JSONValue]:
        totals: dict[str, JSONValue] = {}
        for policy in LabPolicy:
            rows = [result for result in self.results if result.policy is policy]
            totals[policy.value] = {
                "tasks": len(rows),
                "write_calls": sum(row.write_calls for row in rows),
                "observation_calls": sum(row.observation_calls for row in rows),
                "prevented_writes": sum(row.prevented_writes for row in rows),
                "boundary_blocks": sum(row.boundary_blocks for row in rows),
                "actual_effects": sum(row.actual_effects for row in rows),
                "invalid_effects": sum(row.invalid_effects for row in rows),
                "duplicate_effects": sum(row.duplicate_effects for row in rows),
                "false_terminal_claims": sum(row.false_terminal_claims for row in rows),
                "valid_completions": sum(row.valid_completion for row in rows),
                "correct_resolutions": sum(row.correct_resolution for row in rows),
                "unresolved_outcomes": sum(row.outcome is Knowledge.UNKNOWN for row in rows),
                "pending_at_horizon": sum(row.pending_at_horizon for row in rows),
            }
        results: list[JSONValue] = [result.as_dict() for result in self.results]
        return {
            "format_version": 1,
            "kind": "scripted_failure_laboratory",
            "scenario_set": "refund-replanning/1",
            "max_observations": self.max_observations,
            "horizon_step": self.horizon_step,
            "logical_schedule": {
                "initial_dispatch": 0,
                "replan_proposal": 1,
                "first_lookup": 2,
                "late_commit": 3,
            },
            "results": results,
            "totals_by_policy": totals,
        }


class _Clock:
    """The provider driver advances independently of the read-only adapter."""

    def __init__(self, ledger: RefundLedger, *, delayed_key: OperationKey | None) -> None:
        self._ledger = ledger
        self._delayed_key = delayed_key
        self._ticks: asyncio.Queue[asyncio.Future[None] | None] = asyncio.Queue()
        self.step = 0
        self.trace: list[TraceStep] = []
        self.write_calls = 0
        self.observation_calls = 0

    def advance_to(self, step: int) -> None:
        if step < self.step:
            raise ValueError("The laboratory clock cannot move backwards.")
        if self._delayed_key is not None and step >= 3:
            key = self._delayed_key
            self._ledger.commit_pending(key)
            self.trace.append(TraceStep(3, "provider_commit", key.operation_id))
            self._delayed_key = None
        self.step = step

    async def next_lookup_step(self) -> None:
        tick = asyncio.get_running_loop().create_future()
        await self._ticks.put(tick)
        await tick

    async def drive(self) -> None:
        while True:
            tick = await self._ticks.get()
            if tick is None:
                return
            self.advance_to(self.step + 1)
            if not tick.cancelled():
                tick.set_result(None)

    async def stop(self) -> None:
        await self._ticks.put(None)


@dataclass(frozen=True, slots=True)
class _InstrumentedTool:
    delegate: RefundTool
    clock: _Clock

    @property
    def name(self) -> str:
        return self.delegate.name

    @property
    def contract_version(self) -> str:
        return self.delegate.contract_version

    def validate(self, operation: Operation) -> None:
        self.delegate.validate(operation)

    async def execute(self, record: OperationRecord) -> Observation:
        self.clock.write_calls += 1
        observation = await self.delegate.execute(record)
        self.clock.trace.append(
            TraceStep(
                self.clock.step,
                "dispatch",
                record.key.operation_id,
                self.name,
                observation.knowledge,
            )
        )
        return observation

    async def observe(self, record: OperationRecord) -> Observation:
        self.clock.observation_calls += 1
        await self.clock.next_lookup_step()
        observation = await self.delegate.observe(record)
        self.clock.trace.append(
            TraceStep(
                self.clock.step,
                "observe",
                record.key.operation_id,
                self.name,
                observation.knowledge,
            )
        )
        return observation


def _unmanaged_record(operation: Operation, tool: _InstrumentedTool) -> OperationRecord:
    """The same adapter envelope, without a durable runtime for the two baselines."""
    tool.validate(operation)
    now = datetime.now(UTC).isoformat()
    return OperationRecord(
        key=operation.key,
        scope=operation.scope,
        approval_ref=operation.approval_ref,
        tool_name=tool.name,
        contract_version=tool.contract_version,
        payload_json=encode_arguments(operation.arguments),
        knowledge=Knowledge.UNKNOWN,
        phase=Phase.RESERVED,
        reason="Unpersisted baseline request envelope.",
        receipt_json=None,
        created_at=now,
        updated_at=now,
    )


async def _run_case(
    directory: Path, scenario: Scenario, policy: LabPolicy, budget: RecoveryBudget
) -> LabCaseResult:
    stem = f"{scenario.value}-{policy.value}"
    ledger = RefundLedger(directory / f"{stem}-provider.sqlite3")
    operation = Operation(
        "refund-42",
        EffectScope("tenant-a", "payments", "order-42", "refund"),
        "approval-42",
        {"amount_minor": 5000, "currency": "USD"},
    )
    clock = _Clock(
        ledger, delayed_key=operation.key if scenario is Scenario.DELAYED_COMMIT else None
    )
    original = _InstrumentedTool(RefundTool(ledger, scenario), clock)
    alternate = _InstrumentedTool(RefundTool(ledger, name="refund_order"), clock)
    proposals = prevented = boundary_blocks = 0

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(clock.drive())
        with ExitStack() as resources:
            runtime = (
                resources.enter_context(Runtime(directory / f"{stem}-journal.sqlite3"))
                if policy is LabPolicy.EFFECT_BOUNDARY
                else None
            )
            baseline_record = _unmanaged_record(operation, original)
            if runtime is not None:
                first = await runtime.perform(operation, tool=original)
                if isinstance(first, BlockedByUnresolvedEffect):
                    raise RuntimeError("The isolated first laboratory action was blocked.")
                knowledge = first.record.knowledge
            else:
                knowledge = (await original.execute(baseline_record)).knowledge

            clock.advance_to(1)
            if knowledge is Knowledge.UNKNOWN:
                proposal = replace(
                    operation, operation_id="replanned-refund", approval_ref="replanned-approval"
                )
                proposals += 1
                clock.trace.append(TraceStep(1, "proposal", proposal.operation_id, alternate.name))
                if policy is LabPolicy.FRESH_RETRY:
                    knowledge = (
                        await alternate.execute(_unmanaged_record(proposal, alternate))
                    ).knowledge
                elif runtime is not None:
                    proposed = await runtime.perform(proposal, tool=alternate)
                    if isinstance(proposed, BlockedByUnresolvedEffect):
                        prevented += 1
                        boundary_blocks += 1
                        clock.trace.append(
                            TraceStep(1, "boundary_blocked", proposal.operation_id, alternate.name)
                        )
                    else:
                        knowledge = proposed.record.knowledge
                else:
                    prevented += 1
                    clock.trace.append(
                        TraceStep(1, "policy_declined", proposal.operation_id, alternate.name)
                    )

                if knowledge is Knowledge.UNKNOWN:
                    if runtime is not None:
                        recovered = await runtime.recover(
                            operation.key, tool=original, budget=budget
                        )
                        knowledge = recovered.record.knowledge
                    else:
                        for _ in range(budget.max_observations):
                            knowledge = (await original.observe(baseline_record)).knowledge
                            if knowledge is not Knowledge.UNKNOWN:
                                break

            clock.trace.append(TraceStep(clock.step, "terminal_claim", knowledge=knowledge))
            horizon = max(4, budget.max_observations + 2)
            clock.advance_to(horizon)
            clock.trace.append(TraceStep(horizon, "horizon"))
            await clock.stop()

    # Ground truth is read only after all policy decisions and late provider work.
    refunds = ledger.refunds()
    effects = len(refunds)
    matching = sum(
        refund.key.tenant == operation.scope.tenant
        and refund.key.account == operation.scope.account
        and refund.resource == operation.scope.resource
        and refund.amount_minor == operation.arguments["amount_minor"]
        and refund.currency == operation.arguments["currency"]
        for refund in refunds
    )
    pending = ledger.pending_count()
    completed = knowledge is Knowledge.APPLIED and effects == matching == 1 and pending == 0
    excluded = knowledge is Knowledge.NOT_APPLIED_FINAL and effects == 0 and pending == 0
    false_claims = int(knowledge is not Knowledge.UNKNOWN and not (completed or excluded))
    return LabCaseResult(
        scenario=scenario,
        policy=policy,
        write_calls=clock.write_calls,
        observation_calls=clock.observation_calls,
        replan_proposals=proposals,
        prevented_writes=prevented,
        boundary_blocks=boundary_blocks,
        actual_effects=effects,
        invalid_effects=effects - matching,
        duplicate_effects=max(0, effects - 1),
        pending_at_horizon=pending,
        outcome=knowledge,
        false_terminal_claims=false_claims,
        valid_completion=completed,
        correct_resolution=completed or excluded,
        trace=tuple(clock.trace),
    )


async def run_lab(*, max_observations: int = 2) -> LabReport:
    """Run the frozen scenario set with equal lookup allowances and evaluation horizons."""
    budget = RecoveryBudget(max_observations=max_observations)
    results: list[LabCaseResult] = []
    with TemporaryDirectory(prefix="aegentra-lab-") as directory:
        for scenario in Scenario:
            for policy in LabPolicy:
                results.append(await _run_case(Path(directory), scenario, policy, budget))
    return LabReport(max_observations, max(4, max_observations + 2), tuple(results))
