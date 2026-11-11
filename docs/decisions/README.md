# Design decisions

**Audience:** maintainers, operators and their agents.
**Status:** recorded decisions and open evaluations. A decision here is a
design commitment, not a statement that the capability is implemented.

Each document states its own status. `Accepted` means the direction is agreed
and may be implemented; `Implemented` means the change is present in this
repository and exercised by its tests; `Proposed` means the design is written
but not agreed; `Open` means the question is deliberately unresolved.

| Decision | Subject | Status |
|---|---|---|
| [0001](0001-project-identity.md) | Project, package, command and protocol identity | Implemented |
| [0002](0002-updateable-distribution.md) | Reproducible runtime and an updateable distribution | Accepted; allowlist packaging (`tools/package.py`) and the preflight probe exist, the lock file, image and installer do not |
| [0003](0003-store-compatibility.md) | Data-store compatibility and upgrade contract | Implemented; retention bullet superseded by 0010 |
| [0004](0004-instance-topology.md) | One Librarian per project, with its own data root | Implemented, except the per-instance version record |
| [0005](0005-source-onboarding.md) | Starting empty and onboarding sources through an inbox | Accepted; manual upload and cohort intake ([0009](0009-read-only-cohort-intake.md)) exist, connectors and scheduling do not |
| [0006](0006-relationship-traversal.md) | Relationship traversal and graph storage options | Open; no graph capability exists, precondition 1 unsatisfied |
| [0007](0007-execution-traces.md) | Agent execution traces: diagnostics, not memory | Accepted as a boundary; agent memory declined, coverage-gap diagnostics implemented with bare read counts, dead-weight requirement withdrawn, no retrieval-quality figure derived |
| [0008](0008-proof-of-concept-closure.md) | Concluded proof of concept and adopted reusable lessons | Accepted |
| [0009](0009-read-only-cohort-intake.md) | Read-only cohort intake and model-assisted standing review | Implemented; bulk review gated per instance |
| [0010](0010-daily-snapshots-and-retention.md) | Daily store snapshots with bounded retention | Implemented; supersedes the retention bullet in 0003; amended by 0016 |
| [0011](0011-full-text-mcp-audit-log.md) | Opt-in full-text MCP audit log, separate from diagnostics | Implemented |
| [0012](0012-bounded-validation-cost.md) | Bounded validation cost after daily snapshots | Implemented; amended by 0014 and 0015 |
| [0013](0013-concurrent-intake-writers.md) | Concurrent intake writers retry and rebase instead of blocking | Implemented; amended by 0014 and 0015 |
| [0014](0014-intake-ledger-handoff-and-bounded-waits.md) | Intake ledger hand-off without re-validation, bounded lock waits, short readers | Implemented; amends 0012 and 0013 |
| [0015](0015-trusted-head-catalog-commit.md) | Catalog writers validate only their delta on a trusted head | Implemented; amends 0012 and 0013; amended by 0016 |
| [0016](0016-single-replay-daily-snapshot.md) | The first write of a UTC day replays the history once | Implemented; amends 0010 and 0015 |
| [0017](0017-catalog-contention-outside-the-fault-budget.md) | Lost catalog races do not spend an intake stage's fault budget | Implemented; amends 0013 and 0014 |

Planning sequence, completion gates and acceptance cases live in the
[development and ingestion roadmap](../../ROADMAP.md). Working rules live in
[AGENTS.md](../../AGENTS.md). These documents contain no program content,
source inventories, identities or operational records.
