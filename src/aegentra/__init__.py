"""Effect-aware recovery for consequential tool operations."""

from .errors import (
    AdapterMismatch,
    AegentraError,
    EvidenceMismatch,
    IntentConflict,
    JournalInUse,
    JournalVersionError,
    OperationNotFound,
    RuntimeUsageError,
    SnapshotUsageError,
)
from .inspection import Inspection
from .models import (
    BlockedAction,
    BlockedByUnresolvedEffect,
    ConfirmedApplied,
    ConfirmedNotApplied,
    EffectScope,
    EventPage,
    JournalEvent,
    JSONValue,
    Knowledge,
    Observation,
    Operation,
    OperationKey,
    OperationPage,
    OperationRecord,
    Outcome,
    Phase,
    Unresolved,
)
from .recovery import RecoveryBudget
from .runtime import Runtime
from .snapshot import DatabaseSnapshot, SnapshotInfo, snapshot_database
from .tool import Tool

__all__ = [
    "JSONValue",
    "AdapterMismatch",
    "AegentraError",
    "BlockedAction",
    "BlockedByUnresolvedEffect",
    "ConfirmedApplied",
    "ConfirmedNotApplied",
    "EffectScope",
    "EventPage",
    "EvidenceMismatch",
    "IntentConflict",
    "Inspection",
    "JournalEvent",
    "JournalInUse",
    "JournalVersionError",
    "Knowledge",
    "Observation",
    "Operation",
    "OperationKey",
    "OperationNotFound",
    "OperationPage",
    "OperationRecord",
    "Outcome",
    "Phase",
    "Runtime",
    "RecoveryBudget",
    "RuntimeUsageError",
    "Tool",
    "Unresolved",
    "DatabaseSnapshot",
    "SnapshotInfo",
    "SnapshotUsageError",
    "snapshot_database",
]
