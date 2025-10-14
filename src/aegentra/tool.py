"""The narrow provider boundary required by the runtime."""

from typing import Protocol

from .models import Observation, Operation, OperationRecord


class Tool(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def contract_version(self) -> str: ...

    def validate(self, operation: Operation) -> None:
        """Validate intent and declared scope before a dispatch can be reserved."""
        ...

    async def execute(self, record: OperationRecord) -> Observation:
        """One possible write. Return unknown when the response is inconclusive."""
        ...

    async def observe(self, record: OperationRecord) -> Observation:
        """Read-only reconciliation. Absence alone must not establish non-execution."""
        ...
