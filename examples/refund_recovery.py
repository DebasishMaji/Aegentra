"""Run with: uv run python examples/refund_recovery.py"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from aegentra import (
    BlockedByUnresolvedEffect,
    ConfirmedApplied,
    EffectScope,
    Operation,
    Runtime,
    Unresolved,
)
from aegentra.sandbox import RefundLedger, RefundTool, Scenario


async def demonstrate(directory: Path) -> None:
    journal = directory / "agent.sqlite3"
    ledger = RefundLedger(directory / "provider.sqlite3")
    scope = EffectScope("shop", "payments", "order-42", "refund")
    original = Operation(
        "approved-refund-42", scope, "approval-42", {"amount_minor": 5000, "currency": "USD"}
    )
    tool = RefundTool(ledger, Scenario.LOST_ACKNOWLEDGMENT)

    with Runtime(journal) as runtime:
        first = await runtime.perform(original, tool=tool)
        if not isinstance(first, Unresolved):
            raise RuntimeError("The lost acknowledgment should leave an unresolved operation.")
        print("The provider refunded the order. The agent received no confirmation.")

        alternate = Operation("new-call-from-replanning", scope, "approval-42", original.arguments)
        blocked = await runtime.perform(alternate, tool=RefundTool(ledger, name="refund_order"))
        if not isinstance(blocked, BlockedByUnresolvedEffect):
            raise RuntimeError("A new call must not bypass the unresolved effect.")
        print("A different tool and a fresh call ID do not authorize a second refund.")

    with Runtime(journal) as restarted:
        recovered = await restarted.perform(original, tool=tool)
        if not isinstance(recovered, ConfirmedApplied):
            raise RuntimeError("Provider evidence should resolve the original refund.")
        print("After restart, the agent confirms the existing refund without another write.")
        print(f"Actual refunds: {len(ledger.refunds())}")
        for event in restarted.events(original.key):
            if event.blocked_action is not None:
                proposal = event.blocked_action
                print(
                    f"Blocked proposal retained: {proposal.tool_name} "
                    f"({proposal.requested_key.operation_id})"
                )
        print("Journal:", " -> ".join(event.kind for event in restarted.events(original.key)))


def main() -> None:
    with TemporaryDirectory(prefix="aegentra-") as directory:
        asyncio.run(demonstrate(Path(directory)))


if __name__ == "__main__":
    main()
