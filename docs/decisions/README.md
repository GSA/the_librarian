# Design decisions

**Audience:** maintainers, operators and their agents.
**Status:** recorded decisions and open evaluations. A decision here is a
design commitment, not a statement that the capability is implemented.

## Admission rule

A decision record is for something a maintainer or agent must know *before*
changing code, and could not learn from the code, its tests and the operator
docs. Add one only when the change does at least one of these:

1. **Commits to an identity, store-compatibility, topology or distribution
   contract** (0001-0005).
2. **Declines or defers a capability, or draws a boundary** whose absence no
   test can show (0006, 0007, 0008).
3. **Sets or changes a safety, trust, durability or privacy property**, including
   any weakening of what a validation, receipt, attestation, backup or audit
   proves, and states the residual risk and the owner's approval (0010, 0011,
   0017).

These do not get a new record:

- **A performance or implementation change that keeps every check and
  semantic** (cheaper validation, memoization, planning cost, lock hold). Its
  measurements belong in the Kata and the PR, its reproduction in a benchmark
  under `tools/diagnostics/`, and its current behavior in
  [`context/intake.md`](../../context/intake.md) or the README. The code and
  tests carry the rule.
- **A change to documented semantics owned by an existing record.** Amend that
  record in place under a dated *Amendment* heading and update the operator
  docs, as the 2vfn memo amendment did in 0012 and 0021 did for 0009.
- **A fix that restores documented behavior.**

A record states its date and status, links every record it amends or
supersedes (and the amended record links back), and says which of its rules code
or tests enforce and which exist only here. "Not adopted" items name a revisit
trigger that is also tracked in a Kata. When a rule is superseded, mark the old
text rather than leaving it to contradict the new one.

Each document states its own status. `Accepted` means the direction is agreed
and may be implemented; `Implemented` means the change is present in this
repository and exercised by its tests; `Proposed` means the design is written
but not agreed; `Open` means the question is deliberately unresolved.

## Where to read for current behavior

Records are dated and amend each other; read the operator contract first, then
only the records that own the rule you are changing.

