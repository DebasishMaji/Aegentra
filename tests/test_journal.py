import sqlite3
from pathlib import Path

import pytest

from aegentra import (
    JournalInUse,
    JournalVersionError,
    OperationKey,
    OperationNotFound,
    Runtime,
    RuntimeUsageError,
)


def test_second_runtime_is_rejected_and_lock_is_released(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite3"
    with Runtime(path):
        with pytest.raises(JournalInUse):
            Runtime(path)
    with Runtime(path):
        pass


def test_missing_operation_is_an_explicit_error(tmp_path: Path) -> None:
    with Runtime(tmp_path / "agent.sqlite3") as runtime:
        with pytest.raises(OperationNotFound):
            runtime.inspect(OperationKey("tenant", "account", "absent"))


def test_closed_runtime_cannot_be_used(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path / "agent.sqlite3")
    runtime.close()
    runtime.close()
    with pytest.raises(RuntimeUsageError, match="closed"):
        runtime.inspect(OperationKey("tenant", "account", "absent"))


def test_unsupported_schema_fails_and_releases_ownership(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 999")
    for _ in range(2):
        with pytest.raises(JournalVersionError, match="999"):
            Runtime(path)
