# The Librarian development and ingestion roadmap

**Audience:** maintainers, operators and their agents.
**Updated:** October 26, 2026.
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

The October 3 data-empty extraction includes native catalog/review stores,
verified local backups, configurable source restrictions, metadata inventory,
Gemini TXT/ODT extraction, explicit retrieval selection, exact/compact/batched
reads and read-only MCP. Empty initialization and source-only packaging remain
requirements, not temporary demo limitations.

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
See [decision 0001](docs/decisions/0001-project-identity.md).

### Recorded design decisions - October 21

Deployment and storage direction is now recorded as numbered decisions under
[docs/decisions](docs/decisions/README.md): an updateable distribution with a
pinned, preflight-verified runtime; a store-compatibility contract that never
migrates authority data implicitly and always rebuilds the derived index; one
Librarian per project with its own data root; inbox-first source onboarding
with read-only remote scopes; and an open evaluation that declines a graph
database server in favor of a rebuildable derived projection if traversal is
ever needed. These are design commitments and inventory, not implemented
capability. The configuration work they depend on - per-instance store homes,
policy that follows the data root, and index identity that does not bind an
absolute host path - is not built.

Decision 0006 is updated with a generic report that its first precondition -
a recorded real question the existing reads, search and selection could not
answer - is **not yet satisfied**, and with October 22 license, release and
packaging facts for the two obvious embedded Cypher candidates. Neither is
adoptable as written. Graph work remains "later, only if justified".

An October 23 addition separates the two questions that "should we add a graph"
keeps merging: where traversable relationships are **stored and queried**,
versus where candidate edges **come from**. A derived projection can only
project what the authority stores record, so intra-artifact structure - symbols,
calls, imports, module groupings - is unavailable to it by construction. Closing
that gap means ingesting a third-party extractor's output, which is an ingestion
question under decision 0005 and needs no graph decision. Such output is a
receipted, attributed source, never a relationship layer this software vouches
for; model-synthesized node classes stay inference, a cited-but-absent companion
artifact is an unresolved source flag, and invoking an extractor locally is a
decision-0002 dependency decision. Precondition 1 is unaffected: a missing
upstream artifact is a delivery gap, not a failed question. A new fifth
precondition requires every projected edge to have an identified origin - an
owned authority store or a named ingested artifact.

The same update renames the review-event schema `$id` to
`urn:the-librarian:review-events:1`. This changed the store contract hash and
required re-initializing the local review store, which was only acceptable
because it was empty. Authority strings remain unchanged; application IDs
were subsequently renamed with explicit superseded-ID refusal, as recorded in
[decision 0008](docs/decisions/0008-proof-of-concept-closure.md).

