# Bounded evidence gathering

`reconcile` still performs at most one read-only provider lookup. Use `recover`
when a caller explicitly wants a bounded sequence:

```python
from aegentra import RecoveryBudget

outcome = await runtime.recover(
    operation.key,
    tool=refund_tool,
    budget=RecoveryBudget(
        max_observations=3,
        timeout_seconds=5.0,
        interval_seconds=0.25,
    ),
)
```

The count limits started `Tool.observe` calls, including a lookup interrupted by
the deadline. The optional deadline includes polling waits. There is no wait after
the last allowed lookup. A settled operation returns its stored outcome without
using the allowance. Recovery does not call `Tool.execute`.

Exhaustion records `recovery_exhausted`, returns `Unresolved`, and retains the
scope obligation. It says nothing about whether the provider applied the write.
The record's latest reason describes exhaustion; earlier events retain the
provider's individual observations.

The deadline is cooperative, using asyncio cancellation. Blocking synchronous
adapter code and adapters that suppress cancellation cannot be forcibly stopped.
A definitive observation returned while cancellation is being unwound is retained.
An adapter's own `TimeoutError`, other unexpected exceptions, and caller
cancellation propagate unchanged; they are not converted into budget exhaustion.
Interrupted reservations remain uncertain.

An operation stays active for the whole recovery call, including polling waits.
Concurrent calls cannot probe it again, a conflicting proposal is still blocked,
and the runtime cannot close until recovery finishes or is cancelled and awaited.
Budgets are per call, not persistent global quotas or provider cost guarantees.
