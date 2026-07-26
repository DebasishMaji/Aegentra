"""Read-only operator commands and a disposable, explicitly scripted laboratory."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections.abc import Sequence
from importlib.metadata import version

from ._serialization import key_dict, record_dict
from .errors import AegentraError
from .inspection import Inspection
from .lab import run_lab
from .models import JSONValue, Knowledge, OperationKey


def _nonempty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _sequence(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from exc
    if result < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return result


def _limit(value: str) -> int:
    result = _sequence(value)
    if not 1 <= result <= 1000:
        raise argparse.ArgumentTypeError("must be between 1 and 1000")
    return result


def _positive(value: str) -> int:
    result = _sequence(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aegentra", description="Inspect effect obligations or run the scripted failure lab."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('aegentra')}")
    commands = parser.add_subparsers(dest="command", required=True)
    lab = commands.add_parser(
        "lab", help="run deterministic refund counterexamples in temporary files"
    )
    lab.add_argument("--max-observations", type=_positive, default=2)
    listing = commands.add_parser("list", help="list unresolved operations by default")
    listing.add_argument("journal")
    listing.add_argument("--tenant", type=_nonempty)
    listing.add_argument("--account", type=_nonempty)
    listing.add_argument(
        "--knowledge", choices=[item.value for item in Knowledge] + ["all"], default="unknown"
    )
    listing.add_argument("--after", nargs=3, type=_nonempty, metavar=("TENANT", "ACCOUNT", "ID"))
    listing.add_argument("--limit", type=_limit, default=100)
    listing.add_argument("--include-payload", action="store_true")

    for command, help_text in (
        ("inspect", "inspect one namespaced operation"),
        ("trace", "export a bounded page of operation history"),
    ):
        detail = commands.add_parser(command, help=help_text)
        detail.add_argument("journal")
        detail.add_argument("--tenant", required=True, type=_nonempty)
        detail.add_argument("--account", required=True, type=_nonempty)
        detail.add_argument("--operation-id", required=True, type=_nonempty)
        detail.add_argument("--include-payload", action="store_true")
        if command == "trace":
            detail.add_argument("--after-sequence", type=_sequence, default=0)
            detail.add_argument("--limit", type=_limit, default=100)
    return parser


def _query(reader: Inspection, arguments: argparse.Namespace) -> dict[str, JSONValue]:
    if arguments.command == "list":
        page = reader.list_operations(
            knowledge=None if arguments.knowledge == "all" else Knowledge(arguments.knowledge),
            tenant=arguments.tenant,
            account=arguments.account,
            after=OperationKey(*arguments.after) if arguments.after is not None else None,
            limit=arguments.limit,
        )
        records: list[JSONValue] = [
            record_dict(record, include_payload=arguments.include_payload)
            for record in page.records
        ]
        return {
            "format_version": 1,
            "operations": records,
            "next_cursor": key_dict(page.next_cursor) if page.next_cursor is not None else None,
        }
    key = OperationKey(arguments.tenant, arguments.account, arguments.operation_id)
    if arguments.command == "inspect":
        return {
            "format_version": 1,
            "operation": record_dict(
                reader.inspect(key), include_payload=arguments.include_payload
            ),
        }
    return reader.export_trace(
        key,
        include_payload=arguments.include_payload,
        after_sequence=arguments.after_sequence,
        limit=arguments.limit,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "lab":
            report = asyncio.run(run_lab(max_observations=arguments.max_observations)).as_dict()
        else:
            with Inspection(arguments.journal) as reader:
                report = _query(reader, arguments)
        print(json.dumps(report, indent=2, sort_keys=True))
    except (AegentraError, sqlite3.Error, OSError) as exc:
        print(f"aegentra: error: {exc}", file=sys.stderr)
        return 1
    return 0