| Question | Start with | Then |
|---|---|---|
| What does a catalog write validate, and when does it replay history? | [`context/intake.md`](../../context/intake.md) (concurrent intake agents) | [0015](0015-trusted-head-catalog-commit.md) (delta on a trusted head), [0017](0017-attested-file-identity-for-catalog-receipts.md) (current receipt binding and its residual; replaces 0015's hash binding), [0016](0016-single-replay-daily-snapshot.md) (first write of a UTC day) |
| How do writers share the catalog and what happens on a lost race? | `context/intake.md` | [0013](0013-concurrent-intake-writers.md), [0018](0018-catalog-contention-outside-the-fault-budget.md) |
| When is the intake ledger fully validated? | `context/intake.md` | [0014](0014-intake-ledger-handoff-and-bounded-waits.md), the 0012 memo amendment |
| What is backed up and what can be lost? | README (stores) | [0010](0010-daily-snapshots-and-retention.md), [0016](0016-single-replay-daily-snapshot.md) |
| How is a cohort qualified into the index? | `context/intake.md` (read-only cohort intake) | [0009](0009-read-only-cohort-intake.md), [0021](0021-cohort-scoped-selection-reconciliation.md) |

## Identity, distribution and topology

| Decision | Subject | Status |
|---|---|---|
| [0001](0001-project-identity.md) | Project, package, command and protocol identity | Implemented |
| [0002](0002-updateable-distribution.md) | Reproducible runtime and an updateable distribution | Accepted; allowlist packaging (`tools/package.py`) and the preflight probe exist, the lock file, image and installer do not |
| [0003](0003-store-compatibility.md) | Data-store compatibility and upgrade contract | Implemented; retention bullet superseded by 0010 |
| [0004](0004-instance-topology.md) | One Librarian per project, with its own data root | Implemented, except the per-instance version record |
| [0005](0005-source-onboarding.md) | Starting empty and onboarding sources through an inbox | Accepted; manual upload and cohort intake ([0009](0009-read-only-cohort-intake.md)) exist, connectors and scheduling do not |
| [0008](0008-proof-of-concept-closure.md) | Concluded proof of concept and adopted reusable lessons | Accepted; historical, nothing to enforce beyond the clean-copy tests |

## Declined capabilities and boundaries

| Decision | Subject | Status |
|---|---|---|
| [0006](0006-relationship-traversal.md) | Relationship traversal and graph storage options | Open; no graph capability exists, precondition 1 unsatisfied |
| [0007](0007-execution-traces.md) | Agent execution traces: diagnostics, not memory | Accepted as a boundary; agent memory declined, coverage-gap diagnostics implemented with bare read counts, dead-weight requirement withdrawn, no retrieval-quality figure derived |

## Safety, trust, durability and privacy properties

| Decision | Subject | Status |
|---|---|---|
| [0009](0009-read-only-cohort-intake.md) | Read-only cohort intake and model-assisted standing review | Implemented; bulk review gated per instance (code does not enforce the approved-ceiling rule); `qualify` reconciliation amended by [0021](0021-cohort-scoped-selection-reconciliation.md) |
| [0010](0010-daily-snapshots-and-retention.md) | Daily store snapshots with bounded retention | Implemented; supersedes the retention bullet in [0003](0003-store-compatibility.md); amended by [0016](0016-single-replay-daily-snapshot.md) |
| [0011](0011-full-text-mcp-audit-log.md) | Opt-in full-text MCP audit log, separate from diagnostics | Implemented |
| [0017](0017-attested-file-identity-for-catalog-receipts.md) | Catalog validation receipts bind to file identity, with a daily full check | Implemented (owner-approved trust change); amends [0012](0012-bounded-validation-cost.md), [0014](0014-intake-ledger-handoff-and-bounded-waits.md) and [0015](0015-trusted-head-catalog-commit.md); builds on [0016](0016-single-replay-daily-snapshot.md) |
| [0021](0021-cohort-scoped-selection-reconciliation.md) | `qualify` reconciles only its own cohort's selections in a shared manifest | Implemented; amends 0009 |

## Validation, concurrency and cost chain

These records amend each other and mostly describe one subsystem's evolution.
Their current behavior is carried by code, tests and `context/intake.md`; a
current-state design document is proposed to replace the chain as the reading
entry point, and the trust and residual-risk statements in 0014, 0015, 0017 and
0020 must move into it unchanged. Until then they remain authoritative.

| Decision | Subject | Status |
|---|---|---|
| [0012](0012-bounded-validation-cost.md) | Bounded validation cost after daily snapshots | Implemented; amended by [0014](0014-intake-ledger-handoff-and-bounded-waits.md), [0015](0015-trusted-head-catalog-commit.md), [0017](0017-attested-file-identity-for-catalog-receipts.md), [0019](0019-cheaper-catalog-inspect.md) and [0020](0020-constant-cost-upload-planning.md) |
| [0013](0013-concurrent-intake-writers.md) | Concurrent intake writers retry and rebase instead of blocking | Implemented; amended by [0014](0014-intake-ledger-handoff-and-bounded-waits.md), [0015](0015-trusted-head-catalog-commit.md) and [0018](0018-catalog-contention-outside-the-fault-budget.md) |
| [0014](0014-intake-ledger-handoff-and-bounded-waits.md) | Intake ledger hand-off without re-validation, bounded lock waits, short readers | Implemented; amends [0012](0012-bounded-validation-cost.md) and [0013](0013-concurrent-intake-writers.md); amended by [0017](0017-attested-file-identity-for-catalog-receipts.md) and [0018](0018-catalog-contention-outside-the-fault-budget.md) |
| [0015](0015-trusted-head-catalog-commit.md) | Catalog writers validate only their delta on a trusted head | Implemented; amends [0012](0012-bounded-validation-cost.md) and [0013](0013-concurrent-intake-writers.md); amended by [0016](0016-single-replay-daily-snapshot.md) and [0017](0017-attested-file-identity-for-catalog-receipts.md) (the whole-file hash binding is replaced) |
| [0016](0016-single-replay-daily-snapshot.md) | The first write of a UTC day replays the history once | Implemented; amends [0010](0010-daily-snapshots-and-retention.md) and [0015](0015-trusted-head-catalog-commit.md); amended by [0019](0019-cheaper-catalog-inspect.md) |
| [0018](0018-catalog-contention-outside-the-fault-budget.md) | Lost catalog races do not spend an intake stage's fault budget | Implemented; amends 0013 and 0014 |
| [0019](0019-cheaper-catalog-inspect.md) | Catalog `inspect` is cheaper and streams its history | Implemented; amends 0012 and 0016 |
| [0020](0020-constant-cost-upload-planning.md) | Upload planning reads the highest ID per prefix, not every ID | Implemented; amends [0012](0012-bounded-validation-cost.md); leaves [0014](0014-intake-ledger-handoff-and-bounded-waits.md), [0015](0015-trusted-head-catalog-commit.md) and [0017](0017-attested-file-identity-for-catalog-receipts.md) unchanged |

Planning sequence, completion gates and acceptance cases live in the
[development and ingestion roadmap](../../ROADMAP.md). Working rules live in
[AGENTS.md](../../AGENTS.md). These documents contain no program content,
source inventories, identities or operational records.
