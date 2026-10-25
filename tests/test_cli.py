import json
import subprocess
import sys
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

import pytest

from aegentra import Operation, Runtime
from aegentra.adversary import Action, load_case
from aegentra.sandbox import RefundLedger, RefundTool, Scenario

KEY_ARGS = ["--tenant", "tenant-a", "--account", "payments", "--operation-id", "refund-42"]


@pytest.fixture
async def journal(tmp_path: Path, operation: Operation, ledger: RefundLedger) -> Path:
    path = tmp_path / "agent.sqlite3"
    with Runtime(path) as runtime:
        await runtime.perform(operation, tool=RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT))
        await runtime.perform(replace(operation, operation_id="replanned"), tool=RefundTool(ledger))
        settled = replace(
            operation,
            operation_id="refund-43",
            scope=replace(operation.scope, resource="order-43"),
        )
        await runtime.perform(settled, tool=RefundTool(ledger))
    return path


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aegentra", *arguments],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_list_defaults_to_unresolved_and_runs_beside_a_writer(journal: Path) -> None:
    with Runtime(journal):
        result = run_cli("list", str(journal))
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["format_version"] == 1
    assert len(report["operations"]) == 1
    assert report["operations"][0]["key"]["operation_id"] == "refund-42"
    assert "arguments" not in report["operations"][0]
    assert "receipt" not in report["operations"][0]
    assert report["next_cursor"] is None


def test_list_cursor_round_trips_and_can_include_settled_operations(journal: Path) -> None:
    first = run_cli("list", str(journal), "--knowledge", "all", "--limit", "1")
    assert first.returncode == 0, first.stderr
    cursor = json.loads(first.stdout)["next_cursor"]
    second = run_cli(
        "list",
        str(journal),
        "--knowledge",
        "all",
        "--limit",
        "1",
        "--after",
        cursor["tenant"],
        cursor["account"],
        cursor["operation_id"],
    )
    assert second.returncode == 0, second.stderr
    page = json.loads(second.stdout)
    assert page["operations"][0]["key"]["operation_id"] == "refund-43"
    assert page["next_cursor"] is None


def test_inspect_payload_is_explicit(journal: Path) -> None:
    result = run_cli("inspect", str(journal), *KEY_ARGS)
    assert result.returncode == 0, result.stderr
    assert "arguments" not in json.loads(result.stdout)["operation"]
    result = run_cli("inspect", str(journal), *KEY_ARGS, "--include-payload")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["operation"]["arguments"]["amount_minor"] == 5000
    assert json.loads(result.stdout)["operation"]["receipt"] is None


def test_trace_contains_blocked_proposals_and_an_explicit_continuation(journal: Path) -> None:
    first = run_cli("trace", str(journal), *KEY_ARGS, "--limit", "2")
    assert first.returncode == 0, first.stderr
    page = json.loads(first.stdout)
    assert len(page["events"]) == 2
    second = run_cli(
        "trace",
        str(journal),
        *KEY_ARGS,
        "--after-sequence",
        str(page["next_sequence"]),
    )
    assert second.returncode == 0, second.stderr
    page = json.loads(second.stdout)
    assert page["events"][0]["blocked_action"]["requested_key"]["operation_id"] == "replanned"
    assert page["next_sequence"] is None


def test_missing_journal_is_an_error_without_creating_it(tmp_path: Path) -> None:
    path = tmp_path / "absent.sqlite3"
    result = run_cli("list", str(path))
    assert result.returncode == 1
    assert result.stdout == ""
    assert "aegentra: error:" in result.stderr
    assert not path.exists()
    assert not Path(f"{path}.lock").exists()


def test_missing_namespaced_operation_is_an_error(journal: Path) -> None:
    result = run_cli(
        "inspect",
        str(journal),
        "--tenant",
        "other",
        "--account",
        "payments",
        "--operation-id",
        "refund-42",
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "No operation" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["list", "--limit", "0"],
        ["list", "--limit", "1001"],
        ["list", "--limit", "true"],
        ["list", "--tenant", ""],
        ["trace", *KEY_ARGS, "--after-sequence", "-1"],
        ["recover"],
        ["lab", "--max-observations", "0"],
    ],
)
def test_argument_errors_are_not_successes(journal: Path, arguments: list[str]) -> None:
    positional = [] if arguments[0] == "lab" else [str(journal)]
    result = run_cli(arguments[0], *positional, *arguments[1:])
    assert result.returncode == 2
    assert result.stdout == ""
    assert "error:" in result.stderr


def test_module_and_installed_entrypoint_agree_on_version() -> None:
    module = run_cli("--version")
    installed = subprocess.run(
        ["aegentra", "--version"], capture_output=True, text=True, timeout=10, check=False
    )
    assert module.returncode == installed.returncode == 0
    assert module.stdout == installed.stdout == f"aegentra {version('aegentra')}\n"


def test_laboratory_command_emits_a_complete_comparison() -> None:
    result = run_cli("lab", "--max-observations", "1")
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["format_version"] == 1
    assert report["kind"] == "scripted_failure_laboratory"
    assert report["max_observations"] == 1
    assert len(report["results"]) == 15
    assert report["totals_by_policy"]["effect_boundary"]["false_terminal_claims"] == 0


