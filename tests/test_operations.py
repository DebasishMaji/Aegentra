import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from aegentra import (
    Inspection,
    JournalVersionError,
    Knowledge,
    Observation,
    Operation,
    OperationRecord,
    Runtime,
    SnapshotUsageError,
    snapshot,
    snapshot_database,
)
from aegentra.sandbox import RefundLedger, RefundTool, Scenario


def cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aegentra", *arguments],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


async def test_snapshot_preserves_all_tenants_payloads_and_the_pinned_view(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    source, destination = tmp_path / "journal ?#.db", tmp_path / "capture.db"
    tool = RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT)
    other = replace(
        operation,
        operation_id="other",
        scope=replace(operation.scope, tenant="other", resource="other"),
    )
    with Runtime(source) as runtime:
        await runtime.perform(operation, tool=tool)
        applied = await runtime.perform(other, tool=RefundTool(ledger))
        with Inspection(source) as reader:
            events = reader.events(operation.key)
            await runtime.reconcile(operation.key, tool=tool)
            artifact = reader.snapshot(destination)
            assert runtime.inspect(operation.key).knowledge is Knowledge.APPLIED
        with Inspection(destination) as saved:
            assert saved.inspect(operation.key).knowledge is Knowledge.UNKNOWN
            assert saved.events(operation.key) == events
            assert saved.inspect(other.key).receipt_json == applied.record.receipt_json
            assert saved.inspect(operation.key).arguments == operation.arguments
            assert saved.status()["operations_by_knowledge"] == {"unknown": 1, "applied": 1}
            assert saved.snapshot_info == artifact.info
            assert saved.export_trace(operation.key)["snapshot"]["inspection_only"] is True
    assert artifact.sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert artifact.size_bytes == destination.stat().st_size
    assert destination.stat().st_mode & 0o777 == 0o600
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{destination}{suffix}")
        assert not await asyncio.to_thread(sidecar.exists)
    before = destination.read_bytes()
    with pytest.raises(SnapshotUsageError):
        Runtime(destination)
    assert destination.read_bytes() == before
    with pytest.raises(SnapshotUsageError):
        snapshot_database(destination, tmp_path / "relabeled.db")
    assert not (tmp_path / "relabeled.db").exists()


def test_require_existing_never_initializes_missing_or_empty_state(tmp_path: Path) -> None:
    path = tmp_path / "live.db"
    with pytest.raises(FileNotFoundError):
        Runtime(path, require_existing=True)
    assert not path.exists()
    path.touch()
    with pytest.raises(JournalVersionError):
        Runtime(path, require_existing=True)
    assert path.read_bytes() == b""
    with Runtime(path):
        pass
    with Runtime(path, require_existing=True):
        pass
    with pytest.raises(TypeError):
        Runtime(tmp_path / "invalid.db", require_existing="yes")
    assert not (tmp_path / "invalid.db").exists()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal", ".lock"])
def test_source_and_sidecars_are_not_snapshot_destinations(tmp_path: Path, suffix: str) -> None:
    source = tmp_path / "live.db"
    with Runtime(source):
        with pytest.raises(SnapshotUsageError):
            snapshot_database(source, Path(f"{source}{suffix}"))
    assert not list(tmp_path.glob(".inspection-*"))


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_files_and_dangling_symlinks_are_never_replaced(
    tmp_path: Path, symlink: bool
) -> None:
    source, target = tmp_path / "live.db", tmp_path / "occupied.db"
    with Runtime(source):
        pass
    if symlink:
        target.symlink_to(tmp_path / "absent.db")
    else:
        target.write_bytes(b"private existing artifact")
    with pytest.raises(FileExistsError):
        snapshot_database(source, target)
    assert target.is_symlink() if symlink else target.read_bytes() == b"private existing artifact"
    assert not (tmp_path / "absent.db").exists()


def test_case_alias_sidecars_and_hard_link_sources_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "Live.db"
    with Runtime(source):
        pass
    with pytest.raises(SnapshotUsageError):
        snapshot_database(source, tmp_path / "live.DB-WAL")
    alias = tmp_path / "alias.db"
    os.link(source, alias)
    with pytest.raises(SnapshotUsageError, match="hard-link"):
        snapshot_database(alias, tmp_path / "capture.db")
    assert not (tmp_path / "capture.db").exists()


def test_publication_race_does_not_overwrite_or_remove_the_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "live.db", tmp_path / "race.db"
    with Runtime(source):
        pass
    original = os.link

    def race(first: Path, second: Path) -> None:
        second.write_bytes(b"winner")
        original(first, second)

    monkeypatch.setattr(snapshot.os, "link", race)
    with pytest.raises(FileExistsError):
        snapshot_database(source, target)
    assert target.read_bytes() == b"winner"
    assert not list(tmp_path.glob(".inspection-*"))


