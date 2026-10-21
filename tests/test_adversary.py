import asyncio
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from aegentra import Knowledge, adversary
from aegentra.adversary import (
    HARNESS,
    Action,
    FaultCase,
    FaultSearchAgent,
    Invariant,
    SearchBudget,
    SearchReport,
    Target,
    Violation,
    load_case,
    minimize_failure,
    run_trial,
    save_case,
)
from aegentra.adversary import (
    TestCaseError as CaseError,
)
from aegentra.sandbox import Refund, RefundLedger, RefundTool, Scenario


@pytest.fixture(scope="module")
def discovery() -> SearchReport:
    return asyncio.run(FaultSearchAgent().search())


@pytest.mark.parametrize(
    "scenario", [Scenario.LOST_ACKNOWLEDGMENT, Scenario.DELAYED_COMMIT, Scenario.UNOBSERVABLE]
)
async def test_replanning_exposes_duplicates_and_the_boundary_blocks_them(
    scenario: Scenario,
) -> None:
    case = FaultCase(scenario, (Action.REPLAN,))
    unsafe = await run_trial(case, target=Target.UNSAFE_RETRY)
    guarded = await run_trial(case, target=Target.AEGENTRA)
    assert unsafe.violates(Invariant.DUPLICATE_EFFECT)
    assert unsafe.write_calls == unsafe.evaluation.actual_effects == 2
    assert unsafe.evaluation.pending_requests == 0
    assert guarded.write_calls == guarded.evaluation.actual_effects == 1
    assert guarded.blocked_proposals == 1
    assert guarded.violations == ()
    assert guarded.trace[1].action == "replan_blocked"


@pytest.mark.parametrize("target", list(Target))
@pytest.mark.parametrize("scenario", [Scenario.ACKNOWLEDGED, Scenario.REJECTED])
async def test_settled_workflows_do_not_blindly_replan(target: Target, scenario: Scenario) -> None:
    result = await run_trial(FaultCase(scenario, (Action.REPLAN,)), target=target)
    assert result.write_calls == 1
    assert result.trace[1].action == "replan_skipped"
    assert result.violations == ()
    assert result.evaluation.correct_resolution
    assert result.evaluation.valid_completion is (scenario is Scenario.ACKNOWLEDGED)


async def test_horizon_advances_late_effects_after_a_client_claims_success() -> None:
    result = await run_trial(
        FaultCase(Scenario.DELAYED_COMMIT, (Action.REPLAN, Action.OBSERVE)),
        target=Target.UNSAFE_RETRY,
    )
    assert result.trace[1].actual_effects == result.trace[1].pending_requests == 1
    assert Violation(Invariant.FALSE_TERMINAL_CLAIM, 1) in result.violations
    assert Violation(Invariant.DUPLICATE_EFFECT, 3) in result.violations
    assert result.trace[-1].action == "horizon"
    assert result.trace[-1].actual_effects == 2
    assert result.trace[-1].pending_requests == 0


@pytest.mark.parametrize("target", list(Target))
async def test_observing_a_late_commit_can_make_replanning_unnecessary(target: Target) -> None:
    result = await run_trial(
        FaultCase(
            Scenario.DELAYED_COMMIT,
            (Action.ADVANCE, Action.OBSERVE, Action.REPLAN),
        ),
        target=target,
    )
    assert result.violations == ()
    assert result.write_calls == result.observation_calls == 1
    assert result.trace[-2].action == "replan_skipped"
    assert result.evaluation.valid_completion


async def test_restart_distinguishes_memory_only_state_from_the_journal() -> None:
    case = FaultCase(Scenario.ACKNOWLEDGED, (Action.RESTART, Action.REPLAN))
    unsafe = await run_trial(case, target=Target.UNSAFE_RETRY)
    guarded = await run_trial(case, target=Target.AEGENTRA)
    assert unsafe.trace[1].knowledge is Knowledge.UNKNOWN
    assert unsafe.violates(Invariant.DUPLICATE_EFFECT)
    assert guarded.trace[1].knowledge is Knowledge.APPLIED
    assert guarded.trace[2].action == "replan_skipped"
    assert guarded.violations == ()
    assert guarded.write_calls == 1


