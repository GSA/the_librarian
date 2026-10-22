# Docling Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit, reproducible, local Docling OCR/layout extraction and catalog registration to the reusable toolkit and the originating corpus without replacing existing ingestion machinery.

**Architecture:** Share a conversion worker, lifecycle module and CLI as identical versioned files in both installations. A small local bridge adapts policy, catalog metadata and output locations. Run inference in a separate optional environment and network-isolated child process; preserve existing extractor wrapper bytes.

**Tech Stack:** Python 3.11+, Linux/POSIX namespaces, Docling 2.140.0, RapidOCR 3.10.0 with CPU ONNX Runtime 1.31.0, standard-library unittest and existing catalog APIs.

**Spec:** [Approved Docling ingestion design](../specs/2026-10-20-docling-ingestion-design.md).

**Status:** execution approved October 20, 16:19 EDT: subagent-driven, tracked in Kata, working in place, with roborev pre-commit review. The hook-composition decision was answered October 22 and both gates are active. Partial implementation remains blocked on valid OCR assets; real ingestion acceptance is incomplete.

## Global Constraints

- "Supported inputs are PDFs with embedded text, scanned pages or mixed content, and single-image PNG/JPEG files."
- "Do not change either installation's existing `extract.py`."
- "Conversion runs in a child process inside a Linux user/network namespace with network access disabled."
- "Use an explicit CPU-only standard-pipeline profile and explicit local OCR backend."
- "`verify` and `register` independently repeat conversion and compare the complete canonical rendition byte-for-byte."
- "The child remains deferred, not indexed."
- "No change to the existing cohort selection schema is included."
- "Proposed initial per-operation limits are 64 MiB input, 100 PDF pages, 32 million pixels per rasterized page/image, 64 MiB serialized rendition and 600 seconds wall time."
- Toolkit receipt originals only; corpus also accepts explicitly selected retained members and enforces ancestor exclusions.
- No remote inference, VLMs, picture descriptions, automatic review/indexing, source execution, source loss or authoritative-store schema changes.
- Keep the reusable package data-empty and preserve unrelated existing changes in both checkouts.

## Review Focus

- Mixed PDFs with legitimate blank pages must retain a complete page inventory without accepting failed pages as blank; Task 1 exercises both.
- A committed registration whose parent later changes must replay the saved request without overwriting newer metadata; Task 2 exercises it.
- Model assets replaced through links or during inference must not retain the old identity; Task 1 exercises resolution and mutation.
- Very large page dimensions must be rejected before expensive rasterization, not after allocating the full image; Task 1 exercises it.
- Policy tightened after module import or preparation must block the source before conversion; Tasks 2 and 3 exercise it.

## File Map

Common files, developed here and copied identically into the corpus:

- `tools/extraction/docling_worker.py`: fixed conversion profile, optional dependency/runtime inspection, model binding, bounded source inspection and actual Docling conversion.
- `tools/extraction/docling_adapter.py`: isolated process control, rendition identity/publication, repeated verification and idempotent catalog-registration lifecycle.
- `tools/extraction/docling_extract.py`: explicit prepare/verify/register CLI; optional dependencies never imported in the parent.
- `tools/extraction/requirements-docling.txt`: pinned Docling/OCR runtime, with a documented matching CPU PyTorch installation.

Installation-specific files:

- `tools/extraction/docling_bridge.py`: local source policy, catalog selection, handling boundary and registration-record adaptation.
- Toolkit `tests/test_docling.py`; corpus `tools/extraction/test_docling.py`: lifecycle and policy regressions using isolated temporary catalogs.
- `tools/extraction/test_docling_worker.py`: common worker/protocol tests and opt-in real conversion fixtures; copy identically.
- Toolkit `tools/extraction/README.md`: new generic operating guide. Corpus `tools/extraction/README.md`: scoped Docling addition.
- Toolkit `README.md`, `ROADMAP.md`, `release-files.json`; corpus `README.md`, prompt/maintenance history: accurate interface and completion documentation.

## Execution Setup

- [x] Owner selected subagent-driven execution using Kata, roborev pre-commit review and in-place work on the current branches. Preserve pre-existing modified/untracked files; they are saved changes, not evidence the owner personally edited them.
- [x] Owner chose in-place work. Private pre-implementation diffs/status snapshots are saved in the session files; do not assume HEAD includes the current toolkit baseline.
- [x] Created the plan-owned ignored `.superpowers/sdd/2026-10-20-docling-ingestion/` ledger and recorded both wrapper hashes and shared-interface rulings. This is execution evidence, not a second maintained project register.
- [x] Toolkit baseline `.venv/bin/python -m unittest discover -s tests -v`: 15 pass. Corpus baseline `tools/extraction/.venv/bin/python -m unittest discover -s tools/extraction -p 'test_*.py'`: 103 pass.
- [ ] Keep task checkpoints scoped. Do not automatically commit pre-existing modified files; any checkpoint commit must contain only verified task-owned changes and the required co-author trailer.

