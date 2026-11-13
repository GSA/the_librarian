# The Librarian development and ingestion roadmap

**Audience:** maintainers, operators and their agents.
**Updated:** October 28, 2026.
**Status:** capability inventory and proposed work, not an implementation,
permission grant, delivery commitment or program-readiness register.

This is the canonical generic roadmap. Keep private source inventories,
access decisions, tickets, job runs, evidence and observations in the program
instance. No customer content is needed to maintain or distribute this plan.

## Goal and current baseline

Discover useful context across an authorized, initially unorganized document
collection; maintain attributed knowledge; support Jira-to-Kata refinement
and engineering; and return supported discoveries to that knowledge base.
The operator should not need to identify relevant documents before asking
a question or refining a ticket.

The current data-empty toolkit includes native catalog/review stores, verified
local backups, configurable source restrictions, metadata inventory, strict
ordinary UTF-8 text, Gemini TXT and ODT extraction, explicitly selected strictly
reviewed Docling text/table retrieval, exact/compact/batched reads and read-only
MCP. The October 3 extraction established the initial data-empty baseline;
later dated sections record subsequent increments. Empty initialization and
source-only packaging remain requirements, not temporary demo limitations.

The October 19 update names the project **The Librarian** and adds the
[Librarian](agents/librarian.md), [ticket/refinement](agents/refiner.md),
[Engineer](agents/coder.md) and
[contribution/disposition](agents/contributions.md) contracts.
These are instructions, not an agent runner, task importer or automatic inbox.

The October 21 update unifies software identity: the checkout, package,
module, commands and MCP protocol name are all renamed to The Librarian, with
no compatibility aliases. Record syntax remains unchanged. The later ingestion
completion supersedes the unchanged-application-ID decision: catalog `LCAT`,
review `LREV`, and derived index `LCTX` explicitly refuse `FCAT`, `FREV`, and
`FCTX`. Authority stores need explicit migration or deliberate empty-store
re-initialization; the derived index can be rebuilt from authority data.
See the [current-state design](docs/design/current-state.md).

### Design baseline - October 21 to November 12

Identity, store compatibility, instance topology, backups, validation, concurrency
and observation are described as they work today in the
[current-state design](docs/design/current-state.md), with the test behind each
claim. The numbered decision records that used to hold this are in git history. The
configuration work they depended on (per-instance store homes, policy that follows
the data root, index identity that does not bind an absolute host path) is
implemented. The pinned lock file, image and installer, and a per-instance version
record, are not built and are open product questions, not commitments.

The October 21 update also renamed the review-event schema `$id` to
`urn:the-librarian:review-events:1`, which changed the store contract hash and
required re-initializing the then-empty local review store.

