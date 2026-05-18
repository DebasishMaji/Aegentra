"""Limits on read-only evidence gathering, never permission to retry a write."""

from dataclasses import dataclass
from math import isfinite


def _seconds(value: float, name: str, *, allow_zero: bool) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number of seconds.")
    try:
        finite = isfinite(value)
    except OverflowError as exc:
        raise ValueError(f"{name} is too large.") from exc
    if not finite or value < 0 or (not allow_zero and value == 0):
        qualifier = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{name} must be finite and {qualifier}.")


@dataclass(frozen=True, slots=True)
class RecoveryBudget:
    max_observations: int = 3
    timeout_seconds: float | None = None
    interval_seconds: float = 0.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_observations, bool)
            or not isinstance(self.max_observations, int)
            or self.max_observations < 1
        ):
            raise ValueError("max_observations must be a positive integer.")
        if self.timeout_seconds is not None:
            _seconds(self.timeout_seconds, "timeout_seconds", allow_zero=False)
        _seconds(self.interval_seconds, "interval_seconds", allow_zero=True)