### Task 0: Kata Tracking and roborev Pre-commit Gate

**Owner addendum:** October 20, 16:19 EDT requires Kata and roborev pre-commit
review for this full technical feature. Both repositories now bind to the
separate local `librarian` Kata project; do not put this work in the engineering
platform's own backlog or generate a corpus TODO file.

**Files:** shared `.githooks/pre-commit`, `tools/review/roborev_precommit.py`,
`tools/review/install_hooks.py`, `tools/review/test_precommit.py`; a small
installation-specific review selection/configuration file if needed.

- [x] Track this feature and Tasks 0-4 in Kata with stable idempotency keys,
  explicit parent/dependency relationships and verified completion evidence.
  Delegate tracked child work without assigning tasks to people.
- [x] Write failing tests proving the hook reviews staged feature bytes only,
  excludes unstaged/untracked and handling-restricted corpus content, preserves
  existing hooks/configuration, and blocks on findings or unavailable review.
- [x] Implement a source-only private staged snapshot for roborev: the installed
  CLI supports `--dirty` but not `--staged`, so do not review the full live
  worktree. Use an explicit technical-feature allowlist and report excluded
  staged paths; do not imply those paths were reviewed. Retain exact staged
  diff identity and reject concurrent index changes.
- [x] Use the existing scoped roborev launcher and configured Pi/USAi path.
  Do not change providers, credentials, global hooks or daemon settings.
  Store machine-specific launcher paths in local Git configuration only.
- [x] Install a repository-local pre-commit gate in both installations,
  preserving/refusing conflicting existing hooks rather than overwriting them.
  Avoid recursive reviews when constructing the private snapshot.
- [x] Run focused failure/snapshot tests and a real review of public structural
  fixture code. Inspect stored verdict/exit status; a queued job is not a passed
  review. No automatic fix, push, publication or source processing follows.

**Completion:** both local Git hooks invoke roborev against exactly the selected
staged technical content; Kata remains the live feature/task record.

**Current gate:** code independently reviewed, including reproduced Git-attribute
binding fixes. Both installations are now activated. The owner's October 22
composition decision mirrors the toolkit layout: the corpus uses
`core.hooksPath=.githooks` with the concurrently installed managed roborev
`post-commit`/`post-rewrite`/`pre-push` copied in byte-for-byte beside this
feature's `pre-commit`. The managed private hook directory, its launcher
configuration and the daemon were not modified. Those three managed hooks
select each installation's own launcher path, so they are deliberately not
common files.

### Task 1: Reproducible Offline Conversion Worker

**Files:** create `docling_worker.py`, `requirements-docling.txt`, `test_docling_worker.py` in `tools/extraction/`.

**Interfaces:**
- `profile() -> dict`: fixed JSON-compatible profile: CPU, one inference thread, standard PDF pipeline, local RapidOCR/ONNX, OCR and accurate tables enabled, remote/plugins/enrichments disabled.
- `inspect_runtime(artifacts_path: Path) -> dict`: installed distribution/Python versions, complete fixed profile, relative asset inventory and its canonical hash.
- `convert(source: bytes, name: str, format_name: str, artifacts_path: Path) -> dict`: canonical payload with Docling document, source format, page inventory, confidence and warnings.
- Worker CLI consumes a private JSON job and writes one bounded JSON result file. Inspection is a distinct operation with no source argument; conversion never provisions assets.

