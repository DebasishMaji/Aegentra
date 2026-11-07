"""Consistent, quarantined SQLite artifacts, not writable recovery journals."""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .errors import JournalVersionError, RuntimeUsageError, SnapshotUsageError
from .models import JSONValue

SNAPSHOT_APPLICATION_ID = 0x41454753
# Older writers validate user_version, but do not validate application_id.
SNAPSHOT_VERSION = 1_000_001


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    snapshot_id: str
    created_at: str
    source_schema_version: int
    source_event_sequence: int

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "source_schema_version": self.source_schema_version,
            "source_event_sequence": self.source_event_sequence,
            "inspection_only": True,
        }


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    path: str
    info: SnapshotInfo
    size_bytes: int
    sha256: str

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            **self.info.as_dict(),
            "format_version": 1,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "includes_all_payloads": True,
        }


def read_snapshot_info(db: sqlite3.Connection) -> SnapshotInfo:
    rows = db.execute(
        "SELECT snapshot_id, created_at, source_schema_version, source_event_sequence "
        "FROM _inspection_snapshot LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        raise JournalVersionError("Invalid inspection snapshot metadata.")
    identifier, created, version, sequence = rows[0]
    if (
        not isinstance(identifier, str)
        or not identifier.strip()
        or not isinstance(created, str)
        or not created.strip()
        or type(version) is not int
        or version not in (1, 2)
        or type(sequence) is not int
        or sequence < 0
    ):
        raise JournalVersionError("Invalid inspection snapshot metadata.")
    actual = db.execute("SELECT COALESCE(MAX(sequence), 0) FROM events").fetchone()[0]
    if actual != sequence:
        raise JournalVersionError("Snapshot event watermark does not match its data.")
    return SnapshotInfo(identifier, created, version, sequence)


def deadline_for(timeout_seconds: float) -> float:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("timeout_seconds must be a positive finite number.")
    try:
        seconds = float(timeout_seconds)
    except OverflowError as exc:
        raise ValueError("timeout_seconds must be a positive finite number.") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("timeout_seconds must be a positive finite number.")
    return monotonic() + seconds


def check_deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise TimeoutError("SQLite operation exceeded its time budget.")


@contextmanager
def sqlite_deadline(db: sqlite3.Connection, deadline: float) -> Iterator[None]:
    check_deadline(deadline)
    db.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
    try:
        yield
        check_deadline(deadline)
    except sqlite3.OperationalError:
        check_deadline(deadline)
        raise
    finally:
        db.set_progress_handler(None, 0)


def database_issues(db: sqlite3.Connection) -> list[JSONValue]:
    checks = [str(row[0]) for row in db.execute("PRAGMA quick_check(10)")]
    issues: list[JSONValue] = [] if checks == ["ok"] else list(checks)
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        issues.append("Foreign key check failed.")
    return issues


def publish_snapshot(
    db: sqlite3.Connection,
    source: Path,
    destination: str | Path,
    *,
    schema_version: int,
    timeout_seconds: float,
) -> DatabaseSnapshot:
    deadline = deadline_for(timeout_seconds)
    requested = Path(destination).absolute()
    target = requested.parent.resolve(strict=True) / requested.name
    reserved = {
        source,
        *(Path(f"{source}{suffix}") for suffix in ("-wal", "-shm", "-journal", ".lock")),
    }
    if str(target).casefold() in {str(path).casefold() for path in reserved}:
        raise SnapshotUsageError("A snapshot must not replace its source or its sidecars.")
    if source.stat().st_nlink != 1:
        raise SnapshotUsageError("Live snapshot sources must not have hard-link aliases.")
    if os.path.lexists(target):
        raise FileExistsError(f"Snapshot destination already exists: {target}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".inspection-", dir=target.parent)
    temporary = Path(temporary_name)
    os.close(descriptor)
    try:
        with sqlite_deadline(db, deadline):
            sequence = db.execute("SELECT COALESCE(MAX(sequence), 0) FROM events").fetchone()[0]
            info = SnapshotInfo(
                uuid4().hex, datetime.now(UTC).isoformat(), schema_version, sequence
            )
            with closing(sqlite3.connect(temporary, isolation_level=None)) as copy:

                def progress(status: int, remaining: int, total: int) -> None:
                    check_deadline(deadline)

                db.backup(copy, pages=128, progress=progress, sleep=0.01)
                with sqlite_deadline(copy, deadline):
                    mode = copy.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
                    if mode != "delete":
                        raise JournalVersionError(
                            "Snapshot requires standalone rollback-journal mode."
                        )
                    copy.execute("PRAGMA synchronous = FULL")
                    checks = [str(row[0]) for row in copy.execute("PRAGMA integrity_check(10)")]
                    if (
                        checks != ["ok"]
                        or copy.execute("PRAGMA foreign_key_check").fetchone() is not None
                    ):
                        raise JournalVersionError("Refusing to publish an inconsistent database.")
                    with copy:
                        copy.execute("BEGIN IMMEDIATE")
                        copy.execute(
                            "CREATE TABLE _inspection_snapshot (snapshot_id TEXT NOT NULL, "
                            "created_at TEXT NOT NULL, source_schema_version INTEGER NOT NULL, "
                            "source_event_sequence INTEGER NOT NULL)"
                        )
                        copy.execute(
                            "INSERT INTO _inspection_snapshot VALUES (?, ?, ?, ?)",
                            (info.snapshot_id, info.created_at, schema_version, sequence),
                        )
                        copy.execute(f"PRAGMA application_id = {SNAPSHOT_APPLICATION_ID}")
                        copy.execute(f"PRAGMA user_version = {SNAPSHOT_VERSION}")
                    read_snapshot_info(copy)
        digest = hashlib.sha256()
        with temporary.open("rb") as artifact:
            while chunk := artifact.read(1024 * 1024):
                check_deadline(deadline)
                digest.update(chunk)
            os.fsync(artifact.fileno())
        size = temporary.stat().st_size
        check_deadline(deadline)
        os.link(temporary, target)
        temporary.unlink()
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        check_deadline(deadline)
        return DatabaseSnapshot(str(target), info, size, digest.hexdigest())
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(f"{temporary}{suffix}").unlink(missing_ok=True)


def snapshot_database(
    source: str | Path, destination: str | Path, *, timeout_seconds: float = 30.0
) -> DatabaseSnapshot:
    """Capture all tenants and raw payloads. Never promote the result to a writer."""
    from .inspection import Inspection

    deadline_for(timeout_seconds)
    try:
        reader = Inspection(source)
    except RuntimeUsageError as exc:
        raise SnapshotUsageError(str(exc)) from exc
    with reader:
        return reader.snapshot(destination, timeout_seconds=timeout_seconds)
