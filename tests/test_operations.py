import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
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
    RuntimeUsageError,
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


def assert_sqlite_main_lock_held(path: Path) -> None:
    """Probe SQLite's shared-lock range from a child, leaving this process's locks alone."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import fcntl
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDWR)
try:
    try:
        fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0x40000002)
    except BlockingIOError:
        print("held")
    else:
        print("released")
finally:
    os.close(descriptor)
""",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "held"


@pytest.mark.parametrize("require_existing", [False, True])
def test_runtime_startup_preserves_an_existing_inspections_sqlite_lock(
    tmp_path: Path, require_existing: bool
) -> None:
    path = tmp_path / "live.db"
    with Runtime(path):
        pass
    with Inspection(path) as reader:
        before = reader.status()
        assert_sqlite_main_lock_held(path)
        with Runtime(path, require_existing=require_existing):
            assert_sqlite_main_lock_held(path)
        assert_sqlite_main_lock_held(path)
        assert reader.status() == before


def test_provisioning_never_overwrites_a_concurrent_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "live.db"

    def race(source: Path, target: Path) -> None:
        target.write_bytes(b"another provisioner's file")
        raise FileExistsError("Another provisioner won.")

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", race)
        with pytest.raises(sqlite3.DatabaseError):
            Runtime(path)
    assert path.read_bytes() == b"another provisioner's file"
    assert not list(tmp_path.glob(".aegentra-provision-*"))


def test_failed_provisioning_cleans_temporary_files_and_releases_the_owner_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "live.db"

    def fail(source: Path, target: Path) -> None:
        raise OSError("Injected provisioning failure.")

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", fail)
        with pytest.raises(OSError, match="Injected provisioning"):
            Runtime(path)
    assert not path.exists()
    assert not list(tmp_path.glob(".aegentra-provision-*"))
    with Runtime(path):
        pass
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.geteuid() == 0, reason="Root can write files without write mode bits.")
def test_runtime_still_rejects_a_filesystem_read_only_database(tmp_path: Path) -> None:
    path = tmp_path / "live.db"
    with Runtime(path):
        pass
    path.chmod(0o400)
    try:
        with pytest.raises(PermissionError):
            Runtime(path, require_existing=True)
    finally:
        path.chmod(0o600)
    with Runtime(path, require_existing=True):
        pass


@pytest.mark.parametrize("mode", ["runtime", "require_existing", "inspection", "snapshot"])
@pytest.mark.parametrize("open_original", [False, True])
def test_hard_linked_database_paths_are_rejected_before_sqlite_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, open_original: bool
) -> None:
    source, alias = tmp_path / "source.db", tmp_path / "alias.db"
    with Runtime(source):
        pass
    before = source.read_bytes()
    os.link(source, alias)
    path = source if open_original else alias

    def unexpected_open(*args, **kwargs):
        raise AssertionError("An aliased database must not reach SQLite.")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", unexpected_open)
        expected = SnapshotUsageError if mode == "snapshot" else RuntimeUsageError
        with pytest.raises(expected, match="hard-link"):
            if mode == "snapshot":
                snapshot_database(path, tmp_path / "capture.db")
            elif mode == "inspection":
                Inspection(path)
            else:
                Runtime(path, require_existing=mode == "require_existing")
    assert source.read_bytes() == before
    for candidate in (source, alias):
        assert not Path(f"{candidate}-wal").exists()
        assert not Path(f"{candidate}-shm").exists()
    assert not list(tmp_path.glob(".aegentra-provision-*"))


def test_symlink_paths_resolve_to_the_single_authoritative_database(tmp_path: Path) -> None:
    source, alias = tmp_path / "source.db", tmp_path / "symlink.db"
    with Runtime(source):
        pass
    alias.symlink_to(source)
    with Runtime(alias, require_existing=True):
        with Inspection(alias) as reader:
            assert reader.status()["healthy"] is True
    assert alias.is_symlink()
    assert source.stat().st_nlink == 1


