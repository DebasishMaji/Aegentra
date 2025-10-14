"""A persistent refund provider for failure scenarios, not a production payment adapter."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from .models import (
    JSONValue,
    Observation,
    Operation,
    OperationKey,
    OperationRecord,
)


class Scenario(StrEnum):
    ACKNOWLEDGED = "acknowledged"
    LOST_ACKNOWLEDGMENT = "lost_acknowledgment"
    DELAYED_COMMIT = "delayed_commit"
    REJECTED = "rejected"
    UNOBSERVABLE = "unobservable"


def _refund_fields(arguments: dict[str, JSONValue]) -> tuple[int, str]:
    if set(arguments) != {"amount_minor", "currency"}:
        raise ValueError("A refund requires exactly amount_minor and currency.")
    amount = arguments["amount_minor"]
    currency = arguments["currency"]
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount < 2**63:
        raise ValueError("amount_minor must be a positive signed 64-bit integer.")
    if (
        not isinstance(currency, str)
        or len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or not currency.isupper()
    ):
        raise ValueError("currency must be a three-letter uppercase ASCII code.")
    return amount, currency


@dataclass(frozen=True, slots=True)
class Refund:
    provider_id: str
    key: OperationKey
    resource: str
    amount_minor: int
    currency: str


class RefundLedger:
    """Provider-owned state, separate from the runtime's journal.

    There is intentionally no deduplication by operation ID: repeated writes
    create repeated effects, so a broken runtime cannot hide behind the sandbox.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        with closing(self._connect()) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS refund_requests (
                    provider_id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    account TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'applied', 'rejected'))
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous = FULL")
        return db

    def submit(self, record: OperationRecord, *, state: str) -> None:
        amount, currency = _refund_fields(record.arguments)
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO refund_requests
                   (provider_id, tenant, account, operation_id, resource, amount_minor,
                    currency, payload_digest, state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid4()),
                    record.key.tenant,
                    record.key.account,
                    record.key.operation_id,
                    record.scope.resource,
                    amount,
                    currency,
                    record.payload_digest,
                    state,
                ),
            )

    def observe(self, record: OperationRecord) -> Observation:
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT * FROM refund_requests
                   WHERE tenant = ? AND account = ? AND operation_id = ?""",
                (record.key.tenant, record.key.account, record.key.operation_id),
            ).fetchall()
        if not rows:
            return Observation.unknown(
                record, reason="No matching provider record; this does not exclude late execution."
            )
        if len(rows) != 1 or rows[0]["payload_digest"] != record.payload_digest:
            return Observation.unknown(
                record, reason="Provider records conflict with the expected single refund."
            )
        row = rows[0]
        if row["state"] == "pending":
            return Observation.unknown(record, reason="Provider request is still pending.")
        if row["state"] == "rejected":
            return Observation.not_applied(
                record, reason="Provider rejected the request permanently before applying it."
            )
        return Observation.applied(
            record,
            receipt={
                "provider_id": row["provider_id"],
                "amount_minor": row["amount_minor"],
                "currency": row["currency"],
            },
            reason="Provider ledger confirms the matching refund.",
        )

    def commit_pending(self, key: OperationKey) -> None:
        """Advance the provider after client timeout; used only by the scenario driver."""
        with closing(self._connect()) as db, db:
            changed = db.execute(
                """UPDATE refund_requests SET state = 'applied'
                   WHERE tenant = ? AND account = ? AND operation_id = ? AND state = 'pending'""",
                (key.tenant, key.account, key.operation_id),
            ).rowcount
            if changed == 0:
                raise ValueError(f"No pending provider request for {key!r}.")

    def refunds(self) -> tuple[Refund, ...]:
        """Evaluator view of actual effects. Do not expose this as an agent tool."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM refund_requests WHERE state = 'applied' ORDER BY rowid"
            ).fetchall()
        return tuple(
            Refund(
                row["provider_id"],
                OperationKey(row["tenant"], row["account"], row["operation_id"]),
                row["resource"],
                row["amount_minor"],
                row["currency"],
            )
            for row in rows
        )


@dataclass(frozen=True, slots=True)
class RefundTool:
    ledger: RefundLedger
    scenario: Scenario = Scenario.ACKNOWLEDGED
    name: str = "refund_payment"
    contract_version: str = "sandbox-refund/1"

    def validate(self, operation: Operation) -> None:
        if self.name not in {"refund_payment", "refund_order"}:
            raise ValueError("The sandbox supports refund_payment and refund_order.")
        if operation.scope.effect != "refund":
            raise ValueError("Refund tools require the declared 'refund' effect scope.")
        _refund_fields(dict(operation.arguments))

    async def execute(self, record: OperationRecord) -> Observation:
        if self.scenario is Scenario.DELAYED_COMMIT:
            self.ledger.submit(record, state="pending")
            return Observation.unknown(
                record, reason="Client timed out before provider completion."
            )
        if self.scenario is Scenario.REJECTED:
            self.ledger.submit(record, state="rejected")
            return self.ledger.observe(record)

        self.ledger.submit(record, state="applied")
        if self.scenario in {Scenario.LOST_ACKNOWLEDGMENT, Scenario.UNOBSERVABLE}:
            return Observation.unknown(
                record, reason="Acknowledgment lost after possible execution."
            )
        return self.ledger.observe(record)

    async def observe(self, record: OperationRecord) -> Observation:
        if self.scenario is Scenario.UNOBSERVABLE:
            return Observation.unknown(record, reason="Provider offers no usable outcome lookup.")
        return self.ledger.observe(record)
