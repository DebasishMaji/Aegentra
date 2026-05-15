# Inspecting a running journal

Use `Inspection` when an operator needs to read a journal owned by a live runtime.
It opens SQLite in read-only mode, takes no writer ownership lock, and never
creates or upgrades a journal.

```python
from aegentra import Inspection

with Inspection("agent.sqlite3") as reader:
    page = reader.list_operations(tenant="tenant-a", limit=100)
    for record in page.records:
        print(record.key, record.knowledge, record.reason)
```

The default list includes only unresolved operations. Use `knowledge=None` to
include settled operations. Follow `page.next_cursor` using `after=` with the same
filters. Event pages use `after_sequence=` and expose `next_sequence`.

Each reader holds a consistent snapshot until close. A writer can continue
committing in WAL mode, but those new commits do not appear inside an existing
reader. Reopen to refresh. Keep inspection contexts short: long-lived snapshots
can prevent SQLite from reclaiming old WAL pages.

`reader.export_trace(key)` returns a versioned JSON-compatible object with the
operation, an event page, and its continuation sequence. Arguments and receipts
are omitted unless `include_payload=True`. Metadata, approval references and
adapter reasons can still be sensitive; this is not an automatic redaction tool.

Both schema 1 and schema 2 can be inspected. Legacy events have no blocked-action
metadata. Reading an older journal does not perform a migration.
