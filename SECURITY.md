# Security policy

## Reporting a vulnerability

GitHub private vulnerability reporting is enabled for this repository. Use
[Report a vulnerability](https://github.com/DebasishMaji/Aegentra/security/advisories/new)
to share a sensitive finding with repository maintainers.

Do not disclose exploit details, credentials, private journals, real receipts,
or personal data in public issues or pull requests. Use a minimal synthetic
reproduction, and include:

- Affected version or commit, Python/SQLite versions, and operating system.
- Expected boundary, observed failure, and the required attacker capabilities.
- Reproduction steps and potential impact without real provider calls.
- Any proposed mitigation and its limitations.

If the private form is unavailable, do not fall back to a public exploit report.
Check the repository security tab and use GitHub Support for reporting-channel
problems. This policy designates no separate security inbox or paid bounty.

## Maintenance and coordination

Security triage and fixes are best effort on current `main`. Older alpha
versions have no guaranteed maintenance window; report their findings anyway.
There is no guaranteed response or remediation time, support agreement, or
production-certification claim.

Keep sensitive details in the private report while maintainers assess scope
and discuss remediation and disclosure. Publication timing should account for
affected users and available mitigations rather than a promised fixed deadline.
A bugfix commit does not itself prove that deployed applications are protected.

## Boundaries to keep in mind

Aegentra relies on trusted application identity, authorization, scope mapping,
provider adapters, and a local filesystem honoring SQLite durability. It does
not authenticate provider finality, provide tenant access control, or make
untrusted database files safe to open. A report that violates a documented
boundary can still be useful; state the preconditions instead of assuming them.

Journals and inspection snapshots can contain plaintext payloads, receipts, and
all tenants' records. A snapshot is not a redacted export; a checksum is not a
signature. Protect files and transfers outside the library.

Do not test against real accounts or live payment endpoints. Do not remove WAL
files, ownership locks, or quarantine markers in a live journal. Prefer
isolated temporary fixtures, and consult [operations](docs/operations.md) and
[execution semantics](docs/semantics.md) for the supported threat boundaries.