def test_retained_example_can_be_inspected_and_refuses_to_overwrite(tmp_path: Path) -> None:
    directory = tmp_path / "refund-demo"
    script = Path(__file__).resolve().parents[1] / "examples" / "refund_recovery.py"
    command = [sys.executable, str(script), "--directory", str(directory)]
    example = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    assert example.returncode == 0, example.stderr
    assert "Actual refunds: 1" in example.stdout
    result = run_cli(
        "trace",
        str(directory / "agent.sqlite3"),
        "--tenant",
        "shop",
        "--account",
        "payments",
        "--operation-id",
        "approved-refund-42",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["operation"]["knowledge"] == "applied"
    assert any(event["kind"] == "effect_blocked" for event in report["events"])
    before = (directory / "agent.sqlite3").read_bytes()
    repeated = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    assert repeated.returncode == 2
    assert "Cannot create demo directory" in repeated.stderr
    assert (directory / "agent.sqlite3").read_bytes() == before


def test_agent_discovers_saves_and_replays_a_counterexample(tmp_path: Path) -> None:
    path = tmp_path / "counterexample.json"
    result = run_cli("test-agent", "--case-out", str(path))
    assert result.returncode == 3, result.stderr
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["kind"] == "adversarial_search"
    assert report["goal_found"]
    assert report["status"] == "failure_found"
    assert report["case_file"] == str(path)
    assert report["budget"]["max_trials"] == 64
    assert report["reduction"]["one_minimal"]
    assert load_case(path).as_dict() == report["reduction"]["result"]["case"]

    unsafe = run_cli("replay-test", str(path), "--target", "unsafe_retry")
    assert unsafe.returncode == 3, unsafe.stderr
    assert unsafe.stderr == ""
    assert json.loads(unsafe.stdout)["actual_effects"] == 2

    guarded = run_cli("replay-test", str(path), "--target", "aegentra")
    assert guarded.returncode == 0, guarded.stderr
    assert guarded.stderr == ""
    replay = json.loads(guarded.stdout)
    assert replay["case"] == report["reduction"]["result"]["case"]
    assert replay["actual_effects"] == 1
    assert replay["blocked_proposals"] == 1
    assert replay["violations"] == []
    assert not replay["valid_completion"]


def test_exhausted_search_does_not_create_an_artifact(tmp_path: Path) -> None:
    path = tmp_path / "not-found.json"
    result = run_cli("test-agent", "--max-trials", "1", "--case-out", str(path))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "no_failure_found"
    assert report["stop_reason"] == "trial_budget_exhausted"
    assert report["case_file"] is None
    assert not path.exists()


def test_non_goal_findings_still_return_the_violation_exit_code(tmp_path: Path) -> None:
    path = tmp_path / "no-goal.json"
    result = run_cli("test-agent", "--goal", "invalid_effect", "--case-out", str(path))
    assert result.returncode == 3, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "other_failure_found"
    assert "duplicate_effect" in report["observed_invariants"]
    assert not report["goal_found"]
    assert report["case_file"] is None
    assert not path.exists()


def test_case_output_cannot_overwrite_an_existing_artifact(tmp_path: Path) -> None:
    path = tmp_path / "existing.json"
    path.write_text("preserve this")
    result = run_cli("test-agent", "--case-out", str(path))
    assert result.returncode == 1
    assert result.stdout == ""
    assert "aegentra: error:" in result.stderr
    assert path.read_text() == "preserve this"


def test_zero_shrink_allowance_exports_a_case_without_claiming_minimality(tmp_path: Path) -> None:
    path = tmp_path / "unreduced.json"
    result = run_cli("test-agent", "--max-shrink-trials", "0", "--case-out", str(path))
    assert result.returncode == 3, result.stderr
    report = json.loads(result.stdout)
    assert report["shrink_trials"] == 0
    assert not report["reduction"]["one_minimal"]
    assert load_case(path).as_dict() == report["failure"]["case"]


@pytest.mark.parametrize("payload", ['{"format_version":true}', '{"harness":', "null"])
def test_replay_reports_malformed_cases_on_stderr(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(payload)
    result = run_cli("replay-test", str(path), "--target", "aegentra")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "aegentra: error:" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["test-agent", "--max-trials", "0"],
        ["test-agent", "--max-steps", "-1"],
        ["test-agent", "--max-steps", "33"],
        ["test-agent", "--max-shrink-trials", "-1"],
        ["test-agent", "--goal", "all_clear"],
        ["test-agent", "--target", "production"],
        ["test-agent", "--case-out", ""],
        ["replay-test", "case.json"],
        ["replay-test", "case.json", "--target", "production"],
    ],
)
def test_testing_commands_reject_bad_arguments(arguments: list[str]) -> None:
    result = run_cli(*arguments)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "error:" in result.stderr


def test_adversarial_demo_separates_matched_replay_from_recovery(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "examples" / "adversarial_testing.py"
    path = tmp_path / "discovered.json"
    result = subprocess.run(
        [sys.executable, str(script), "--case-out", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "single-deletion minimal: True" in result.stdout
    assert "Aegentra: exactly the same case" in result.stdout
    assert "matched replay is still unresolved" in result.stdout
    assert "extended recovery trace (different input)" in result.stdout
    assert "Valid completion: False" in result.stdout
    assert "Valid completion: True" in result.stdout
    assert load_case(path).actions == (Action.REPLAN,)
