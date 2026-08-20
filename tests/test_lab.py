import asyncio

import pytest

from aegentra import Knowledge
from aegentra.lab import LabCaseResult, LabPolicy, LabReport, run_lab
from aegentra.sandbox import Scenario


@pytest.fixture(scope="module")
def report() -> LabReport:
    return asyncio.run(run_lab())


def case(report: LabReport, scenario: Scenario, policy: LabPolicy) -> LabCaseResult:
    return next(row for row in report.results if row.scenario is scenario and row.policy is policy)


def test_all_frozen_cases_and_policies_are_present(report: LabReport) -> None:
    assert len(report.results) == 15
    assert {(row.scenario, row.policy) for row in report.results} == {
        (scenario, policy) for scenario in Scenario for policy in LabPolicy
    }
    assert report.horizon_step == 4
    assert all(row.trace[-1].step == report.horizon_step for row in report.results)
    assert all(row.invalid_effects == 0 for row in report.results)


@pytest.mark.parametrize("policy", list(LabPolicy))
def test_acknowledgment_and_final_rejection_are_not_conflated(
    report: LabReport, policy: LabPolicy
) -> None:
    applied = case(report, Scenario.ACKNOWLEDGED, policy)
    assert applied.outcome is Knowledge.APPLIED
    assert applied.write_calls == 1
    assert applied.observation_calls == applied.replan_proposals == 0
    assert applied.actual_effects == 1
    assert applied.correct_resolution and applied.valid_completion
    assert applied.false_terminal_claims == 0

    rejected = case(report, Scenario.REJECTED, policy)
    assert rejected.outcome is Knowledge.NOT_APPLIED_FINAL
    assert rejected.actual_effects == rejected.observation_calls == 0
    assert rejected.correct_resolution and not rejected.valid_completion
    assert rejected.false_terminal_claims == 0


@pytest.mark.parametrize(
    "scenario", [Scenario.LOST_ACKNOWLEDGMENT, Scenario.DELAYED_COMMIT, Scenario.UNOBSERVABLE]
)
def test_fresh_retry_exposes_duplicates_even_after_client_success(
    report: LabReport, scenario: Scenario
) -> None:
    row = case(report, scenario, LabPolicy.FRESH_RETRY)
    assert row.write_calls == row.actual_effects == 2
    assert row.replan_proposals == row.duplicate_effects == row.false_terminal_claims == 1
    assert row.observation_calls == row.prevented_writes == 0
    assert row.outcome is Knowledge.APPLIED
    assert not row.valid_completion


@pytest.mark.parametrize("policy", [LabPolicy.OBSERVE_OR_STOP, LabPolicy.EFFECT_BOUNDARY])
@pytest.mark.parametrize(
    ("scenario", "observations", "outcome"),
    [
        (Scenario.LOST_ACKNOWLEDGMENT, 1, Knowledge.APPLIED),
        (Scenario.DELAYED_COMMIT, 2, Knowledge.APPLIED),
        (Scenario.UNOBSERVABLE, 2, Knowledge.UNKNOWN),
    ],
)
def test_careful_policies_share_information_budgets_and_results(
    report: LabReport, policy: LabPolicy, scenario: Scenario, observations: int, outcome: Knowledge
) -> None:
    row = case(report, scenario, policy)
    assert row.write_calls == row.actual_effects == 1
    assert row.observation_calls == observations
    assert row.replan_proposals == row.prevented_writes == 1
    assert row.boundary_blocks == int(policy is LabPolicy.EFFECT_BOUNDARY)
    assert row.duplicate_effects == row.false_terminal_claims == 0
    assert row.outcome is outcome
    assert row.valid_completion is (outcome is Knowledge.APPLIED)


async def test_one_lookup_does_not_stop_the_provider_from_committing_later() -> None:
    report = await run_lab(max_observations=1)
    for policy in (LabPolicy.OBSERVE_OR_STOP, LabPolicy.EFFECT_BOUNDARY):
        row = case(report, Scenario.DELAYED_COMMIT, policy)
        terminal = next(step for step in row.trace if step.action == "terminal_claim")
        commit = next(step for step in row.trace if step.action == "provider_commit")
        assert terminal.step == 2 < commit.step == 3
        assert row.outcome is Knowledge.UNKNOWN
        assert row.actual_effects == 1
        assert row.pending_at_horizon == row.false_terminal_claims == 0
        assert not row.correct_resolution and not row.valid_completion
    assert all(row.observation_calls <= 1 for row in report.results)


def test_delayed_duplicate_is_counted_after_unsafe_policy_stops(report: LabReport) -> None:
    row = case(report, Scenario.DELAYED_COMMIT, LabPolicy.FRESH_RETRY)
    terminal = next(step for step in row.trace if step.action == "terminal_claim")
    commit = next(step for step in row.trace if step.action == "provider_commit")
    assert terminal.step == 1 < commit.step == 3
    assert row.actual_effects == 2


def test_totals_use_all_assigned_cases_and_keep_rejections_separate(report: LabReport) -> None:
    totals = report.as_dict()["totals_by_policy"]
    for policy in (LabPolicy.OBSERVE_OR_STOP, LabPolicy.EFFECT_BOUNDARY):
        row = totals[policy.value]
        assert row["tasks"] == 5
        assert row["write_calls"] == row["observation_calls"] == 5
        assert row["valid_completions"] == 3
        assert row["correct_resolutions"] == 4
        assert row["unresolved_outcomes"] == 1
        assert row["duplicate_effects"] == row["false_terminal_claims"] == 0
    unsafe = totals["fresh_retry"]
    assert unsafe["duplicate_effects"] == unsafe["false_terminal_claims"] == 3
    assert unsafe["valid_completions"] == 1
    assert unsafe["correct_resolutions"] == 2
