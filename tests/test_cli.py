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
