"""Errors that must not be mistaken for evidence of non-execution."""


class AegentraError(Exception):
    """Base class for runtime and integration errors."""


class IntentConflict(AegentraError):
    """An existing operation was presented with a different approved intent."""


class AdapterMismatch(AegentraError):
    """Recovery requires the operation's original tool and contract version."""


class EvidenceMismatch(AegentraError):
    """An observation does not belong to the operation being reconciled."""


class OperationNotFound(AegentraError):
    """The journal has no operation with this namespaced identity."""


class JournalInUse(AegentraError):
    """Another runtime owns this journal."""


class JournalVersionError(AegentraError):
    """The journal schema is not supported by this runtime."""


class RuntimeUsageError(AegentraError):
    """The runtime is closed, busy, or used outside its owning thread or event loop."""
