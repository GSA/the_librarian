# Repository simplification design

**Date:** October 28, 2026  
**Status:** Approved in conversation; implementation planning pending  
**Audience:** Maintainers and implementation reviewers

## Intent

Simplify The Librarian before its first corpus and ontology implementation. The
result should be easier for people and agents to navigate, modify, test and
review. Success is lower conceptual load and clearer boundaries, not the
smallest line count.

The repository has no production consumer whose accidental interfaces must be
preserved. Selective breaking changes are therefore acceptable when they make
the architecture materially clearer and all repository callers, tests and
documentation are updated together.

The first authorized corpus will validate the cleaned-up toolkit. Its
instance-specific facts, identities, paths, policy, source material and defaults
must remain outside this reusable, data-empty repository.

## Current observations

The toolkit has strong behavioral safeguards but a costly source layout:

- large standalone Python programs combine domain rules, persistence, command
  parsing and presentation;
- internal scripts often import peers by mutating `sys.path` or loading files
  dynamically;
- executable conventions and argument parsing vary by tool;
- release contents are maintained separately from source layout;
- test setup repeatedly loads scripts and launches subprocesses;
- some test contracts can be discovered more than once;
- the documented full test command requires an installed MCP dependency, while
  the system interpreter does not currently provide it;
- completed implementation plans and extensive historical material are shipped
  beside current operator documentation.

These are candidates for investigation, not advance conclusions that every
large file, subprocess, dynamic import or document is redundant. In particular,
Docling process isolation, store integrity checks, freshness binding, policy
checks, transaction boundaries and mutation-race defenses are intentional.

## Chosen approach

Use cohesive staged simplification rather than either cosmetic cleanup or an
immediate full application rewrite.

Keep current product and authority boundaries, but organize them consistently:

1. executable scripts become thin adapters over importable code where that
   separation makes responsibilities clearer;
2. ordinary imports replace path mutation and dynamic loading where no genuine
   isolation boundary requires them;
3. shared mechanisms are extracted only when they express the same invariant;
4. large modules are split only at independently explainable, testable
   responsibilities;
5. command parsing, JSON output and errors follow consistent conventions;
6. tests share setup where it improves scenario readability, while security and
   race cases remain explicit;
7. packaging and documentation describe the resulting source of truth without
   parallel inventories where avoidable.

A unified top-level application command or generic plugin framework is not a
required outcome. Those abstractions would be premature before a real corpus
validates them.

## Work streams

### 1. Baseline and development workflow

Establish a reproducible baseline using the installed environment required by
the repository. Make missing prerequisites actionable rather than silently
skipping coverage. Audit initialization, packaging, schema export, hook
installation and review scripts as first-class maintained code.

The implementation plan must identify the exact baseline commands and distinguish
required tests from explicitly opt-in Docling tests.

### 2. Imports and command interfaces

Inventory each executable and classify it as:

- a stable operator command;
- a thin entry point over reusable domain code;
- an internal implementation detail; or
- obsolete or redundant.

For each changed interface, update all in-repository callers, tests, help text
and documentation in the same commit. Breaking changes must have a concrete
readability or maintainability benefit; interface churn by itself is not
simplification.

Docling worker process and namespace isolation remain process boundaries. The
cleanup must not replace them with in-process imports merely to reduce files or
subprocess calls.

### 3. Domain modules

Review catalog, review events, context index, renditions, extraction,
diagnostics and MCP separately. A module should have one describable purpose,
a small explicit interface and dependencies visible from imports or arguments.

Potential shared code must be justified by a common contract. Similar-looking
catalog and review-event operations must remain separate if their authority,
validation, schema, transaction or recovery invariants differ. Avoid generic
utility modules that conceal domain language.

The following properties are non-negotiable:

- authority stores are never rebuilt as derived data;
- append-only and hash-chain validation remains intact;
- policy, source, code and catalog mutations continue to fail closed;
- qualifications, attribution, budgets and freshness are not weakened;
- backup and restore checks remain explicit;
- operational data and private configuration remain excluded from releases;
- initialization never seeds a demonstration corpus.

### 4. Tests

Tests should describe behavior through named fixtures and focused helpers rather
than repeated loader and subprocess boilerplate. Consolidation is appropriate
when it exposes intent; it is inappropriate when it hides security-relevant
steps or fault injection.

Remove accidental duplicate discovery or execution. Preserve separate tests
when they verify a contract in both the source checkout and packaged clean copy.
The full suite must use temporary workspaces and must not touch an authoritative
installation.

### 5. Documentation and release contents

Update the README, relevant decisions, roadmap, tool READMEs and explicit
release allowlist when capabilities or interfaces change. Historical plans and
specifications may be removed from the release artifact, or from the active
source tree if genuinely obsolete, only after confirming they are not needed
for current contracts or license/provenance history. Git remains the historical
record.

Do not describe planned extension points as installed functionality.

## Deferred work

Implementation of corpus-, ontology- and instance-extension capabilities is out
of scope. Create or refine prioritized Katas for generic future work, including:

- per-instance configuration and portable store layout;
- source onboarding and normalized text/rendition lifecycle;
- ontology, entity, claim and relationship contracts;
- extension interfaces proven by a real first corpus;
- migration and compatibility policy for those interfaces.

Search and relate existing Katas before creating new ones. Dependencies and
priorities should favor the minimum path to a first evidence-backed corpus.
Private task context may name a specific validation target, but versioned
source and generic defaults must not contain deployment facts.

## Incremental delivery and review

Each coherent change receives its own local commit with focused verification.
RoboRev reviews are inspected after commits; actionable findings are fixed in
new commits, and rejected findings receive a technical disposition. Completed
work is not amended without owner approval.

Before completion:

1. run focused tests throughout development;
2. run the complete installed-environment suite and package checks;
3. regenerate and check schema-only snapshots if storage definitions change;
4. inspect the release artifact contents;
5. run an independent code review against the original request and this design;
6. verify the branch is clean and all actionable RoboRev findings are resolved;
7. create a pull request that lists breaking changes, before/after structure,
   verification evidence, review dispositions and deferred Katas.

A passing package check is not privacy certification or publication approval.
Pushing the working branch and creating the requested pull request does not
authorize release or deployment.

## Success criteria

- A maintainer or agent can identify each major component, its public interface
  and its dependencies without first reverse-engineering script loaders.
- Changed modules have focused responsibilities and fewer implicit couplings.
- Script behavior and development commands are consistent and documented.
- Tests are easier to read and do not accidentally duplicate contracts.
- Safety and authority invariants have direct regression coverage.
- Release contents remain explicit, data-empty and reproducible.
- Breaking changes are enumerated and justified.
- Future corpus and ontology needs exist as prioritized Katas rather than
  speculative framework code.
- The complete supported suite and RoboRev pass before PR creation.

## Out of scope

- corpus content, business ontology or deployment-specific configuration;
- processing or indexing real source material;
- implementing new source connectors or agent automation;
- migrating populated stores or relaxing compatibility checks;
- a generic plugin system or unified command solely for architectural symmetry;
- publication, deployment or changes to global/user configuration.