- [ ] Write failing `test_fixed_profile_forbids_remote_and_automatic_backends`, `test_model_binding_detects_changed_bytes_and_outside_links`, `test_source_inspection_rejects_encryption_and_large_geometry`, `test_blank_pages_are_not_failed_pages`, `test_partial_or_empty_conversion_refuses`, and `test_payload_preserves_tables_provenance_and_unknown_confidence`. Assert observable outputs/rejections, not source-code strings.
- [ ] Run `python3 -m unittest discover -s tools/extraction -p 'test_docling_worker.py' -v`; verify failures identify the missing worker behavior.
- [ ] Implement the interfaces. Use canonical JSON with no NaN/Infinity, timings, temporary paths or generated image payloads. Preserve substantive Docling fields. Resolve model assets only within the selected root and recheck their hash manifest around inference.
- [ ] Add the pinned optional requirements. Use the official Docling slim distribution with the PDF, local-model, conversion, CLI and RapidOCR/ONNX extras needed by this profile, rather than unrelated audio/remote features. Pin resolved runtime versions; select a matching released CPU PyTorch/torchvision pair and document its separate official CPU-index installation.
- [ ] Create `tools/extraction/.venv-docling` only after the manifest exists. If dependency resolution fails, diagnose explicitly; do not alter the MCP environment or loosen pins silently. Provision only the required public model assets into ignored private local storage in a separate explicit step.
- [ ] Add real synthetic native/scanned/mixed PDFs, PNG and JPEG generation using installed runtime libraries and toolkit/public text. Pin assertions for a negation, `0123456789`, two-column sequence, a simple table's cell associations and original page/bounding-box references. Never store customer fixtures or weights in Git.
- [ ] Run the lightweight worker tests, then explicit real-conversion tests with the dedicated interpreter and assets directory. Repeat each canonical conversion independently and require byte equality. Stop for a contract decision if repeatability cannot be achieved.

**Completion:** the real CPU worker produces faithful source-located structural fixtures twice without network access; error and budget paths remain non-successful.

**Current gate:** the three worker files and optional pinned CPU runtime exist.
Official OCR provisioning returned invalid HTML twice; downloading stopped.
The worker fails closed on those assets. Real conversion fidelity and exact
repeatability have not passed; Task 1 must not close or silently advance its
dependent lifecycle work. Resume only with valid assets obtained through an
approved channel, then complete real acceptance and independent review.

### Task 2: Toolkit Lifecycle, Policy Bridge and CLI

**Files:** create toolkit `docling_adapter.py`, `docling_bridge.py`, `docling_extract.py`, `tests/test_docling.py`; update only the release allowlist entries needed for clean-copy tests.

**Interfaces:**
- Bridge `source(root: Path, receipt: str, format_name: str, artifact_id: str | None = None) -> tuple`: returns catalog state, selected original record, source identity and bounded bytes. Reject toolkit member selection.
- Bridge `binding() -> dict`: current policy and bridge/helper-code identities, checked against the policy loaded by this process.
- Bridge `registration_records(root: Path, original: dict, path: Path, value: dict) -> list[dict]`: existing record shapes with OCR/layout scope and inherited restrictions.
- Common `run(root: Path, command: str, receipt: str, format_name: str, artifacts_path: Path, python: Path, actor: str | None = None, expected_state: str | None = None, artifact_id: str | None = None) -> dict`.
- CLI accepts global `--root`; prepare/verify/register accept receipt, `--format pdf|image`, `--artifacts-path`, `--python`. Register additionally requires actor/state; only the corpus accepts `--artifact`.

- [ ] Write failing lifecycle tests for private non-overwriting prepare, exact repeat verify/register, wrong actor, tampered output, stale state, source/model/profile mutation, excluded source before read, policy tightening after import, unsupported format and toolkit member refusal. Use real temporary catalog submission and filesystem effects; substitute only slow inference where necessary.
- [ ] Add failure tests for missing dependencies/assets, unavailable namespace, bounded stderr, timeout/worker failure and output limits. Assert no generated artifact is registered and no source/review/selection is silently changed.
- [ ] Run `PYTHONPATH=tests .venv/bin/python -m unittest test_docling -v`; observe the intended failures.
- [ ] Implement the bridge using the existing policy/catalog APIs and unchanged atomic publication helpers. Bind all adapter/bridge/policy/helper hashes, source metadata, runtime/models and complete profile into the new run ID. Use the installation's existing extraction home and `A-extract-`/`R-extract-` identity conventions.
- [ ] Implement the common runner around `unshare -Urn` and the dedicated interpreter. Before heavy imports, enforce one thread, 16 GiB address-space limit, source/output/pixel/page limits and offline settings. Parent enforces the 600-second wall timeout and terminates only its own worker process group; diagnostics are capped at 64 KiB.
- [ ] Implement registration using exact saved request/completion records and existing transactional submission/backups. Recheck source eligibility, bytes, engine/models and catalog state at operation boundaries. Preserve parent coverage; child remains deferred.
- [ ] Write and run `test_committed_retry_preserves_newer_parent_metadata`, `test_postcommit_failure_reports_committed_and_recovers`, and `test_uncommitted_metadata_drift_requires_reconciliation`; first demonstrate each expected failure, then implement the missing recovery behavior.
- [ ] Run lifecycle tests and one complete real Docling prepare/verify/register/retry through the toolkit CLI against an isolated temporary catalog. Verify deferred artifact/relationship scope, unchanged review events/intake/selection and current-source restrictions.

