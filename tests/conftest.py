import sqlite3
from pathlib import Path

import pytest

from aegentra import EffectScope, Operation
from aegentra.sandbox import RefundLedger


@pytest.fixture
def operation() -> Operation:
    return Operation(
        "refund-42",
        EffectScope("tenant-a", "payments", "order-42", "refund"),
        "approval-42",
        {"amount_minor": 5000, "currency": "USD"},
    )


@pytest.fixture
def ledger(tmp_path: Path) -> RefundLedger:
    return RefundLedger(tmp_path / "provider.sqlite3")


@pytest.fixture
def legacy_journal(tmp_path: Path) -> Path:
    path = tmp_path / "legacy.sqlite3"
    schema = Path(__file__).with_name("fixtures") / "journal_v1.sql"
    with sqlite3.connect(path) as db:
        db.executescript(schema.read_text())
    return path