It also adds an observation capability that is unlike the ingestion
sources: **body-free retrieval diagnostics** covering coverage gaps, plus bare
search and read counts, specified under
[Retrieval diagnostics from agent execution](#retrieval-diagnostics-from-agent-execution).
An earlier, broader proposal to ingest agent execution traces as durable agent
memory is declined in [decision 0007](docs/decisions/0007-execution-traces.md),
which also withdraws the dead-weight requirement rather than deferring it:
pruning selection by traffic is not a goal, and attributing reads would turn
the log into a usage profile. The coverage-gap half of the kept proposal is
implemented: an empty search returns an absence classification, and an opt-in
body-free log records that classification without the query.

### Docling integration design - October 20

The owner requested Docling ingestion in both this toolkit and the originating
corpus installation. The [approved design](docs/superpowers/specs/2026-10-20-docling-ingestion-design.md)
specifies a shared optional local-only PDF/PNG/JPEG adapter, separate from
historical extractor wrappers, with independent repeat verification and
explicit catalog registration. It does not add automatic review or retrieval
selection. The [implementation plan](docs/superpowers/plans/2026-10-20-docling-ingestion.md)
is approved for subagent-driven, Kata-tracked, in-place implementation with
roborev pre-commit review. The [October 22 completion plan](docs/superpowers/plans/2026-10-22-ingestion-completion.md)
supersedes Tasks 1-4 and withdraws the second-installation obligation. The OCR
asset blocker is resolved, with recorded repeatable real synthetic-fixture
acceptance. The optional lifecycle, policy bridge, CLI, clean-copy regressions,
store-identity changes and operating guide are committed and reviewed.
October 26 closure verified the real CLI lifecycle:
prepare twice, verify, register and idempotent retry, with independent offline
conversions and identical complete rendition bytes in a temporary catalog.
The installed suite, cloned CLI/policy boundary, packaging and schema checks pass;
whole-change review binds the exact delivered diff and passes.
Approved toolkit-only ingestion work is complete. Renditions remain deferred:
no automatic review, intake advancement, retrieval selection or indexing, and
no member selection or additional formats are added. Real-document OCR accuracy
remains unmeasured; package checks do not grant publication approval.

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
| Broader extraction | Gemini TXT and ODT only | Port ordinary TXT, DOCX/XLSX/PPTX and embedded-text PDF; preserve format limitations, dependency pins and source/engine-bound versions |
| Archive-member and large-workbook handling | No explicit retained-member extraction selector or row-chunk engine | Port member-policy/ancestor checks and versioned XLSX chunking without automatic recursive expansion or legacy-run rewriting |
| Bounded ingestion cohorts | No cohort driver or operation-local catalog snapshot API | Adapt `cohort.py` and `ReceiptMetadataSnapshot`; retain source/state checks, serialized writes, saved requests, explicit partial failures and backups |
| Read-only remote discovery | No remote connector; local explicit-root inventory exists | Generalize `drive-intake` planning and verified retained-byte bindings; remove installation-specific account/drive counts, paths and configuration |
| Continuous copy and intake | Not implemented in either inspected installation | Design and implement the durable pipeline below; a read-only mount is not a local copy or ingestion service |
| MCP audit and body-free metrics | Opt-in body-free retrieval diagnostics implemented for search absence and a bare read count (`tools/diagnostics/retrieval_log.py`); no general operation audit, no cohort metrics | Adapt the remaining opt-in audit/`metrics.py` and cohort metrics; private modes, explicit logging failures and no telemetry-as-job-authority. Dead weight is withdrawn, not pending, per [Retrieval diagnostics from agent execution](#retrieval-diagnostics-from-agent-execution) |
| Retrieval refinements | Exact/compact/batched reads exist; later source-budget batch plans and reversible discovery archives absent | Port deterministic complete-packet sizing guidance and explicit archive inclusion without losing qualifiers or silently retrying |
| Reader diagnostics and regressions | Earlier native event reader retained | Review later fixes, including required/allowed output sizes; preserve exact payloads and outer response-budget caveats |
| Explicit relationship traversal | No derived graph | Evaluate a generic `context-graph` adaptation after core field use; preserve canonical evidence routes and stale-read refusal. Evaluation recorded in [decision 0006](docs/decisions/0006-relationship-traversal.md), whose precondition 1 is reported unsatisfied as of October 21. Upstream edge extraction is separated as an ingestion question on October 23; ingested extractor output is a receipted, attributed source, not a relationship layer |
| Declared relation vocabulary | `config/ontology.json` declares the relation terms, per-relation endpoint kinds and disjoint pairs the catalog validator enforces; the vocabulary is bound to the store contract and a changed vocabulary is refused pending explicit migration | Implemented October 28 under [decision 0009](docs/decisions/0009-declared-ontology-and-entailment.md). Declared `symmetric`/`transitive` properties are recorded and never applied: no entailment is computed anywhere, and `basis` keeps entailed edges out of every authority store. Computing labelled, premise-carrying entailment remains gated on decision 0006's preconditions and the projection that does not exist |
| Agent execution traces | No trace endpoint, instructions or schema; nothing records what an agent did with returned context. Search absence is classified and optionally logged body-free, alongside a bare read count | Body-free retrieval diagnostics only - coverage gaps (implemented); dead weight withdrawn; no retrieval-quality figure is derived, because the available counts cannot support one. Traces as durable agent memory are declined in [decision 0007](docs/decisions/0007-execution-traces.md); agent findings route through the contribution contract instead |
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

Preserve verified pre/post-write backups and retained originals. Never add
automatic retention deletion or rebuild authoritative stores as job recovery.
Define pause, clean shutdown and explicit resume before enabling scheduling.

## Retrieval diagnostics from agent execution

**Status: Partly implemented.** Search absence classification and an opt-in
body-free diagnostics log are implemented and tested; see
[What is implemented](#what-is-implemented) below. Dead-weight and
retrieval-quality diagnostics are still proposed and unimplemented. This
supersedes an earlier, broader proposal to ingest agent execution traces as
durable memory. That broader version is declined; the reasoning is recorded in
[decision 0007](docs/decisions/0007-execution-traces.md).

### What is wanted, and what is declined

This corpus exists to supply other agents with business context gathered from
users, Google Drive and Confluence. The question is whether recording what
those agents did with returned context pays its way.

**Declined: traces as agent memory.** Letting an agent read its own prior
conclusions back as context inverts the corpus's purpose. The value here is
that every answer is traceable to a Drive file or a Confluence page with a
locator, a hash and a review record. An agent's recollection has no author, no
version and no upstream owner, and it is wrong the moment the underlying page
changes. Stored at the same retrieval surface, unverified recollection
competes with sourced material - and the model cannot reliably tell them
apart. A corpus whose authority is provenance should not also store content
whose only provenance is that an agent once said it.

An agent finding that genuinely deserves to persist already has a route: the
[contribution and disposition contract](agents/contributions.md), which
requires evidence, basis, affected context, handling and attribution, and
which ends in review. That path is deliberately slower than writing a trace.
The friction is the feature.

**Kept: body-free retrieval diagnostics.** What this corpus cannot currently
see is its own performance. One question matters and it was not answerable
before this work:

- **Coverage gaps.** Which questions produced no usable result? That is the
  prioritized ingestion backlog, and it is the highest-value signal available.
  Right now a question the corpus cannot answer leaves no trace at all.

Search and read counts are also recorded, as bare facts about observed traffic.
No retrieval-quality figure is derived from them.

**Not reported: retrieval quality.** Reads per answered search looks like a
quality signal and is not one. It cannot distinguish an unconvincing search
excerpt from an agent that read enough from the search result and never needed
the full record - two cases calling for opposite responses. The counts are
kept; the ratio is not published until something can tell those cases apart.
[Decision 0007](docs/decisions/0007-execution-traces.md) records why.

**Withdrawn: dead weight.** An earlier version of this section asked which
selected documents are never returned or never read, so selection could be
pruned. That requirement is withdrawn in
[decision 0007](docs/decisions/0007-execution-traces.md). Less data is not
assumed to be better: removing a selected document is only an improvement if
keeping it demonstrably costs something, and nothing here demonstrates that,
while a wrongly pruned document is unavailable the first time it is needed and
leaves a gap indistinguishable from a source never collected. The measurement
is also weakest where it would be used - at observed traffic volumes "never
returned" and "not yet asked about" are the same observation - and it is the
only proposed signal that requires attributing reads, which would
make the log a usage profile of the corpus and of the asking agent.

These are observations about the corpus's behavior, not claims about the
world, and they answer operator questions rather than feeding agent context.

### Shape

Follow the existing body-free metrics precedent rather than inventing a trace
format. The originating installation has a 247-line `metrics.py` that already
enforces the hard part: a closed field set that rejects bodies and queries
outright, a fixed operation vocabulary, exception class names instead of error
messages, a per-record byte budget, private file modes and locked appends. It
is explicitly "never a catalog or review authority." That is the model.

Required, beyond what `metrics.py` already does:

- **Record the question's outcome, not the question.** A zero-result or
  unusable-result event needs enough shape to drive ingestion priority without
  storing the query text, which carries the asking agent's task content.
  Finding a formulation that is useful and still body-free is the real design
  problem and is unsolved.
- **Record use, not just return.** Not shipped as a quality measure. Reads are
  counted as bare events with no locator; no ratio is derived, because the
  observable counts cannot separate a weak search result from a question the
  search result already answered. Distinguishing returned from actually used
  would require cooperation from the calling agent and is not implemented.
- **Reference records by locator, hash and state token.** Deliberately not
  done. Recording a locator is what would turn the log into a usage profile;
  see the withdrawn dead-weight signal above.

### Boundaries

- **Diagnostics are never retrieval context.** They are not indexed, not
  searchable alongside corpus records, and not returned by read tools. They
  answer operator questions about the corpus, and nothing else.
- **Not an authority.** Not in the catalog store, not in the review-event
  store, never countable as a review, never support for a qualified answer.
- **Body-free is a hard constraint, not a default.** Queries, prompts, model
  output and source excerpts are all excluded. This is what keeps the log from
  becoming an unclassified copy of restricted source text and the likeliest
  place for a credential to land. If a diagnostic needs a body to be useful,
  it does not ship.
- **Bounded and prunable from the first version.** Diagnostics accumulate with
  no upstream owner, so they need a stated size and retention policy with
  deliberate verified pruning. The no-automatic-deletion rule protects
  authority stores and retained originals; a diagnostic log needs its own
  answer rather than inheriting unbounded growth.
- **Opt-in, per-instance, and explicit about failure.** Off by default. A
  logging failure is reported, never silently swallowed, and never becomes job
  authority.
- **No silent learning.** Nothing derived from diagnostics changes selection,
  ranking or qualification automatically. A gap report is input to an operator
  decision about what to ingest next. Any automatic use is a separate,
  versioned, tested policy with its own decision record.
- **The distribution stays empty.** Diagnostics are instance data; no log
  content or example session ships in the package.

### Classifying an unanswered question without storing it

The hard part of the coverage-gap signal is that "this question had no usable
answer" seems to require the question, and the question carries the asking
agent's task content. It does not, because the index already holds enough to
classify the failure.

The reader normalizes every search into literal terms and conjoins them, so a
zero-result search can be classified by probing each term against the
full-text index on the same read-only connection, before the result is
returned. Three outcomes are distinguishable:

| Classification | Meaning | Action it implies |
|---|---|---|
| `absent-corpus` | No query term appears anywhere in the selected corpus | Ingestion gap: this subject matter has never been collected |
| `absent-partial` | Some terms are known, others are absent from the vocabulary | Partial coverage: the count of unknown terms is the priority signal |
| `present-uncombined` | Every term exists, but no section contains them together | Findability or selection problem, not a collection problem |

The recorded fields are the classification, the number of query terms, the
number absent from the index vocabulary, and the number of sections matching
any single term. No term, no query string and no excerpt is stored. This is
the distinction the roadmap most needs - "we never collected this" versus "we
have it and search could not surface it" - and it survives the body-free rule
intact.

The mechanism requires no schema change: single-term `MATCH` probes run on the
existing read-only connection, so there is no new table, no index schema bump,
no engine-fingerprint change and no rebuild. It runs on the server side of the
adapter and needs no cooperation from the calling agent, so the signal is
observed rather than self-reported.

What this does not solve is recurrence: without storing terms, two different
agents asking the same unanswerable question are two anonymous counts rather
than one ranked gap. Hashing the normalized term set would cluster them, but
the index vocabulary is itself the dictionary needed to reverse such a hash
for short term sets, so that is not a privacy-preserving shortcut and is left
open. A counted classification histogram is weaker than a ranked list of
missing subjects and is still decision-grade: a rising `absent-corpus` count
means connect another source, while a rising `present-uncombined` count means
fix retrieval or selection.

### What is implemented

The coverage-gap signal described above now exists. Two pieces:

**Absence classification in the reader.** `context_index.search` returns an
`absence` object only when a search produced no hits. It contains the
classification, the number of query terms, the number absent from the index,
the number of sections matching any single term, whether the scope was the
whole selection or one path, and a caveat stating that the report describes
what is indexed and is never evidence that a fact does not exist. The
classification is computed with single-term `MATCH` probes on the same
read-only connection, so there is no new table, no schema bump, no
engine-fingerprint change beyond the reader's own content hash, and no
rebuild. The object never contains a query term, a locator or an excerpt, and
a successful search carries no `absence` key at all.

**An opt-in body-free diagnostics log.** `tools/diagnostics/retrieval_log.py`
is a standalone writer and reporter, off unless the operator passes
`--diagnostics-log <path>.jsonl` to the MCP adapter. It follows the
`metrics.py` precedent: a closed field set, a fixed operation vocabulary
(`search` and an unattributed `read`), exception class names instead of
messages, a per-record byte
budget, a log byte budget, private file modes and locked appends. Beyond that
precedent it enforces internal consistency - a classification must agree with
its own term counts, an empty outcome cannot report hits, an error type cannot
accompany a successful outcome, and a read record cannot carry a count,
classification or scope - so a malformed or over-reporting record is
refused rather than stored. `search` is recorded in full and `get`/`get-many`
only as a bare count; `status`, `catalog` and `review_events` write nothing. A
logging failure raises instead of returning the result, so diagnostics cannot
silently degrade into a no-op.

`retrieval_log.py report` summarizes the separation the roadmap wanted: a
collection-gap rate from `absent-corpus` against a findability-gap rate from
`present-uncombined`, with raw search and read counts and the limits of the
signal stated in the output itself - including that a read count is not a basis
for pruning selection and that no search-to-read ratio is
reported. `retrieval_log.py prune --keep N` is explicit, never
automatic, and reports the before and after counts. Nothing derived from the
log changes selection, ranking or qualification.

What is deliberately absent: any attribution of a read to a document, and
therefore any dead-weight signal; and any derived retrieval-quality figure,
because the available counts cannot support one. What is still not implemented:
clustering of recurring gaps, and any distinction between a record being
returned and a record being used.

### Open questions

- Whether recurrence of the same unanswerable question can be clustered
  without storing or reversibly hashing its terms. Left open above.
- Whether use-versus-return can be observed at all from a stdio adapter beyond
  a bare read count, or whether it requires cooperation from the calling agent -
  which would make the signal self-reported and weaker.
- Whether gap reporting belongs in this log or in the existing intake
  tracking, which already owns ingestion backlog.


## Delivery sequence and completion gates

| Phase | Scope | Completion gate |
|---|---|---|
| A: manual collaboration | Existing qualified retrieval plus the three role contracts and feedback route | Actual authorized Jira input becomes local Kata tasks and an implemented/reviewed change; a supported contribution receives a persisted disposition |
| B: reusable parity | Prioritize extraction, cohort/snapshot support, metrics and retrieval fixes from the gap table | Generic tests and clean-root packaging pass; version/fidelity contracts and operational limits documented |
| C: local continuous inbox | One allowlisted local inbox; scheduled discovery/copy/register/extract; explicit review/publication boundaries | Restart, duplicate, mutation, failure and committed-retry cases below pass without duplicate authoritative records |
| D: remote discovery/copy | One approved read-only provider/folder scope, then additional scopes | Complete paginated reconciliation, binary/native identity behavior and authorization failures demonstrated; no destructive synchronization |
| E: agent-driven maintenance | Bounded review/incorporation and approved publication policy | Evidence fidelity, independent-review triggers, recoverability and qualified-answer preservation shown under ongoing intake |
| F: retrieval diagnostics | Body-free coverage-gap observation plus bare search and read counts; dead weight is withdrawn, not pending, and no retrieval-quality figure is derived | Diagnostics are written and read without appearing at any retrieval surface or being countable as reviews; no query text, prompt, model output, source excerpt or locator is ever stored; retention is bounded and pruning is verified; no retrieval behavior changes automatically from diagnostic content. Coverage-gap classification, the bare read count and the opt-in log satisfy this gate |
| Later, only if justified | Graph, richer discovery, hosted/multi-user services | Demonstrated use case and explicit handling, authorization, concurrency and recovery design; not prerequisites for the local demo. Graph preconditions are recorded in [decision 0006](docs/decisions/0006-relationship-traversal.md) |

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
