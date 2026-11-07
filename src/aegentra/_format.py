"""Recognize supported journals before reads, migrations, or dispatch."""

import sqlite3
import stat
from pathlib import Path

from .errors import JournalVersionError, RuntimeUsageError, SnapshotUsageError
from .snapshot import (
    SNAPSHOT_APPLICATION_ID,
    SNAPSHOT_VERSION,
    SnapshotInfo,
    read_snapshot_info,
)


def check_database_path(path: Path) -> None:
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise sqlite3.OperationalError(f"unable to open database file: {path}") from exc
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
        raise RuntimeUsageError(f"SQLite database files must not have hard-link aliases: {path}")


def read_format(
    db: sqlite3.Connection, *, allow_snapshot: bool = False
) -> tuple[int, SnapshotInfo | None]:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    application = db.execute("PRAGMA application_id").fetchone()[0]
    info = None
    if application == SNAPSHOT_APPLICATION_ID or version == SNAPSHOT_VERSION:
        if not allow_snapshot:
            raise SnapshotUsageError("Inspection snapshots cannot be opened as writable journals.")
        if application != SNAPSHOT_APPLICATION_ID or version != SNAPSHOT_VERSION:
            raise JournalVersionError("Invalid inspection snapshot format.")
        info = read_snapshot_info(db)
        version = info.source_schema_version
    elif application != 0:
        raise JournalVersionError("Unrecognized journal application identifier.")
    if version not in (1, 2):
        raise JournalVersionError(f"Unsupported journal version {version}.")
    expected = {
        "operations": (
            "tenant",
            "account",
            "operation_id",
            "resource",
            "effect",
            "approval_ref",
            "tool_name",
            "contract_version",
            "payload_json",
            "knowledge",
            "phase",
            "reason",
            "receipt_json",
            "created_at",
            "updated_at",
        ),
        "events": ("sequence", "tenant", "account", "operation_id", "kind", "reason", "recorded_at")
        + (
            ("requested_operation_id", "requested_tool_name", "requested_contract_version")
            if version == 2
            else ()
        ),
    }
    for table, columns in expected.items():
        actual = tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})"))
        if actual != columns:
            raise JournalVersionError(f"Invalid journal table layout: {table}.")
    index = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'unresolved_scope'"
    ).fetchone()
    if index is None or not isinstance(index[0], str):
        raise JournalVersionError("Invalid unresolved-effect uniqueness index.")
    prefix, separator, predicate = index[0].partition("=")
    if (
        not separator
        or predicate.strip() != "'unknown'"
        or "".join(prefix.lower().split())
        != (
            "createuniqueindexunresolved_scopeonoperations"
            "(tenant,account,resource,effect)whereknowledge"
        )
    ):
        raise JournalVersionError("Invalid unresolved-effect uniqueness index.")
    return version, info