@pytest.mark.parametrize("failure_call", [1, 2])
def test_sync_failure_is_explicit_and_only_private_temporary_files_are_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_call: int
) -> None:
    source, target = tmp_path / "live.db", tmp_path / "capture.db"
    with Runtime(source):
        pass
    original = os.fsync
    calls = 0

    def fail(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError("injected sync failure")
        original(descriptor)

    monkeypatch.setattr(snapshot.os, "fsync", fail)
    with pytest.raises(OSError, match="injected"):
        snapshot_database(source, target)
    assert target.exists() is (failure_call == 2)
    if target.exists():
        with Inspection(target) as reader:
            assert reader.status()["healthy"] is True
    assert not list(tmp_path.glob(".inspection-*"))


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_invalid_budgets_do_not_create_an_artifact(tmp_path: Path, timeout: float) -> None:
    source, target = tmp_path / "live.db", tmp_path / "capture.db"
    with Runtime(source):
        pass
    with pytest.raises(ValueError):
        snapshot_database(source, target, timeout_seconds=timeout)
    assert not target.exists()


async def test_real_partial_backup_deadline_cleans_up_without_losing_live_state(
    tmp_path: Path, operation: Operation, ledger: RefundLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "live.db", tmp_path / "capture.db"

    class BulkyTool(RefundTool):
        async def execute(self, record: OperationRecord) -> Observation:
            await super().execute(record)
            return Observation.unknown(record, reason="x" * 2_000_000)

    with Runtime(source) as runtime:
        await runtime.perform(operation, tool=BulkyTool(ledger, Scenario.UNOBSERVABLE))
    copying = False
    partial = False
    original = sqlite3.connect

    class TimedConnection(sqlite3.Connection):
        def backup(self, target, *, pages=-1, progress=None, name="main", sleep=0.250):
            nonlocal copying
            copying = True

            def step(status, remaining, total):
                nonlocal partial
                partial = remaining > 0
                progress(status, remaining, total)

            return super().backup(target, pages=pages, progress=step, name=name, sleep=sleep)

    monkeypatch.setattr(snapshot, "monotonic", lambda: 31.0 if copying else 0.0)
    monkeypatch.setattr(
        snapshot.sqlite3,
        "connect",
        lambda *args, **kwargs: original(*args, **kwargs, factory=TimedConnection),
    )
    with pytest.raises(TimeoutError):
        snapshot_database(source, target, timeout_seconds=30.0)
    assert partial
    assert not target.exists()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".inspection-*")))
    with Runtime(source, require_existing=True) as runtime:
        assert runtime.inspect(operation.key).knowledge is Knowledge.UNKNOWN


def test_legacy_snapshot_is_readable_without_migrating_its_source(
    tmp_path: Path, legacy_journal: Path
) -> None:
    before = legacy_journal.read_bytes()
    target = tmp_path / "legacy-capture.db"
    artifact = snapshot_database(legacy_journal, target)
    assert artifact.info.source_schema_version == 1
    with Inspection(target) as reader:
        assert reader.schema_version == 1
        assert reader.list_operations().records == ()
    assert legacy_journal.read_bytes() == before


def test_wal_refusal_is_explicit_and_releases_runtime_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "live.db"
    original = sqlite3.connect

    class RefusesWal(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            if statement == "PRAGMA journal_mode = WAL":
                statement = "PRAGMA journal_mode"
            return super().execute(statement, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(
            sqlite3,
            "connect",
            lambda *args, **kwargs: original(*args, **kwargs, factory=RefusesWal),
        )
        with pytest.raises(JournalVersionError, match="write-ahead"):
            Runtime(path)
    with Runtime(path, require_existing=True):
        pass


@pytest.mark.parametrize("literal", ["UNKNOWN", " unknown", "u n k n o w n"])
def test_partial_index_literal_is_not_normalized_into_a_false_match(
    tmp_path: Path, literal: str
) -> None:
    path = tmp_path / "live.db"
    with Runtime(path):
        pass
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX unresolved_scope")
        db.execute(
            "CREATE UNIQUE INDEX unresolved_scope ON operations "
            f"(tenant, account, resource, effect) WHERE knowledge = '{literal}'"
        )
    with pytest.raises(JournalVersionError, match="uniqueness"):
        Runtime(path)
    with pytest.raises(JournalVersionError, match="uniqueness"):
        Inspection(path)


def test_provisioning_does_not_claim_an_unrelated_view_only_database(tmp_path: Path) -> None:
    path = tmp_path / "unrelated.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE VIEW application_view AS SELECT 1")
    before = path.read_bytes()
    with pytest.raises(JournalVersionError, match="unrecognized"):
        Runtime(path)
    assert path.read_bytes() == before


def test_format_spoofing_and_foreign_key_damage_fail_closed(tmp_path: Path) -> None:
    source, target = tmp_path / "live.db", tmp_path / "capture.db"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA user_version = 2")
    before = source.read_bytes()
    with pytest.raises(JournalVersionError):
        Runtime(source)
    with pytest.raises(JournalVersionError):
        snapshot_database(source, target)
    assert source.read_bytes() == before
    source.unlink()
    with Runtime(source):
        pass
    with sqlite3.connect(source) as db:
        db.execute(
            "INSERT INTO events (tenant, account, operation_id, kind, reason, recorded_at) "
            "VALUES ('absent', 'absent', 'absent', 'test', 'reason', 'time')"
        )
    result = cli("status", str(source))
    assert result.returncode == 3, result.stderr
    assert json.loads(result.stdout)["healthy"] is False
    with pytest.raises(JournalVersionError, match="inconsistent"):
        snapshot_database(source, target)
    assert not target.exists()


def test_operator_commands_and_errors_are_machine_readable(tmp_path: Path) -> None:
    source, target = tmp_path / "live.db", tmp_path / "capture.db"
    with Runtime(source):
        assert cli("status", str(source)).returncode == 0
        result = cli("snapshot", str(source), "--out", str(target))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert report["inspection_only"] is True
    assert cli("status", str(target)).returncode == 0
    for value in ("0", "nan", "inf", "-1"):
        result = cli("snapshot", str(source), "--out", str(target), "--timeout", value)
        assert result.returncode == 2
        assert result.stdout == ""
    missing = tmp_path / "absent.db"
    assert cli("status", str(missing)).returncode == 1
    assert cli("snapshot", str(missing), "--out", str(tmp_path / "new.db")).returncode == 1
    assert not missing.exists()
    assert not (tmp_path / "new.db").exists()