async def test_unknown_is_neither_a_violation_nor_a_completed_refund() -> None:
    result = await run_trial(FaultCase(Scenario.UNOBSERVABLE), target=Target.AEGENTRA)
    assert result.violations == ()
    assert result.trace[-1].knowledge is Knowledge.UNKNOWN
    assert result.evaluation.actual_effects == 1
    assert not result.evaluation.valid_completion
    assert not result.evaluation.correct_resolution


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant", "another-tenant"),
        ("account", "another-account"),
        ("resource", "another-order"),
        ("amount_minor", 5001),
        ("currency", "EUR"),
    ],
)
async def test_the_oracle_checks_intent_not_just_acknowledgments(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str | int,
) -> None:
    original = RefundLedger.refunds

    def corrupt_intent(ledger: RefundLedger) -> tuple[Refund, ...]:
        return tuple(
            replace(refund, key=replace(refund.key, **{field: value}))
            if field in ("tenant", "account")
            else replace(refund, **{field: value})
            for refund in original(ledger)
        )

    monkeypatch.setattr(RefundLedger, "refunds", corrupt_intent)
    result = await run_trial(FaultCase(Scenario.ACKNOWLEDGED), target=Target.UNSAFE_RETRY)
    assert result.trace[-1].knowledge is Knowledge.APPLIED
    assert result.evaluation.actual_effects == result.evaluation.invalid_effects == 1
    assert result.violates(Invariant.INVALID_EFFECT)
    assert result.violates(Invariant.FALSE_TERMINAL_CLAIM)
    assert not result.violates(Invariant.DUPLICATE_EFFECT)


async def test_adapter_errors_propagate_instead_of_becoming_clean_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_observe(self: RefundTool, record) -> None:
        raise ConnectionError("Injected observation failure")

    monkeypatch.setattr(RefundTool, "observe", broken_observe)
    with pytest.raises(ConnectionError, match="Injected observation"):
        await run_trial(
            FaultCase(Scenario.UNOBSERVABLE, (Action.OBSERVE,)),
            target=Target.AEGENTRA,
        )


def test_agent_discovers_a_failure_without_starting_from_one(discovery: SearchReport) -> None:
    assert discovery.found
    assert discovery.failure is not None and discovery.reduction is not None
    assert 1 < len(discovery.attempts) <= 64
    assert discovery.attempts[0].case.scenario is Scenario.ACKNOWLEDGED
    assert Action.REPLAN not in discovery.attempts[0].case.actions
    assert discovery.attempts[0].violations == ()
    assert all(len(attempt.case.actions) <= 8 for attempt in discovery.attempts)
    assert len({attempt.case for attempt in discovery.attempts}) == len(discovery.attempts)
    assert discovery.failure.violates(Invariant.DUPLICATE_EFFECT)
    assert discovery.reduction.one_minimal
    assert len(discovery.reduction.result.case.actions) < len(discovery.failure.case.actions)
    report = discovery.as_dict()
    assert report["total_trials"] == len(discovery.attempts) + discovery.reduction.trials
    assert report["planner"] == "deterministic_behavioral_frontier"


async def test_minimized_counterexample_replays_and_each_deletion_loses_the_goal(
    discovery: SearchReport,
) -> None:
    assert discovery.reduction is not None
    minimal = discovery.reduction.result
    replayed = await run_trial(minimal.case, target=minimal.target)
    assert replayed.as_dict() == minimal.as_dict()
    for index in range(len(minimal.case.actions)):
        candidate = replace(
            minimal.case,
            actions=minimal.case.actions[:index] + minimal.case.actions[index + 1 :],
        )
        assert not (await run_trial(candidate, target=minimal.target)).violates(discovery.goal)


async def test_shrinking_preserves_the_requested_failure_not_just_any_failure(
    monkeypatch: pytest.MonkeyPatch, discovery: SearchReport
) -> None:
    assert discovery.failure is not None
    failure = discovery.failure

    async def different_failure(case: FaultCase, *, target: Target):
        return replace(
            failure,
            case=case,
            target=target,
            violations=(Violation(Invariant.INVALID_EFFECT, 0),),
        )

    monkeypatch.setattr(adversary, "run_trial", different_failure)
    reduction = await minimize_failure(failure, goal=Invariant.DUPLICATE_EFFECT)
    assert reduction.result == failure
    assert reduction.trials == len(failure.case.actions)
    assert reduction.one_minimal


@pytest.mark.parametrize("allowance", [0, 1])
async def test_shrink_budget_exhaustion_does_not_claim_minimality(allowance: int) -> None:
    failure = await run_trial(
        FaultCase(Scenario.LOST_ACKNOWLEDGMENT, (Action.REPLAN, Action.ADVANCE, Action.OBSERVE)),
        target=Target.UNSAFE_RETRY,
    )
    reduction = await minimize_failure(
        failure, goal=Invariant.DUPLICATE_EFFECT, max_trials=allowance
    )
    assert reduction.trials == allowance
    assert not reduction.one_minimal
    assert reduction.result.violates(Invariant.DUPLICATE_EFFECT)


