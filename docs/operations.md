# Embedded-worker operations

This is an operational baseline for a research-alpha SDK, not a production
certification, distributed runtime, or disaster-recovery service.

## Provision once; fail closed on worker startup

Provision a new journal deliberately, before admitting business writes:

```python
from aegentra import Runtime

with Runtime("/srv/agent/obligations.sqlite3"):
    pass
```

Production workers should then open it with:

```python
with Runtime("/srv/agent/obligations.sqlite3", require_existing=True) as runtime:
    # Perform only application-approved operations through the trusted adapter.
    pass
```

`require_existing=True` rejects missing files and uninitialized databases. It
does not authenticate a volume, detect replacement by another valid live
journal, or establish that the file contains all prior provider effects.
Do not implement a fallback that creates a new file, chooses a fresh namespace,
or invents new operation IDs after an open/recovery error.

Existing database files are opened only through SQLite. Opening and closing a
raw descriptor, even just to check permissions, can release another connection's
process-wide POSIX locks; see [SQLite's locking caveat, section 2.2](https://www.sqlite.org/howtocorrupt.html).
Do not independently open/read/close a live database in the same process.
Use `Inspection` and the snapshot API for diagnostics and captures.

Deliberate first-time provisioning creates a private `0600` empty file in the
same directory, closes its descriptor, and publishes it with an exclusive hard
link. The temporary name is removed before SQLite connects. A concurrent
provisioner's file is never overwritten and still undergoes normal format
validation. Existing permissions are preserved; writable startup rejects files
without effective-user read/write access. Provisioning installs an empty file,
not an initialized schema, a recovery copy, or proof of a journal's identity.

One runtime owns a journal, in one process, thread, and event loop. Await or
cancel its active operations before closing it. Offloaded operator work must
construct its own `Inspection` in the receiving thread, not share an existing
connection. Ownership locks are advisory; do not remove the retained `.lock`
file or let other applications mutate the database.

Use a trusted local filesystem with SQLite WAL support, reliable synchronization,
hard-link support for first-time provisioning and snapshots, and directory-sync
support for snapshot publication. Do not deploy on
a network share, synchronize live files with a file-sync agent, or swap database
files underneath open connections. WAL activation is checked, but that cannot
prove the hardware honors durable writes.
Do not use hard-link aliases for live SQLite files: aliases can select different
WAL/lock paths. `Runtime` and `Inspection` reject multiply-linked database files
with `RuntimeUsageError` before SQLite opens them. Snapshot capture rejects them
with `SnapshotUsageError`, including before reading the source. Both the original
name and its alias are refused: link count cannot establish which filename owns
the authoritative WAL. Read-only inspection archives also require an unaliased
database file. Normal symlinks are resolved to their target and remain supported.

This checks the current link count, not history completeness, a WAL's identity,
or filesystem changes after opening. Do not repair failures by blindly deleting
aliases or sidecars. Stop affected workers, preserve the files, and establish the
authoritative database/WAL pair before reopening. No automatic repair or choice
of an authoritative filename is performed. Snapshot capture also reserves
case-folded source/sidecar destinations, including on case-sensitive disks.

## Read-only diagnostics

```sh
aegentra status /srv/agent/obligations.sqlite3 --timeout 5
aegentra list /srv/agent/obligations.sqlite3 --limit 100
aegentra trace /srv/agent/obligations.sqlite3 \
  --tenant shop --account payments --operation-id approved-refund-42 --limit 100
```

These commands never create or migrate the journal and do not acquire its writer
ownership lock. `status` reports a SQLite quick check, foreign-key check,
operation counts by knowledge, and event count/watermark in one pinned read view.
`healthy=true` means these structural checks passed, **not** that a provider
effect is settled, an approval is valid, or the journal has a complete history.
Use the adapter and the existing reconciliation API for business evidence.

JSON goes to stdout; operational errors go to stderr with exit 1; invalid
arguments exit 2. Failed structural checks emit JSON with `healthy=false` and
exit 3. The laboratory/testing commands also retain their documented exit-3
invariant-violation behavior. Arguments and receipts require `--include-payload`;
identifiers, approval references, and reasons can still be sensitive.

## Consistent inspection snapshots

```sh
aegentra snapshot /srv/agent/obligations.sqlite3 \
  --out /srv/archives/obligations-incident.sqlite3 --timeout 30
aegentra status /srv/archives/obligations-incident.sqlite3
```

```python
from aegentra import Inspection, snapshot_database

receipt = snapshot_database("obligations.sqlite3", "incident.sqlite3")
print(receipt.as_dict())
with Inspection("incident.sqlite3") as reader:
    print(reader.status())
```

The destination directory must already exist and be trusted. Every snapshot
contains the **entire database: all tenants/accounts, raw arguments, receipts,
and events**, even when later inspection selects one tenant. This is not a
redacted or tenant-scoped export. New artifacts are mode `0600`; protect archive
directories, transfers, and any copied receipts as well.

Capture uses SQLite backup from a pinned read transaction, including committed
WAL content while a writer remains active. `Inspection.snapshot()` captures that
inspection's existing view, not commits that occurred after it opened. A long
read view can retain WAL pages; keep inspections short and monitor disk space.

The copy is integrity/foreign-key checked, converted to standalone rollback
journaling, tagged inspection-only, hashed, file-synced, and published with an
exclusive atomic hard link followed by directory sync. Existing targets,
including dangling symlinks, are never overwritten. A distinct application ID
and unsupported-for-writers version marker quarantine it; both current and
pre-hardening writers reject it. Only inspection reads understand the artifact.
Capture refuses to relabel an existing snapshot as new live evidence.

The receipt includes the snapshot ID, creation timestamp, original schema,
captured global event watermark, byte count, and SHA-256 checksum. Creation time
is not a provider-state timestamp; the checksum is not a signature or a
tamper-proof audit. Retain the receipt independently if you need to compare
archive integrity after transfer.

Budgets are cooperative: SQLite VM checks, backup chunks, and hashing check the
deadline. They cannot preempt a blocked operating-system call or impose an exact
wall-clock limit on source opening, disk I/O, or synchronization. On failure,
private temporary files are cleaned up. **Failure after atomic publication can
leave a complete artifact without a successful receipt**, for example if
directory sync fails. Preserve and investigate it; do not overwrite or delete a
potentially consumed archive automatically.

## Recovery and production acceptance

An old journal can omit a reservation whose external effect already happened.
Opening that old state as live authority can authorize a duplicate write.
There is deliberately **no snapshot promotion, automatic restore, downgrade,
pruning, or operator override API**. Do not edit snapshot markers to bypass the
guard, copy an old live database into service, or copy just a WAL database's main
file as a backup.

For an interrupted but intact journal, restart the worker against the same live
file with `require_existing=True`, preserve operation IDs/contracts, and
reconcile through the original adapter. For missing/corrupt state, stop dispatch,
preserve available journal/WAL/provider evidence, and involve the application
owner in reconstructing authoritative state. This library does not automate
that reconstruction or claim uninterrupted availability after data loss.

Before accepting a deployment, exercise the actual provider's ambiguous outcomes,
restart/reconciliation, cancellation, volume loss, operator access, and disk-full
behavior. Establish alerts for unresolved age/counts, storage errors, WAL/disk
growth, and archive failures. Enforce payload/storage quotas in the application.
Manage encryption at rest, access control, audit export, retention, and credential
handling externally. No compliance attestation or provider-specific production
validation is implied by this local-storage baseline.