def test_cli_rejects_aliases_without_a_healthy_report_or_traceback(tmp_path: Path) -> None:
    source, alias = tmp_path / "source.db", tmp_path / "alias.db"
    with Runtime(source):
        pass
    os.link(source, alias)
    result = cli("status", str(alias))
    assert result.returncode == 1
    assert result.stdout == ""
    assert "hard-link" in result.stderr
    assert "Traceback" not in result.stderr


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


async def test_literal_percent_escapes_and_unicode_database_paths(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    journal_dir = tmp_path / "téřřä_✨_δοκιμή"
    journal_dir.mkdir()
    source = journal_dir / "journal%2Fname.db"

    archive_dir = tmp_path / "áŕćhïvé_📦_δοκιμή"
    archive_dir.mkdir()
    destination = archive_dir / "snapshot%2Farchive.db"

    tool = RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT)

    # 1. Create an unresolved sandbox operation in that database and close the runtime.
    with Runtime(source) as runtime:
        await runtime.perform(operation, tool=tool)
        assert runtime.inspect(operation.key).knowledge is Knowledge.UNKNOWN

    # 2. Reopen the exact file with require_existing=True.
    with Runtime(source, require_existing=True) as reopened:
        reopened_record = reopened.inspect(operation.key)
        assert reopened_record.knowledge is Knowledge.UNKNOWN
        assert reopened_record.arguments == operation.arguments

    # 3. Confirm read-only inspection and CLI diagnostics select that exact journal%2Fname.db file.
    with Inspection(source) as reader:
        status = reader.status()
        assert status["healthy"] is True
        assert status["operations_by_knowledge"] == {"unknown": 1}
        unresolved = reader.find_unresolved(operation.scope)
        assert unresolved is not None
        assert unresolved.key == operation.key
        assert reader.inspect(operation.key).knowledge is Knowledge.UNKNOWN
        listing = reader.list_operations(knowledge=Knowledge.UNKNOWN)
        assert len(listing.records) == 1
        assert listing.records[0].key == operation.key

        # 4. Capture a snapshot/archive to unusual destination (Unicode dir + literal %2F).
        artifact = reader.snapshot(destination)

    # CLI diagnostics on source database
    status_result = cli("status", str(source))
    assert status_result.returncode == 0, status_result.stderr
    status_report = json.loads(status_result.stdout)
    assert status_report["healthy"] is True
    assert status_report["operations_by_knowledge"] == {"unknown": 1}

    list_result = cli("list", str(source))
    assert list_result.returncode == 0, list_result.stderr
    list_report = json.loads(list_result.stdout)
    assert len(list_report["operations"]) == 1
    assert list_report["operations"][0]["key"]["operation_id"] == operation.key.operation_id

    inspect_result = cli(
        "inspect",
        str(source),
        "--tenant",
        operation.key.tenant,
        "--account",
        operation.key.account,
        "--operation-id",
        operation.key.operation_id,
    )
    assert inspect_result.returncode == 0, inspect_result.stderr
    inspect_report = json.loads(inspect_result.stdout)
    assert inspect_report["operation"]["knowledge"] == "unknown"

    # 5. Confirm the captured archive remains inspection-only.
    with Inspection(destination) as saved:
        saved_status = saved.status()
        assert saved_status["healthy"] is True
        assert saved_status["operations_by_knowledge"] == {"unknown": 1}
        assert saved_status["snapshot"]["inspection_only"] is True
        assert saved.snapshot_info == artifact.info
        assert saved.inspect(operation.key).knowledge is Knowledge.UNKNOWN
        assert saved.export_trace(operation.key)["snapshot"]["inspection_only"] is True

    archive_cli_result = cli("status", str(destination))
    assert archive_cli_result.returncode == 0, archive_cli_result.stderr
    archive_cli_report = json.loads(archive_cli_result.stdout)
    assert archive_cli_report["healthy"] is True
    assert archive_cli_report["snapshot"]["inspection_only"] is True

    destination_bytes = destination.read_bytes()
    with pytest.raises(SnapshotUsageError):
        Runtime(destination)
    with pytest.raises(SnapshotUsageError):
        Runtime(destination, require_existing=True)
    with pytest.raises(SnapshotUsageError):
        snapshot_database(destination, archive_dir / "relabeled%2Fcopy.db")
    assert destination.read_bytes() == destination_bytes

    # 6. Explicitly assert no unintended decoded sibling/alternate path was created.
    assert source.is_file()
    assert destination.is_file()

    assert not (journal_dir / "journal").exists()
    assert not (journal_dir / "journal" / "name.db").exists()
    assert not (journal_dir / "name.db").exists()
    assert not (tmp_path / "journal").exists()

    assert not (archive_dir / "snapshot").exists()
    assert not (archive_dir / "snapshot" / "archive.db").exists()
    assert not (archive_dir / "archive.db").exists()
    assert not (tmp_path / "snapshot").exists()

    for entry in journal_dir.rglob("*"):
        assert "journal%2Fname.db" in entry.name
    for entry in archive_dir.rglob("*"):
        assert "snapshot%2Farchive.db" in entry.name


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


