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
