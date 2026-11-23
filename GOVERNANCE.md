# Project governance

Aegentra is an Apache-2.0 research-alpha project. This policy explains how to
participate and how decisions are made; it is not a support contract.

## Maintainer responsibility

The repository owner, [@DebasishMaji](https://github.com/DebasishMaji), currently
makes final scope, merge, access, and release decisions. Contributors are
welcome to propose changes, review pull requests, and reproduce failures.
Commit volume alone does not confer maintainer access.

Maintainer responsibilities include preserving the documented contracts,
reviewing evidence, crediting contributions, and explaining substantive
rejections. Reviews and security fixes are best effort, without a guaranteed
response time. Automation may assist checks and triage; it does not replace
maintainer responsibility for approvals or releases.

## Proposals and triage

Use [issues](https://github.com/DebasishMaji/Aegentra/issues) for reproducible
bugs, documentation questions, and bounded proposals. Read the
[roadmap](ROADMAP.md) and search existing threads first. Follow the
[contribution guide](CONTRIBUTING.md) for setup and pull requests.

| Label | Meaning |
|---|---|
| `bug` | Reported incorrect behavior; confirmation requires evidence. |
| `help wanted` | Open contribution opportunity; read its scope and acceptance criteria. |
| `good first issue` | Bounded starting task, not a promise that no guidance is needed. |
| `area: docs`, `area: tests`, `area: ci`, `area: examples` | Primary location of the proposed work. |
| `needs design` | Discuss the scenario, interface, and boundaries before implementation. |

Comment with your intended approach before starting. Check assignees and linked
pull requests; coordinate with an existing volunteer rather than duplicating
their work. Assignment indicates coordination, not permanent ownership or
exclusivity. Do not generate placeholder issues or comments just for activity.

## Review and compatibility

Keep changes small enough to connect the reported problem to its evidence.
Public API, schema, adapter-contract, dependency, and laboratory changes need
an issue discussion before implementation. Record decisions in the issue or
pull request so future contributors can understand the tradeoffs.

Review considers correctness, compatibility, test independence, privacy, and
honest claims. A passing sandbox or CI run is not provider certification or a
production guarantee. First-time fork workflows may need maintainer approval;
do not bypass that protection.

Alpha APIs may change. Schema changes must state what existing live journals
can open and how unresolved obligations remain protected. Experimental contract
changes require explicit versioning; routine contributions do not authorize
paper submission, artifact promotion, or a release.

Contributors retain authorship attribution through commits and pull requests.
Maintainers may ask for focused revisions rather than accepting a broad change.
Respectful disagreements should state a concrete counterexample or alternative;
the maintainer records the final decision. Community moderation follows the
[code of conduct](CODE_OF_CONDUCT.md). Sensitive vulnerability reports use the
[private security channel](SECURITY.md), not public issue threads.