def test_snapshot_deadline_bounds_source_lock_wait_and_allows_retry(tmp_path: Path) -> None:
    source, target = tmp_path / "live.db", tmp_path / "capture.db"
    with Runtime(source):
        pass
    with sqlite3.connect(source, isolation_level=None) as lock:
        assert lock.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
        lock.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError):
                snapshot_database(source, target, timeout_seconds=0.02)
        finally:
            elapsed = time.monotonic() - started
            lock.execute("ROLLBACK")
        assert elapsed < 1.0
    assert not target.exists()
    assert not list(tmp_path.glob(".inspection-*"))
    receipt = snapshot_database(source, target)
    with Inspection(target) as reader:
        assert reader.status()["healthy"] is True
    assert receipt.info.source_event_sequence == 0


@pytest.mark.parametrize("validation_elapsed", [6.0, 11.0])
def test_snapshot_does_not_restart_the_budget_after_source_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, validation_elapsed: float
) -> None:
    from aegentra import _format
    from aegentra import inspection as inspection_module

    source, target = tmp_path / "live.db", tmp_path / "capture.db"
    with Runtime(source):
        pass
    clock = 0.0
    original_format = _format.read_format
    original_connect = sqlite3.connect

    def validate(*args, **kwargs):
        nonlocal clock
        result = original_format(*args, **kwargs)
        clock = validation_elapsed
        return result

    class SlowCopy(sqlite3.Connection):
        def backup(self, target, *, pages=-1, progress=None, name="main", sleep=0.250):
            def step(status, remaining, total):
                nonlocal clock
                clock = 11.0
                progress(status, remaining, total)

            return super().backup(target, pages=pages, progress=step, name=name, sleep=sleep)

    monkeypatch.setattr(_format, "read_format", validate)
    monkeypatch.setattr(inspection_module, "read_format", validate)
    monkeypatch.setattr(snapshot, "monotonic", lambda: clock)
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: original_connect(*args, **kwargs, factory=SlowCopy),
    )
    with pytest.raises(TimeoutError):
        snapshot_database(source, target, timeout_seconds=10.0)
    assert not target.exists()
    assert not list(tmp_path.glob(".inspection-*"))


def test_sqlite_deadline_preserves_a_shorter_busy_policy_and_restores_it(tmp_path: Path) -> None:
    source = tmp_path / "locked.db"
    with sqlite3.connect(source, isolation_level=None) as lock:
        lock.execute("CREATE TABLE entries (value TEXT)")
        lock.execute("BEGIN EXCLUSIVE")
        with sqlite3.connect(source, timeout=0) as reader:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                with snapshot.sqlite_deadline(reader, snapshot.deadline_for(1.0)):
                    reader.execute("SELECT * FROM entries").fetchall()
            assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        lock.execute("ROLLBACK")
    with sqlite3.connect(source) as reader:
        reader.execute("PRAGMA busy_timeout = 1234")
        with snapshot.sqlite_deadline(reader, snapshot.deadline_for(0.02)):
            assert reader.execute("PRAGMA busy_timeout").fetchone()[0] <= 20
        assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
        assert reader.execute("SELECT * FROM entries").fetchall() == []


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