async def test_passing_trials_cannot_be_presented_as_failures_to_shrink() -> None:
    passing = await run_trial(FaultCase(Scenario.ACKNOWLEDGED), target=Target.UNSAFE_RETRY)
    with pytest.raises(ValueError, match="does not violate"):
        await minimize_failure(passing, goal=Invariant.DUPLICATE_EFFECT)


async def test_search_budget_exhaustion_is_not_reported_as_a_proof_of_safety() -> None:
    report = await FaultSearchAgent(SearchBudget(max_trials=1)).search()
    assert not report.found
    assert report.stop_reason == "trial_budget_exhausted"
    assert len(report.attempts) == 1
    assert report.failure is report.reduction is None
    assert report.as_dict()["status"] == "no_failure_found"


async def test_missing_the_requested_goal_does_not_hide_other_violations() -> None:
    report = await FaultSearchAgent().search(goal=Invariant.INVALID_EFFECT)
    assert not report.found
    assert Invariant.DUPLICATE_EFFECT in report.observed_invariants
    assert report.as_dict()["status"] == "other_failure_found"
    assert not report.as_dict()["goal_found"]


async def test_zero_depth_only_explores_initial_dispatches() -> None:
    report = await FaultSearchAgent(SearchBudget(max_trials=20, max_steps=0)).search()
    assert not report.found
    assert report.stop_reason == "behavioral_frontier_exhausted"
    assert len(report.attempts) == len(Scenario)
    assert all(attempt.case.actions == () for attempt in report.attempts)


async def test_no_novel_behavior_does_not_expand_the_frontier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = adversary._mutations
    expanded = set()

    def track(case: FaultCase, max_steps: int):
        expanded.add(case)
        yield from original(case, max_steps)

    monkeypatch.setattr(adversary, "_mutations", track)
    report = await FaultSearchAgent().search(target=Target.AEGENTRA)
    assert not report.found
    assert expanded == {item.case for item in report.attempts if item.new_signatures}
    assert any(item.new_signatures == 0 for item in report.attempts)


async def test_search_and_reports_are_deterministic(discovery: SearchReport) -> None:
    assert (await FaultSearchAgent().search()).as_dict() == discovery.as_dict()


def test_case_artifacts_round_trip_and_refuse_overwrite(tmp_path: Path) -> None:
    case = FaultCase(Scenario.LOST_ACKNOWLEDGMENT, (Action.REPLAN,))
    path = tmp_path / "case.json"
    save_case(case, path)
    assert load_case(path) == case
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        save_case(FaultCase(Scenario.REJECTED), path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"\xff",
        b'{"format_version":1,"format_version":2}',
        b" " * 16_385,
        b"[" * 2000 + b"]" * 2000,
        b'{"format_version":' + b"1" * 5000 + b"}",
        b"null",
    ],
)
def test_malformed_artifacts_raise_explicit_case_errors(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "case.json"
    path.write_bytes(payload)
    with pytest.raises(CaseError):
        load_case(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", True),
        ("format_version", 2),
        ("harness", "another-model/1"),
        ("scenario", "invented"),
        ("scenario", 1),
        ("actions", ["execute_python"]),
        ("actions", [False]),
        ("actions", "replan"),
        ("actions", ["replan"] * 33),
        ("extra", "ignored"),
    ],
)
def test_case_schema_is_closed_and_strict(field: str, value: object) -> None:
    payload = FaultCase(Scenario.ACKNOWLEDGED).as_dict()
    payload[field] = value
    with pytest.raises(CaseError):
        FaultCase.from_dict(payload)


def test_artifacts_do_not_accept_arbitrary_programs() -> None:
    with pytest.raises(CaseError):
        FaultCase.from_dict({"harness": HARNESS, "script": "print('not executed')"})
    with pytest.raises(CaseError):
        FaultCase(Scenario.ACKNOWLEDGED, ["replan"])
    with pytest.raises(CaseError):
        FaultCase("acknowledged")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_trials": 0},
        {"max_trials": True},
        {"max_trials": 1.5},
        {"max_steps": -1},
        {"max_steps": 33},
        {"max_steps": True},
        {"max_shrink_trials": -1},
        {"max_shrink_trials": float("nan")},
    ],
)
def test_invalid_search_budgets_fail_before_execution(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        SearchBudget(**kwargs)


async def test_public_entrypoints_require_explicit_types() -> None:
    with pytest.raises(TypeError, match="SearchBudget"):
        FaultSearchAgent(None)
    with pytest.raises(TypeError, match="Target"):
        await FaultSearchAgent().search(target="unsafe_retry")
    with pytest.raises(TypeError, match="Target"):
        await run_trial(FaultCase(Scenario.ACKNOWLEDGED), target="unsafe_retry")
