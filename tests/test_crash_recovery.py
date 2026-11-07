import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aegentra import (
    ConfirmedApplied,
    EffectScope,
    Inspection,
    Knowledge,
    Operation,
    Runtime,
    RuntimeUsageError,
    Unresolved,
)
from aegentra.sandbox import RefundLedger, RefundTool

_CRASHING_PROCESS = """
import asyncio
import os
import sys
from pathlib import Path
from aegentra import EffectScope, Operation, Runtime
from aegentra.sandbox import RefundLedger, RefundTool

directory = Path(sys.argv[1])
crash_before_send = sys.argv[2] == "before_send"
ledger = RefundLedger(directory / "provider.sqlite3")

class InterruptedTool(RefundTool):
    async def execute(self, record):
        if not crash_before_send:
            await super().execute(record)
        os._exit(73)

async def main():
    operation = Operation(
        "refund-42", EffectScope("tenant-a", "payments", "order-42", "refund"),
        "approval-42", {"amount_minor": 5000, "currency": "USD"},
    )
    with Runtime(directory / "agent.sqlite3") as runtime:
        await runtime.perform(operation, tool=InterruptedTool(ledger))

asyncio.run(main())
"""


@pytest.mark.parametrize("boundary", ["before_send", "after_commit"])
def test_process_death_never_turns_into_fresh_dispatch(tmp_path: Path, boundary: str) -> None:
    process = subprocess.run(
        [sys.executable, "-c", _CRASHING_PROCESS, str(tmp_path), boundary],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert process.returncode == 73, process.stderr

    ledger = RefundLedger(tmp_path / "provider.sqlite3")
    operation = Operation(
        "refund-42",
        EffectScope("tenant-a", "payments", "order-42", "refund"),
        "approval-42",
        {"amount_minor": 5000, "currency": "USD"},
    )
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        assert runtime.inspect(operation.key).knowledge is Knowledge.UNKNOWN
        result = asyncio.run(runtime.perform(operation, tool=RefundTool(ledger)))
        if boundary == "after_commit":
            assert isinstance(result, ConfirmedApplied)
            assert len(ledger.refunds()) == 1
        else:
            assert isinstance(result, Unresolved)
            assert ledger.refunds() == ()


def test_second_process_cannot_take_over_a_live_journal(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite3"
    program = """
import sys
from aegentra import JournalInUse, Runtime
try:
    Runtime(sys.argv[1])
except JournalInUse:
    sys.exit(17)
sys.exit(1)
"""
    with Runtime(path):
        process = subprocess.run(
            [sys.executable, "-c", program, str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    assert process.returncode == 17, process.stderr


def test_hard_link_rejection_preserves_the_pending_wal_obligation(tmp_path: Path) -> None:
    source, alias = tmp_path / "agent.sqlite3", tmp_path / "alias.sqlite3"
    with Runtime(source):
        pass
    process = subprocess.run(
        [sys.executable, "-c", _CRASHING_PROCESS, str(tmp_path), "after_commit"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert process.returncode == 73, process.stderr
    wal = Path(f"{source}-wal")
    before = wal.read_bytes()
    assert before
    os.link(source, alias)
    with pytest.raises(RuntimeUsageError, match="hard-link"):
        Inspection(alias)
    with pytest.raises(RuntimeUsageError, match="hard-link"):
        Runtime(source, require_existing=True)
    assert wal.read_bytes() == before
    alias.unlink()

    operation = Operation(
        "refund-42",
        EffectScope("tenant-a", "payments", "order-42", "refund"),
        "approval-42",
        {"amount_minor": 5000, "currency": "USD"},
    )
    ledger = RefundLedger(tmp_path / "provider.sqlite3")
    with Runtime(source, require_existing=True) as recovered:
        assert recovered.inspect(operation.key).knowledge is Knowledge.UNKNOWN
        result = asyncio.run(recovered.perform(operation, tool=RefundTool(ledger)))
        assert isinstance(result, ConfirmedApplied)
    assert len(ledger.refunds()) == 1