@pytest.mark.parametrize(
    "defect", ["missing_identity", "partial_identity", "casefold_identity", "casefold_scope"]
)
def test_incompatible_identity_indexes_are_rejected_without_modifying_state(
    tmp_path: Path, defect: str
) -> None:
    from aegentra.journal import _SCHEMA_V1, _UPGRADE_V1_TO_V2

    source, target = tmp_path / "incompatible.db", tmp_path / "capture.db"
    with sqlite3.connect(source) as db:
        for statement in _SCHEMA_V1:
            if defect in ("missing_identity", "partial_identity"):
                statement = statement.replace(
                    "PRIMARY KEY (tenant, account, operation_id)",
                    "CHECK (length(operation_id) > 0)",
                )
            elif defect == "casefold_identity":
                statement = statement.replace(
                    "tenant TEXT NOT NULL", "tenant TEXT COLLATE NOCASE NOT NULL"
                )
            else:
                statement = statement.replace(
                    "resource TEXT NOT NULL", "resource TEXT COLLATE NOCASE NOT NULL"
                )
            db.execute(statement)
        for statement in _UPGRADE_V1_TO_V2:
            db.execute(statement)
        if defect == "missing_identity":
            db.execute("CREATE INDEX identity_shape ON operations(tenant, account, operation_id)")
        elif defect == "partial_identity":
            db.execute(
                "CREATE UNIQUE INDEX identity_shape ON operations(tenant, account, operation_id) "
                "WHERE knowledge = 'unknown'"
            )
        db.execute("PRAGMA user_version = 2")
    before = source.read_bytes()
    with pytest.raises(JournalVersionError, match="identity|uniqueness"):
        with Runtime(source, require_existing=True):
            pass
    with pytest.raises(JournalVersionError, match="identity|uniqueness"):
        with Inspection(source):
            pass
    with pytest.raises(JournalVersionError, match="identity|uniqueness"):
        snapshot_database(source, target)
    assert source.read_bytes() == before
    assert not target.exists()


def test_identity_validation_accepts_an_equivalent_named_unique_index(tmp_path: Path) -> None:
    from aegentra.journal import _SCHEMA_V1, _UPGRADE_V1_TO_V2

    source = tmp_path / "live.db"
    with sqlite3.connect(source) as db:
        for statement in _SCHEMA_V1:
            db.execute(
                statement.replace(
                    "PRIMARY KEY (tenant, account, operation_id)",
                    "CHECK (length(operation_id) > 0)",
                )
            )
        for statement in _UPGRADE_V1_TO_V2:
            db.execute(statement)
        db.execute(
            'CREATE UNIQUE INDEX "identity""key" ON operations'
            "(tenant COLLATE binary, account COLLATE binary, operation_id COLLATE binary)"
        )
        db.execute("PRAGMA user_version = 2")
    with Runtime(source, require_existing=True):
        with Inspection(source) as reader:
            assert reader.status()["healthy"] is True


async def test_case_distinct_operation_and_scope_identifiers_remain_independent(
    tmp_path: Path, operation: Operation, ledger: RefundLedger
) -> None:
    source = tmp_path / "live.db"
    other = replace(
        operation, scope=replace(operation.scope, tenant=operation.scope.tenant.upper())
    )
    with Runtime(source) as runtime:
        first = await runtime.perform(operation, tool=RefundTool(ledger))
        second = await runtime.perform(other, tool=RefundTool(ledger))
        assert first.record.key == operation.key
        assert second.record.key == other.key
        assert runtime.inspect(operation.key) == first.record
        assert runtime.inspect(other.key) == second.record
    assert len(ledger.refunds()) == 2


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
