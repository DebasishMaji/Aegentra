import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from aegentra import Operation, Runtime
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