**Completion:** the toolkit exposes explicit, reproducible OCR ingestion with catalog recovery and no automatic content publication.

### Task 3: Corpus Integration without Historical Extractor Changes

**Files:** copy common files from Tasks 1-2 into corpus `tools/extraction/`; create corpus bridge and `test_docling.py`.

**Interfaces:** consume Task 1 worker and Task 2 common runner unchanged. The corpus bridge implements the same three functions and supplies existing corpus policy/handling/output locations and retained-member selection.

- [ ] Write failing corpus tests for originals in ordinary intake stages, exact receipt/member ownership, excluded ancestors, deleted package wrapper versus excluded ancestor, current filename/subtree restrictions, source-hash mismatch, symlinks and policy changes before source reads.
- [ ] Add corpus lifecycle tests for exact byte verification, independent registration, stale state, source mutation and committed retry. Reuse structural fixture patterns from `test_extract.py`, not real intake.
- [ ] Run `tools/extraction/.venv/bin/python -m unittest discover -s tools/extraction -p 'test_docling.py' -v` and observe failures before implementing the bridge.
- [ ] Copy identical reviewed common files and implement only the corpus bridge. Use the existing retained-artifact snapshot API; preserve audience/handling and member ancestry. Do not modify `extract.py`, compact/streaming adapters or cohort routing.
- [ ] Run the corpus Docling tests, common worker tests and existing extraction suite. Compare both original wrapper hashes with the recorded baseline and verify legacy fingerprint checks.
- [ ] Run the corpus CLI's real scanned-PDF prepare/verify/register against an isolated temporary catalog under corpus policy paths. No command targets the populated production catalog or real originals for this exercise.
- [ ] Verify shared files match the toolkit byte-for-byte. If bridge differences require a common-interface change, update both consumers and regressions together; record the ruling.

**Completion:** both installations exercise their real source-policy bridge with the identical common engine; historical extraction still works unchanged.

### Task 4: Operating Guide, Packaging and Independent Handoff

**Files:** toolkit extraction guide, README, roadmap, release allowlist and approved spec/plan status; corpus extraction guide, README and scoped prompt/maintenance entries.

**Interfaces:** document only actual CLI/profile/model provisioning from Tasks 1-3. Existing read-only MCP and selection formats do not change.

- [ ] Document separate environment/model provisioning, exact commands, resource limits, inherited restrictions, output scope, inspection, repeated verification, registration retries and actionable failures. State explicitly that Docling renditions are not yet qualified-reader selections.
- [ ] Update toolkit roadmap capability state without pretending other missing adapters/cohort automation were ported. Include every required new source/guide/test in `release-files.json`, never environments, model locks/weights, operational records or private fixtures.
- [ ] Run toolkit tests and clean-copy initialization/package checks; confirm optional Docling is not required for ordinary initialization/extraction. Run both installations' targeted suites and explicit real conversion acceptance matrix. Record actual commands/results and remaining fidelity/resource uncertainty in the scoped ledger.
- [ ] Record the corpus's exact owner prompts and approved scope as direction, not program evidence. Log direction, decision, saved integration and observed verification without changing ORR status or intake stages.
- [ ] Inspect persisted changes, links, source preservation and scoped diffs. Run explicit context preflight refresh if selected context/index rules were affected; do not rebuild authoritative stores.
- [ ] Request one fresh whole-change code review with the original request, approved spec/plan, exact task-owned diff, fixture outcomes and limitations. Resolve high-confidence correctness findings through failing regressions before final verification.
- [ ] Final handoff distinguishes shipped behavior, unsupported formats, any blocked runtime prerequisites and unmeasured private-document accuracy. Do not claim completion if real OCR/repeatability/registration acceptance remains blocked.

**Completion:** both working paths persist, the reusable package stays data-empty, documentation matches behavior and a fresh review has no unresolved important findings.

## Plan Self-Review

The worker owns format fidelity, runtime/model identity and resource enforcement;
the coordinator owns verification/publication/registration; bridges own each
installation's source rules. Their signatures above agree. Tasks 1-4 cover the
spec's behavior, compatibility, authority, recovery, packaging and acceptance
requirements. Each Review Focus item has a named owning test/task.

The owner supplied the required execution/workspace choices and the Task 0
addendum. Execute continuously, with per-task independent review and verified
Kata evidence; escalate real blockers rather than repeatedly seeking approval.