It also adds an observation capability unlike the ingestion sources: **body-free
retrieval diagnostics** covering coverage gaps, plus bare search and read counts,
described under
[Retrieval diagnostics from agent execution](#retrieval-diagnostics-from-agent-execution).
Agent execution traces as durable agent memory were not built; see the design doc
for the reasoning. The coverage-gap half is implemented: an empty search returns an
absence classification, and an opt-in body-free log records it without the query.

### Docling integration - October 20-26

The optional offline PDF/PNG/JPEG Docling extraction lifecycle (`tools/extraction/docling_extract.py`, `docling_adapter.py`, `docling_bridge.py`, `docling_worker.py`, and `tools/extraction/README.md`) is implemented and verified:
- Prepare twice with independent offline conversions, verify identical rendition bytes, and register deferred renditions in the catalog with idempotent retry.
- Clean-copy tests (`tests/test_kit.py`, `tests/test_docling.py`, `tests/test_worker_contracts.py`) verify packaging, policy isolation, and store-identity boundaries.
- Qualified retrieval can explicitly select exact-rendition, substantively reviewed Docling text and table records, with retained-byte, JSON-pointer, and page provenance. Unsupported content collections fail closed.
- The later standing-policy intake stages implement authorized evidence-based review, qualification, selection, and publication through the existing atomic index (see [the intake contract](context/intake.md)). Parser success alone never qualifies content. Real-document OCR accuracy remains unmeasured.


## Reuse gaps identified on October 19

Comparison covers the extracted repository and the originating installation's
current tooling/guides. It is a capability inventory, not exhaustive code
parity, a correctness audit or permission to copy installation data.
**No runtime backports are included in this documentation update.**
Source component names below identify porting candidates, not files promised
to exist in this distribution.

| Capability | State in this repository | Required action |
|---|---|---|
| Three-agent collaboration and returned findings | Generic contracts captured; no orchestration or Jira/Kata integration | Exercise explicit handoffs, original-criterion preservation, correlation and returned disposition before adding a runner |
| Broader extraction | Strict ordinary UTF-8 text with explicit line retrieval, Gemini TXT and ODT; optional offline Docling PDF/image conversion | Port DOCX/XLSX/PPTX and embedded-text PDF; preserve format limitations, dependency pins and source/engine-bound versions |
| Archive-member and large-workbook handling | No explicit retained-member extraction selector or row-chunk engine | Port member-policy/ancestor checks and versioned XLSX chunking without automatic recursive expansion or legacy-run rewriting |
| Bounded ingestion cohorts | No cohort driver or operation-local catalog snapshot API | Adapt `cohort.py` and `ReceiptMetadataSnapshot`; retain source/state checks, serialized writes, saved requests, explicit partial failures and backups |
| Read-only remote discovery | No remote connector; local explicit-root inventory exists | Generalize `drive-intake` planning and verified retained-byte bindings; remove installation-specific account/drive counts, paths and configuration |
| Continuous copy and intake | Private exact-request intake ledger, explicit native writer stages, bounded attempts/DLQ/replay, acknowledgment recording, and explicit manual file upload / marked local-drop intake; no acquisition or scheduler | Add separately authorized connectors, scheduling and return transport; a read-only mount is not a local copy or ingestion service |
| MCP audit and body-free metrics | Opt-in body-free retrieval diagnostics implemented for search absence and a bare read count (`tools/diagnostics/retrieval_log.py`); opt-in full-text per-agent MCP audit log (`--audit-log`, [design](docs/design/current-state.md#6-observation-audit-log-and-diagnostics)); no cohort metrics | Add cohort metrics if wanted; keep private modes, explicit logging failures and no telemetry-as-job-authority. The audit log is implemented (`tests/test_mcp_audit.py`); see [Retrieval diagnostics from agent execution](#retrieval-diagnostics-from-agent-execution) |
| Retrieval refinements | Exact/compact/batched reads exist; later source-budget batch plans and reversible discovery archives absent | Port deterministic complete-packet sizing guidance and explicit archive inclusion without losing qualifiers or silently retrying |
| Reader diagnostics and regressions | Earlier native event reader retained | Review later fixes, including required/allowed output sizes; preserve exact payloads and outer response-budget caveats |
| Explicit relationship traversal | No derived graph | Evaluate a generic `context-graph` adaptation after core field use; preserve canonical evidence routes and stale-read refusal. Evaluation recorded in the [design](docs/design/current-state.md#7-relationship-traversal) (no real failed question has been recorded) |
| Agent execution traces | No trace endpoint, instructions or schema; nothing records what an agent did with returned context. Search absence is classified and optionally logged body-free, alongside a bare read count | Body-free coverage-gap diagnostics (implemented); reads are not attributed to documents and no retrieval-quality figure is derived. Agent findings route through the contribution contract; see the [design](docs/design/current-state.md#6-observation-audit-log-and-diagnostics) |
| Discovery-card experiments | Not shipped | Retain the lesson: skims can miss relevant material; evaluate candidate recall with deferred controls, never use cards as proof of absence |
| Operating guidance | Generic role/feedback contracts now included; private corpus procedures intentionally omitted | Keep task-triggered loading and source fidelity/authority boundaries; add generic format/recovery guides alongside each future port |

Do not re-port functionality already present: state-pinned native queries,
append-only corrections, validated catalog read receipts, explicit selections,
compact/batch retrieval and the existing original-source exclusion check.
Historical import mappings, domain coverage parsers, private dictionaries,
source documents, review history, tokens and populated stores remain excluded.

## Refresh procedure

1. Compare a pinned source revision and identified working-tree changes with
   this repository. Record the exact port scope; avoid blind directory copies.
2. Generalize policy, source selection and format assumptions. Do not embed
   program identifiers, source paths, permissions or historical mappings.
3. Port one coherent capability with its relevant failure/recovery tests and
   generic operating guide. Preserve old extractor identities; use new
   versions rather than claiming a changed engine verifies old bytes.
4. If persisted structures change, design the explicit migration and recovery
   path before editing populated stores. Schema snapshots are not backups.
5. Run the existing tests and clean-root reproduction. Update the explicit
   release allowlist, inspect every added file, and build a new source archive
   without overwriting a retained earlier artifact.
6. Exercise the capability against authorized instance inputs, recording real
   limitations privately. Synthetic success is not field-use evidence or
   publication clearance. Update this roadmap only for verified capability.

## Automation pipeline

Design for authorized standing policies and agent-driven routine processing,
not a human approval click for every file. Humans resolve consequential
scope, handling and business ambiguity. Initial enablement requires explicit
source/destination/model permissions; each stage needs an implemented contract
before it can run unattended.

### 1. Discover documents and changes

Start with explicit allowlisted local inbox roots and read-only remote folder
scopes. A scheduled reconciliation poll is sufficient for the first version;
watch notifications are hints, not the only source of truth. Traverse permitted
scope broadly enough to discover unknown relevance, not just ticket keywords.

Capture upstream identity, known version/checksum, representation, parent
location, observation time, permissions/handling and complete pagination state.
Keep the durable discovery cursor separate from the committed-ingestion cursor:
finding an item must not mark it processed. Failed pages and expired credentials
remain visible; an empty/unmounted directory is not successful reconciliation.

Binary identity and native-document identity need separate rules. Binary
remote checksums may help compare bytes; retained local content uses SHA-256.
Native document exports need a verified upstream version/export mapping.
If the provider cannot establish it, report `needs-version-verification`;
do not infer unchanged content from titles or timestamps.

### 2. Copy into a preserved private inbox

Copy only permitted content to staging beneath the authorized instance.
Recheck identity/version around acquisition, hash and validate complete bytes,
then publish without overwriting an earlier source version. Reject unsafe paths,
symlinks, collisions, incomplete files and unsupported representations.
Read-only remote access must remain read-only.

Use stable source identity + version + representation for replay decisions.
Same bytes across origins may share a retained representative only with all
origins and handling preserved; they are not independent corroboration.
Renames/moves do not alone create new source content. Changes create linked
versions. Remote deletion must not silently delete retained originals or
backups; loss of access must trigger handling/serving-policy evaluation rather
than assuming an old local copy remains authorized for retrieval.

### 3. Register and extract

Register real receipt/source/artifact metadata through the supported catalog
writer. A copied file is not a registered receipt until its commit is confirmed.
Use a private operational journal for durable work and retries, **not** a
parallel metadata, review or intake-status authority.

Run bounded, format-routed extraction only after registration and handling
checks. Preserve source hashes, exact locators, parser versions, omitted scope
and warnings. Unsupported, encrypted, malformed or over-budget files stay
explicitly pending. No empty success fallback, macro execution or recursive
archive expansion. New adapters need fidelity/recovery tests before scheduling.

### 4. Review and incorporate context

Select bounded review scopes and inspect substantive content, not only
metadata/cards. Save actual attributed reviews and append-only corrections
through the native writer. Put supported knowledge in its canonical home,
link evidence and preserve conflicts, uncertainty and unreviewed scope.

Do not wait for a hypothetical ticket to establish all relevant sources.
Discover and synthesize across the collection; later ticket questions can
trigger deeper reading of known gaps. Routine agent review is allowed under
standing authorization; new mappings and disputed corrections require
independent review. Parsing, review and business acceptance stay separate.

### 5. Publish selected retrieval and return outcomes

Apply explicit versioned source-selection/qualification policy; copying or
registering a document must not automatically make it qualified context.
Build the derived index atomically, validate freshness and exact packets, and
make approved updated knowledge available only after successful publication.
Any future automated selection needs its own defined policy and tests;
the present installation still requires deliberate explicit selection.

Return receipt/disposition to an originating contribution or task when one
exists. Mark a source consumed only under the instance's verified incorporation/
disposition rules, not because it downloaded or extracted successfully.
Record refresh failures and unavailable return routes explicitly.

### 6. Operate, retry and reconcile

The shipped `tools.intake` interface implements the private exact-request ledger,
named-stage native writer recovery, bounded retries, explicit dead-letter replay,
pinned publication and supplied transport acknowledgment described in
[context/intake.md](context/intake.md). Acquisition, orchestration and return
transport remain separate planned integrations; this does not install a service.

Keep a durable bounded job record with source/version/representation,
operation/phase, stable request IDs, attempts, committed catalog/event
references, published artifact identity and failure/revisit information.
Derive authoritative state from existing stores and intake tracking.

Use one authoritative writer per store with existing locking/state checks.
Parsing workers may run independently; they must not invent concurrent commit
authority. Persist exact requests before submission, inspect committed state
after ambiguous failures, and retry identical committed work idempotently.
New bytes or changed semantics require a new linked request.

Resume unfinished jobs after interruption. Use bounded retries/backoff;
after two failures on the same access/copy path, block that item and report
the prerequisite rather than repeatedly hammering it. Continue unrelated
eligible work. Expose backlog age, blocked/error counts, last successful
reconciliation and source-coverage limitations. Body-free metrics are useful
diagnostics, not the durable recovery ledger or evidence of review.

Preserve the verified daily store snapshots (see the [design](docs/design/current-state.md#4-backups)) and retained
originals. Never rebuild authoritative stores as job recovery.
Define pause, clean shutdown and explicit resume before enabling scheduling.

## Retrieval diagnostics from agent execution

**Status: Partly implemented.** Search absence classification and an opt-in
body-free diagnostics log exist and are tested. How they behave, and the tests
that back each claim, are in the
[design](docs/design/current-state.md#6-observation-audit-log-and-diagnostics);
usage is in the [README](README.md) and [`tools/context-mcp/README.md`](tools/context-mcp/README.md). Agent findings persist through
the [contribution and disposition contract](agents/contributions.md), not through
a trace store, and no trace endpoint exists (`tests/test_mcp.py` asserts the
read-only tool set).

### What is implemented

- **Absence classification.** `context_index.search` returns an `absence` object
  only when a search produced no hits: `absent-corpus`, `absent-partial` or
  `present-uncombined`, with term counts and a caveat, never a term, locator or
  excerpt. It uses single-term `MATCH` probes on the existing read-only
  connection, so there is no schema change or rebuild.
- **An opt-in body-free log.** `tools/diagnostics/retrieval_log.py`, enabled by
  `--diagnostics-log <path>.jsonl` on the MCP adapter, records search outcomes and
  a bare, unattributed read count. Its field set is closed, records that
  contradict themselves are refused, and a logging failure raises instead of
  returning the result. `retrieval_log.py report` separates a collection-gap rate
  (`absent-corpus`) from a findability-gap rate (`present-uncombined`);
  `retrieval_log.py prune --keep N` is explicit and never automatic.
- **Not derived.** Reads are not attributed to documents and no search-to-read
  ratio or retrieval-quality figure is reported; the counts cannot separate an
  unconvincing excerpt from an agent that needed nothing more
  (`test_reads_are_counted_but_never_attributed`,
  `test_report_counts_reads_without_deriving_a_ratio`).
- **Boundaries.** The log is never indexed, searched or returned by a read tool,
  is never a review or catalog authority, and nothing derived from it changes
  selection, ranking or qualification. No log content ships in the package.

### Open questions

- Whether recurrence of the same unanswerable question can be clustered without
  storing or reversibly hashing its terms.
- Whether a record being returned versus used can be observed from a stdio
  adapter beyond a bare read count, or needs the calling agent's cooperation.
- Whether gap reporting belongs in this log or in the existing intake tracking,
  which already owns ingestion backlog.


## Delivery sequence and completion gates

| Phase | Scope | Completion gate |
|---|---|---|
| A: manual collaboration | Existing qualified retrieval plus the three role contracts and feedback route | Actual authorized Jira input becomes local Kata tasks and an implemented/reviewed change; a supported contribution receives a persisted disposition |
| B: reusable parity | Prioritize extraction, cohort/snapshot support, metrics and retrieval fixes from the gap table | Generic tests and clean-root packaging pass; version/fidelity contracts and operational limits documented |
| C: local continuous inbox | One allowlisted local inbox; scheduled discovery/copy/register/extract; explicit review/publication boundaries | Restart, duplicate, mutation, failure and committed-retry cases below pass without duplicate authoritative records |
| D: remote discovery/copy | One approved read-only provider/folder scope, then additional scopes | Complete paginated reconciliation, binary/native identity behavior and authorization failures demonstrated; no destructive synchronization |
| E: agent-driven maintenance | Bounded review/incorporation and approved publication policy | Evidence fidelity, independent-review triggers, recoverability and qualified-answer preservation shown under ongoing intake |
| F: retrieval diagnostics | Body-free coverage-gap observation plus bare search and read counts; reads are not attributed to documents and no retrieval-quality figure is derived | Diagnostics are written and read without appearing at any retrieval surface or being countable as reviews; no query text, prompt, model output, source excerpt or locator is ever stored; retention is bounded and pruning is verified; no retrieval behavior changes automatically from diagnostic content. Coverage-gap classification, the bare read count and the opt-in log satisfy this gate |
| Later, only if justified | Graph, richer discovery, hosted/multi-user services | Demonstrated use case and explicit handling, authorization, concurrency and recovery design; not prerequisites for the local demo. Graph evaluation notes are in the [design](docs/design/current-state.md#7-relationship-traversal) |

These are proposed dependencies, not assignments or dates. The full automation
roadmap is not a prerequisite for the first real business demonstration.

## Required acceptance and failure cases

- Repeated polls and restarts do not duplicate receipts, reviews or Kata tasks;
  existing task mappings are reconciled rather than regenerated blindly.
- Same-name different files, same-byte different origins, renamed files and
  changed versions retain correct identity, provenance and handling.
- File mutation during discovery/copy/extraction cannot publish a mixed or
  incorrectly attributed version.
- Native-document changes cannot be skipped on a guessed revision; unsupported
  export identity remains visible.
- Pagination interruption, expired credentials, denied access, lost mounts,
  permission changes and source deletion are distinguishable from no changes.
- Interrupted copy, disk full and crash between publication and registration
  resume without source loss or overwriting a prior version.
- A committed write with failed acknowledgment/backup/logging is reported as
  potentially or actually committed and reconciled using its saved request.
- Source restrictions apply before source/rendition reads and again before
  serving; prompt injection and credentials never become executable authority.
- Parser, budget and partial-cohort failures stay pending and non-successful;
  unrelated items can proceed only under the defined partial-processing policy.
- Stale catalog state or changed source/engine/selection refuses publication;
  qualified text and required corrections are never silently truncated.
- Review records describe actual inspected scope; extraction or a skim cannot
  create a substantive review or negative finding.
- Contribution acknowledgment, persisted disposition and retrieval availability
  are distinguishable; communication records retain task/source/code references.
- Clean installation and source packaging still contain no program content,
  operational records, credentials, private overrides or populated databases.
- A recorded retrieval diagnostic cannot be counted as a review, returned by a
  read tool, surfaced in search, or used as support for a qualified answer.
- Diagnostic capture never writes query text, prompts, model output, source
  excerpts or credentials; a record carrying any of them is refused rather
  than truncated.
