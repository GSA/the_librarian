# The Librarian development and ingestion roadmap

**Audience:** maintainers, operators and their agents.
**Updated:** November 20, 2026.
**Status:** capability inventory and proposed work, not an implementation,
permission grant, delivery commitment or program-readiness register.

This is the canonical generic roadmap. Keep private source inventories,
access decisions, tickets, job runs, evidence and observations in the program
instance. No customer content is needed to maintain or distribute this plan.

## How to read this roadmap

Every item carries one status label, checked against the code and tests in this
repository:

- **Delivered**: implemented and covered by tests; how it works today is in the
  [current-state design](docs/design/current-state.md) and
  [intake contract](context/intake.md).
- **Partial**: some of the item is implemented; the remainder is stated.
- **Open**: proposed, not implemented.

Delivered work is listed under [Delivered](#delivered) and is not repeated as
planned work. Labels describe the toolkit, not whether a program instance has
exercised it on real data.

## Goal

Discover useful context across an authorized, initially unorganized document
collection; maintain attributed knowledge; support Jira-to-Kata refinement
and engineering; and return supported discoveries to that knowledge base.
The operator should not need to identify relevant documents before asking
a question or refining a ticket.

## Delivered

### Data-empty toolkit baseline - October 3

**Delivered.** Native catalog/review stores, verified local backups,
configurable source restrictions, metadata inventory, strict ordinary UTF-8
text, Gemini TXT and ODT extraction, exact/compact/batched reads and read-only
MCP. Empty initialization and source-only packaging remain requirements, not
temporary demo limitations.

### Role contracts - October 19

**Delivered** (instructions only). The project is named **The Librarian** and
ships the [Librarian](agents/librarian.md),
[ticket/refinement](agents/refiner.md), [Engineer](agents/coder.md) and
[contribution/disposition](agents/contributions.md) contracts. These are
instructions, not an agent runner, task importer or automatic inbox; that
orchestration is **Open** (see the reuse-gap table).

### Identity rename - October 21

**Delivered.** The checkout, package, module, commands and MCP protocol name
are all The Librarian, with no compatibility aliases. Record syntax is
unchanged. The later ingestion completion superseded the unchanged-application-ID
decision: catalog `LCAT`, review `LREV`, and derived index `LCTX` explicitly
refuse `FCAT`, `FREV`, and `FCTX`. Authority stores need explicit migration or
deliberate empty-store re-initialization; the derived index can be rebuilt from
authority data. The review-event schema `$id` became
`urn:the-librarian:review-events:1`, which changed the store contract hash and
required re-initializing the then-empty local review store. See the
[current-state design](docs/design/current-state.md).

### Design baseline - October 21 to November 12

**Delivered.** Identity, store compatibility, instance topology, backups,
validation, concurrency and observation are described as they work today in the
[current-state design](docs/design/current-state.md), with the test behind each
claim. The numbered decision records that used to hold this are in git history. The
configuration work they depended on (per-instance store homes, policy that follows
the data root, index identity that does not bind an absolute host path) is
implemented. A fully pinned runtime (lock file, image, installer), a per-instance
version record and a per-release compatibility note were considered and dropped
(Kata 7jkt): measured dependency drift between instances has caused no incident, and
the stores refuse a mismatched contract themselves.

### Docling integration - October 20-26

**Delivered.** The optional offline PDF/PNG/JPEG Docling extraction lifecycle (`tools/extraction/docling_extract.py`, `docling_adapter.py`, `docling_bridge.py`, `docling_worker.py`, and `tools/extraction/README.md`):
- Prepare twice with independent offline conversions, verify identical rendition bytes, and register deferred renditions in the catalog with idempotent retry.
- Clean-copy tests (`tests/test_packaging.py`, `tests/test_workspace.py`, `tests/test_docling.py`, `tests/test_worker_contracts.py`) verify packaging, policy isolation, and store-identity boundaries.
- Qualified retrieval can explicitly select exact-rendition, substantively reviewed Docling text and table records, with retained-byte, JSON-pointer, and page provenance. Unsupported content collections fail closed.
- Parser success alone never qualifies content. Real-document OCR accuracy remains unmeasured.

### Operator-run intake pipeline

**Delivered**, run by an operator; nothing is fetched or scheduled. Contract:
[context/intake.md](context/intake.md); design:
[current-state design](docs/design/current-state.md#3-intake-and-review).
- Durable intake ledger (`tools.intake.cli`): exact staged requests, named-stage
  native writer recovery, bounded retries, dead-letter replay, pinned
  publication and supplied transport acknowledgment (`tests/test_intake.py`,
  `tests/test_intake_adapters.py`).
- Manual upload (`submit-file`) and marked local drop (`drop-scan`,
  `drop-submit`) (`tests/test_intake_upload.py`).
- Read-only cohort intake (`tools.intake.cohort`): `classify`, batched `ingest`,
  model-assisted standing-policy `qualify`, standalone `publish`,
  `recover-identity` (`tests/test_intake_cohort.py`, `tests/test_intake_batch.py`,
  `tests/test_standing.py`, `tests/test_model_review.py`).
- MHTML/Confluence `.doc` extraction (`tools/extraction/mhtml.py`,
  `tests/test_mhtml.py`).
- Read-only, body-free progress view (`progress`, `progress-serve`)
  (`tests/test_intake_progress.py`).

### Retrieval diagnostics and MCP audit log

**Partial.** Body-free retrieval diagnostics (search absence classification and
a bare read count) and the opt-in full-text MCP audit log are delivered; agent
execution traces were not built. Details under
[Retrieval diagnostics from agent execution](#retrieval-diagnostics-from-agent-execution).

### Retrieval-quality evaluation harness - November 20

**Delivered.** `python -m tools.evals` generates seeded known-item and absence
cases, runs instance-local cases against a fresh index into a body-free scorecard
and compares scorecards with regression gating (`tests/test_evals.py`). Details
under [Retrieval-quality evaluation](#retrieval-quality-evaluation).

## Reuse gaps identified on October 19

Status labels in this table reflect the repository as of the **Updated** date
above; the "State" column was first written on October 19. Comparison covers the extracted repository and the originating installation's
current tooling/guides. It is a capability inventory, not exhaustive code
parity, a correctness audit or permission to copy installation data.
The October 19 comparison itself included no runtime backports.
Source component names below identify porting candidates, not files promised
to exist in this distribution.

| Capability | Status | State in this repository | Required action |
|---|---|---|---|
| Three-agent collaboration and returned findings | Open | Generic contracts captured; no orchestration or Jira/Kata integration | Exercise explicit handoffs, original-criterion preservation, correlation and returned disposition before adding a runner |
| Broader extraction | Partial | Strict ordinary UTF-8 text with explicit line retrieval, Gemini TXT, MHTML (including Confluence `.doc` exports) and ODT; optional offline Docling PDF/image conversion; cohort intake reads PDFs page by page (each page's embedded text layer, else Docling OCR of that page, retried once, failed pages kept as placeholders) and images (Docling OCR) when the runtime is provisioned; stdlib DOCX/XLSX/PPTX/VSDX/RTF conversion to derived Markdown, legacy Office only with an optional LibreOffice | Other legacy binaries (`.mpp`, OpenDocument spreadsheets) and non-UTF-8 text; preserve format limitations, dependency pins and source/engine-bound versions |
| Archive-member and large-workbook handling | Partial | Explicit `cohort expand-archives` expands zip/tar members by hash into instance storage with name, link and size checks, and `delete-archive-original` deletes a verified, fully registered original (Kata 12sc); no row-chunk engine or member-selection policy | Add versioned XLSX chunking and member-policy selection without automatic recursive expansion or legacy-run rewriting |
| Bounded ingestion cohorts | Partial | Cohort driver delivered (`tools.intake.cohort`: classify, batched ingest, qualify, publish) with saved ledger requests, serialized writes, explicit per-file outcomes and daily backups; no `ReceiptMetadataSnapshot`-style operation-local catalog snapshot API | Port an operation-local catalog snapshot API only if cohort cost measurements need it |
| Read-only remote discovery | Open | No remote connector; local explicit-root inventory, marked local drop and read-only local inbox classification exist | Generalize `drive-intake` planning and verified retained-byte bindings; remove installation-specific account/drive counts, paths and configuration |
| Continuous copy and intake | Partial | Private exact-request intake ledger, explicit native writer stages, bounded attempts/DLQ/replay, acknowledgment recording, explicit manual file upload / marked local-drop intake and operator-run read-only cohort intake; no acquisition or scheduler | Add separately authorized connectors, scheduling and return transport; a read-only mount is not a local copy or ingestion service |
| MCP audit and body-free metrics | Partial | Opt-in body-free retrieval diagnostics implemented for search absence and a bare read count (`tools/diagnostics/retrieval_log.py`); opt-in full-text per-agent MCP audit log (`--audit-log`, [design](docs/design/current-state.md#6-observation-audit-log-and-diagnostics)); body-free intake progress view; no cohort metrics in the MCP diagnostics | Add cohort metrics if wanted; keep private modes, explicit logging failures and no telemetry-as-job-authority. The audit log is implemented (`tests/test_mcp_audit.py`); see [Retrieval diagnostics from agent execution](#retrieval-diagnostics-from-agent-execution) |
| Low-confidence information | Partial | `confidence-v1` labels with stable reasons on search hits, `get`/`get_many` packets and `status` counts; reviewer deferrals qualify as low confidence by operator opt-in (`--deferred qualify-low-confidence`, `cohort recover-deferred`); `cohort rereview` re-reviews older-policy and insufficient-scope standing reviews as corrections (`--include-older-rejected` after a criteria change); policy-pinned review criteria label embedded instructions and sparse records low confidence and carry earlier qualified reviews; seams for converter/OCR kinds (`RENDITION_REASONS`), agent-generated origins (upload `agent_generated`); agent-written context trees ingest as a cohort with a declared origin (`cohort ... --agent-origin`, git commit/date provenance, `origin` on search hits); append-only claim corrections (`tools.review_events.corrections`: correct, dispute, confirm, retract with a cited document, transcript or answered question) mark claims `open-correction` at read time ([design](docs/design/current-state.md#3-intake-and-review)) | Extracting corrections from transcripts automatically, an MCP correction writer, catalog-level counts |
| Retrieval refinements | Open | Exact/compact/batched reads exist; later source-budget batch plans and reversible discovery archives absent | Port deterministic complete-packet sizing guidance and explicit archive inclusion without losing qualifiers or silently retrying |
| Reader diagnostics and regressions | Delivered | The event reader's output-limit refusal reports the required and selected character counts without source text and notes that an outer client/tool limit may still apply (`tests/test_review_event_output_limit.py`, Kata 6sc9) | None; keep exact payloads and the outer response-budget caveat |
| Explicit relationship traversal | Open | No derived graph | Evaluate a generic `context-graph` adaptation after core field use; preserve canonical evidence routes and stale-read refusal. Evaluation recorded in the [design](docs/design/current-state.md#7-relationship-traversal) (no real failed question has been recorded) |
| Agent execution traces | Partial | No trace endpoint, instructions or schema; nothing records what an agent did with returned context. Search absence is classified and optionally logged body-free, alongside a bare read count | Body-free coverage-gap diagnostics (implemented); reads are not attributed to documents and no retrieval-quality figure is derived from diagnostics. Retrieval quality is measured separately, against instance-local cases, by the evaluation harness (see [Retrieval-quality evaluation](#retrieval-quality-evaluation)). Agent findings route through the contribution contract; see the [design](docs/design/current-state.md#6-observation-audit-log-and-diagnostics) |
| Discovery-card experiments | Open (lesson only) | Not shipped | Retain the lesson: skims can miss relevant material; evaluate candidate recall with deferred controls, never use cards as proof of absence |
| Operating guidance | Partial | Generic role/feedback contracts and the intake operator contract included; private corpus procedures intentionally omitted | Keep task-triggered loading and source fidelity/authority boundaries; add generic format/recovery guides alongside each future port |

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

**Status: Partial.** Stages 2 to 6 run today as operator-invoked commands
(see [Operator-run intake pipeline](#operator-run-intake-pipeline)); nothing
discovers remote content, runs on a schedule or returns outcomes over a
transport. Each stage below states what exists.

### 1. Discover documents and changes

**Partial.** Explicit local discovery exists: `tools.catalog.inventory`,
`drop-scan` of a marked drop and cohort `classify` of a read-only local inbox.
Remote scopes, scheduled reconciliation polls and a durable discovery cursor are
**Open**.

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

**Partial.** Upload, drop and cohort ingest publish hash-checked bytes and a
provenance sidecar under the instance's original root without overwriting an
earlier submission; changed bytes become linked corrections. Remote acquisition
is **Open**.

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

**Partial.** Registration through the catalog writer and format-routed
extraction run as intake stages for text, Gemini TXT, MHTML and ODT (cohort
ingest) and PDF/images (cohort ingest through the provisioned Docling runtime, or staged Docling requests). Further formats are **Open**
(see the reuse-gap table).

Register real receipt/source/artifact metadata through the supported catalog
writer. A copied file is not a registered receipt until its commit is confirmed.
Use a private operational journal for durable work and retries, **not** a
parallel metadata, review or intake-status authority.

Run bounded, format-routed extraction only after registration and handling
checks. Preserve source hashes, exact locators, parser versions, omitted scope
and warnings. Unsupported, encrypted, malformed or over-budget files stay
explicitly pending. No empty success fallback, macro execution or automatic
recursive archive expansion (archives expand one level per explicit run). New adapters need fidelity/recovery tests before scheduling.

### 4. Review and incorporate context

**Partial.** Model-assisted standing-policy review of exact renditions
(`qualify`, `tools/intake/model_review.py`) records attributed, append-only
review events. Synthesizing reviewed knowledge into maintained documents
remains agent or operator work under the [Librarian contract](agents/librarian.md).

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

**Partial.** Standing-policy selection and publication (`standing-select`,
`standing-publish`, cohort `publish`) are delivered; returning outcomes is
limited to recording a supplied acknowledgment (`await-ack`/`acknowledge`).
Return transport is **Open**.

Apply explicit versioned source-selection/qualification policy; copying or
registering a document must not automatically make it qualified context.
Build the derived index atomically, validate freshness and exact packets, and
make approved updated knowledge available only after successful publication.
The only automated selection is standing-policy selection, which requires a
pinned, authorized standing policy and a qualifying review of the exact
rendition ([intake contract](context/intake.md)); any other selection is a
deliberate edit of the selection manifest.

Return receipt/disposition to an originating contribution or task when one
exists. Mark a source consumed only under the instance's verified incorporation/
disposition rules, not because it downloaded or extracted successfully.
Record refresh failures and unavailable return routes explicitly.

### 6. Operate, retry and reconcile

**Partial.** The ledger, retries, dead-letter replay and the body-free progress
view are delivered; a scheduler with pause, clean shutdown and resume is **Open**.

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

**Status: Partial.** Search absence classification and an opt-in
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
  ratio or retrieval-quality figure is derived from the log; the counts cannot separate an
  unconvincing excerpt from an agent that needed nothing more
  (`test_reads_are_counted_but_never_attributed`,
  `test_report_counts_reads_without_deriving_a_ratio`).
- **Boundaries.** The log is never indexed, searched or returned by a read tool,
  is never a review or catalog authority, and nothing derived from it changes
  selection, ranking or qualification. No log content ships in the package.

### Retrieval-quality evaluation

**Delivered.** `python -m tools.evals` (`generate`, `run`, `compare`) scores an
instance's published index against ignored local cases
(`config/evals.local.json`): authored or seeded known-item and absence cases with
expected documents or locators within `k`, must-not documents, expected
confidence level and absence classification. A run checks freshness once before
and once after the batch and refuses a stale index; its scorecard is body-free and
records hit@1, hit@k, recall@k, MRR, must-not violations, absence accuracy,
confidence-match rate and the index, policy, standing-policy, cases and toolkit
provenance. `compare` gates on metric drops and per-case regressions. Contract and
procedure: [context/retrieval-evals.md](context/retrieval-evals.md); behavior and
tests: the [design](docs/design/current-state.md#6-observation-audit-log-and-diagnostics).
Remaining: real-question authored cases per instance, and judging answer
correctness rather than findability.

### Open questions

- Whether recurrence of the same unanswerable question can be clustered without
  storing or reversibly hashing its terms.
- Whether a record being returned versus used can be observed from a stdio
  adapter beyond a bare read count, or needs the calling agent's cooperation.
- Whether gap reporting belongs in this log or in the existing intake tracking,
  which already owns ingestion backlog.


## Delivery sequence and completion gates

| Phase | Status | Scope | Completion gate |
|---|---|---|---|
| A: manual collaboration | Partial: contracts delivered; gate evidence is kept in program instances, not here | Existing qualified retrieval plus the three role contracts and feedback route | Actual authorized Jira input becomes local Kata tasks and an implemented/reviewed change; a supported contribution receives a persisted disposition |
| B: reusable parity | Partial: MHTML, cohort driver and MCP audit delivered; remaining gap-table items open | Prioritize extraction, cohort/snapshot support, metrics and retrieval fixes from the gap table | Generic tests and clean-root packaging pass; version/fidelity contracts and operational limits documented |
| C: local continuous inbox | Partial: operator-run cohort intake of one local inbox delivered; scheduling open | One allowlisted local inbox; scheduled discovery/copy/register/extract; explicit review/publication boundaries | Restart, duplicate, mutation, failure and committed-retry cases below pass without duplicate authoritative records |
| D: remote discovery/copy | Open | One approved read-only provider/folder scope, then additional scopes | Complete paginated reconciliation, binary/native identity behavior and authorization failures demonstrated; no destructive synchronization |
| E: agent-driven maintenance | Partial: standing-policy review and publication delivered; gate not demonstrated | Bounded review/incorporation and approved publication policy | Evidence fidelity, independent-review triggers, recoverability and qualified-answer preservation shown under ongoing intake |
| F: retrieval diagnostics | Delivered | Body-free coverage-gap observation plus bare search and read counts; reads are not attributed to documents and no retrieval-quality figure is derived from them (quality is measured against instance-local cases by `tools.evals`) | Diagnostics are written and read without appearing at any retrieval surface or being countable as reviews; no query text, prompt, model output, source excerpt or locator is ever stored; retention is bounded and pruning is verified; no retrieval behavior changes automatically from diagnostic content. Coverage-gap classification, the bare read count and the opt-in log satisfy this gate |
| Later, only if justified | Open | Graph, richer discovery, hosted/multi-user services | Demonstrated use case and explicit handling, authorization, concurrency and recovery design; not prerequisites for the local demo. Graph evaluation notes are in the [design](docs/design/current-state.md#7-relationship-traversal) |

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
