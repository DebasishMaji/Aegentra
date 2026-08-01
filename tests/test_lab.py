import asyncio

import pytest

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
