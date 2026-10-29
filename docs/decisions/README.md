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
| [0002](0002-updateable-distribution.md) | Reproducible runtime and an updateable distribution | Accepted |
| [0003](0003-store-compatibility.md) | Data-store compatibility and upgrade contract | Accepted |
| [0004](0004-instance-topology.md) | One Librarian per project, with its own data root | Accepted |
| [0005](0005-source-onboarding.md) | Starting empty and onboarding sources through an inbox | Accepted |
| [0006](0006-relationship-traversal.md) | Relationship traversal and graph storage options | Open; precondition 1 reported unsatisfied, embedded candidates checked October 22, upstream edge extraction separated October 23 |
| [0007](0007-execution-traces.md) | Agent execution traces: diagnostics, not memory | Accepted as a boundary; agent memory declined, coverage-gap diagnostics implemented with bare read counts, dead-weight requirement withdrawn, no retrieval-quality figure derived |
| [0008](0008-proof-of-concept-closure.md) | Concluded proof of concept and adopted reusable lessons | Accepted |
| [0009](0009-declared-ontology-and-entailment.md) | A declared ontology, and which half of a reasoner is adopted | Accepted; declared vocabulary, contract binding and constraint checking implemented, entailment deferred to a projection that does not exist |

| [0010](0010-business-context-contract.md) | Business entity, claim and relationship contracts | Accepted; pure conformance/resolution implemented, business persistence and traversal remain gated |

Planning sequence, completion gates and acceptance cases live in the
[development and ingestion roadmap](../../ROADMAP.md). Working rules live in
[AGENTS.md](../../AGENTS.md). These documents contain no program content,
source inventories, identities or operational records.
