"""Shared decoding for writer-owned and read-only journal connections."""

from __future__ import annotations

import sqlite3

from .errors import OperationNotFound
from .models import (
    BlockedAction,
    EffectScope,
    JournalEvent,
    Knowledge,
    OperationKey,
    OperationRecord,
    Phase,
)


def key_values(key: OperationKey) -> tuple[str, str, str]:
    return key.tenant, key.account, key.operation_id


def decode_record(row: sqlite3.Row) -> OperationRecord:
    return OperationRecord(
        key=OperationKey(row["tenant"], row["account"], row["operation_id"]),
        scope=EffectScope(row["tenant"], row["account"], row["resource"], row["effect"]),
        approval_ref=row["approval_ref"],
        tool_name=row["tool_name"],
        contract_version=row["contract_version"],
        payload_json=row["payload_json"],
        knowledge=Knowledge(row["knowledge"]),
        phase=Phase(row["phase"]),
        reason=row["reason"],
        receipt_json=row["receipt_json"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_record(db: sqlite3.Connection, key: OperationKey) -> OperationRecord:
    row = db.execute(
        "SELECT * FROM operations WHERE tenant = ? AND account = ? AND operation_id = ?",
        key_values(key),
    ).fetchone()
    if row is None:
        raise OperationNotFound(f"No operation {key!r}.")
    return decode_record(row)


def get_events(
    db: sqlite3.Connection,
    key: OperationKey,
    *,
    schema_version: int = 2,
    after_sequence: int = 0,
    limit: int | None = None,
) -> tuple[JournalEvent, ...]:
    get_record(db, key)
    metadata_columns = (
        "requested_operation_id, requested_tool_name, requested_contract_version"
        if schema_version == 2
        else "NULL AS requested_operation_id, NULL AS requested_tool_name, "
        "NULL AS requested_contract_version"
    )
    query = f"""SELECT sequence, kind, reason, recorded_at, {metadata_columns}
                FROM events
                WHERE tenant = ? AND account = ? AND operation_id = ? AND sequence > ?
                ORDER BY sequence"""
    parameters: list[str | int] = [*key_values(key), after_sequence]
    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit)
    result: list[JournalEvent] = []
    for row in db.execute(query, parameters):
        blocked = (
            BlockedAction(
                OperationKey(key.tenant, key.account, row["requested_operation_id"]),
                row["requested_tool_name"],
                row["requested_contract_version"],
            )
            if row["requested_operation_id"] is not None
            else None
        )
        result.append(
            JournalEvent(row["sequence"], row["kind"], row["reason"], row["recorded_at"], blocked)
        )
    return tuple(result)
