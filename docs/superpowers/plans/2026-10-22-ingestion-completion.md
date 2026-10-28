# Ingestion Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the Docling ingestion capability in this repository alone: re-pin reachable OCR assets and pass real conversion acceptance, build the catalog lifecycle and CLI, prove the installation boundary against a clean copy of this repository's own release allowlist, rename persisted store identity, and document what actually ships.

**Architecture:** One optional conversion worker (already written) runs inference in a dedicated interpreter inside a `unshare -Urn` child. A common adapter owns process control, rendition identity, publication, repeat verification and catalog registration. A single installation-specific bridge owns source policy, catalog selection and registration-record shape. A thin CLI exposes explicit `prepare`/`verify`/`register`. Existing `extract.py` is untouched.

**Tech Stack:** Python 3.11-3.14, Linux user/network namespaces, Docling 2.140.0 (`docling-slim`), RapidOCR 3.10.0 on CPU ONNX Runtime 1.31.0, standard-library `unittest`, existing catalog/policy/review-event modules.

**Planning home:** [development and ingestion roadmap](../../../ROADMAP.md).

**Specs:** [October 22 ingestion completion and closure design](../specs/2026-10-22-ingestion-completion-design.md), which amends the [October 20 Docling ingestion adapter design](../specs/2026-10-20-docling-ingestion-design.md). The October 20 design remains in force except where the October 22 amendment changes it.

**Supersedes:** [October 20 implementation plan](2026-10-20-docling-ingestion.md), Tasks 1-4. That plan's Task 0 (Kata tracking and the roborev pre-commit gate) is complete and is not repeated here. Its Task 3 (porting into a second installation) is withdrawn.

**Status — October 26, 2026: complete for the approved toolkit-only scope.** Worker, lifecycle, clean-copy boundary, identity, guide and reviewed fixes are committed. Final real CLI lifecycle acceptance passes, including repeated prepare and committed registration retry. Worker fidelity acceptance remains applicable; real-document OCR accuracy remains unmeasured. No second installation, source corpus, deployment or publication approval is implied.

**Checklist convention:** checked steps below describe verified outcomes, not invented historical execution. Unverified red-first ordering and original task-split commits are explicitly historical rather than outstanding capability gates. Consolidated delivery replaced the proposed task-by-task commits. Current empty stores already carry the new identities; no destructive re-initialization was performed during closure.

## Final completion reconciliation - October 26, 2026

| Work | Verified state | Remaining in approved scope |
|---|---|---|
| Task 1: assets and worker | Real fixture fidelity/repeatability acceptance; profile version 2 and pinned assets; committed and reviewed | None; fixture matrix not re-run during closure |
| Task 2: lifecycle, bridge and CLI | Lifecycle/recovery regressions and real CLI prepare/verify/register/retry acceptance pass; committed and reviewed | None |
| Task 3: clean-copy boundary | Cloned CLI lifecycle, both policy directions, bridge-removal refusal and package checks pass; committed and reviewed | None; no other installation exercised |
| Task 4: identity, closure and guide | Renamed empty-store identities, superseded-ID refusal, schema checks, guide and closure complete; committed and reviewed | None; no authority store deleted or rebuilt during closure |

Installed-environment tests, lightweight worker tests, schema freshness and
optional-runtime checks pass. Real CLI acceptance independently converts at
each operation and compares complete rendition bytes, without substituting
inference. It verifies private non-overwriting publication, deferred child
registration, inherited handling, honest relationship scope, unchanged parent
coverage/source/review/selection, no index creation and idempotent retry.
Whole-change review covers the exact delivered diff with no excluded paths.
The legacy extractor remains byte-identical. Operational execution evidence
is retained privately, not distributed in the release package.

### Historical reconciliation - October 23–24, 2026 (superseded)

The following records explain the earlier open state; they are not current blockers.

The reconciliation ran the installed-environment suite: 72 tests passed. The
lightweight worker suite passed 25 tests with 7 opt-in skips; it did **not**
re-run real inference. Schema `--check` passed and the legacy extractor hash
still matches the recorded baseline. Passing clean-copy/package tests do not
establish publication approval or real-document accuracy.

**Reviewed Kata reconciliation - October 24:** the supported launcher now
accepts explicit project `librarian` with `--as pi`. Independent completion
audit `283e782c-4fbb-423b-a415-056e2eced147` approved partial cleanup:
`3axg` (worker review), `wxj5` (original worker criteria) and `1s34`
(original hook work under amended scope) closed done; `gb16` retired
`wontfix` as withdrawn, not successfully integrated. Parent `k6pr`, lifecycle
`ekbs` and final handoff `9jj9` remain open. Historical task criteria were
preserved, obsolete attention removed and final handoff now depends on the
unfinished lifecycle, not the withdrawn port. Unrelated tasks were untouched.

The audit found outstanding corrections: check catalog state before rendition
publication; collect diagnostics with a bounded collector rather than slicing
after unbounded collection; align guide installation/provisioning with pinned
requirements; include final-task paths in the mandatory staged-review scope;
and exercise the cloned CLI rather than only its adapter API. The real CLI
cycle, local-store evidence and delivery/final-review gaps remain visible.
The reviewer inspected code and saved evidence but did not execute tests or
fresh inference. No application fixes were made by this reconciliation.
This section is a bounded verification snapshot; Kata owns live task state.

## Global Constraints

- **No second-installation obligation.** Nothing here requires another installation to exist, to receive files, to stay byte-identical with this repository, or to be present for a test to pass. Do not read, port into, rebuild or inspect any other installation.
- Additive only. `tools/extraction/extract.py` bytes must not change; record and re-check its SHA-256.
- Conversion runs in a child process inside a Linux user/network namespace with networking disabled. Never add a network-enabled retry, an alternate model host, a proxy or any security-control bypass.
- `verify` and `register` independently repeat conversion and compare the complete canonical rendition byte-for-byte.
- Supported inputs: PDFs with embedded text, scanned or mixed pages, and single-image PNG/JPEG. Receipt originals only; member selection (`--artifact`) is not in this release.
- The generated rendition is registered as a deferred child artifact. No automatic review, indexing, intake-stage advancement or retrieval selection.
- Per-operation limits stay as the worker defines them: 64 MiB source, 100 PDF pages, 32M pixels per page, 64 MiB result, 16 GiB address space, 600 s wall, 64 KiB diagnostics.
- Keep this repository data-empty. Never commit model weights, environments, fixtures from real sources, operational records or private configuration. Never seed a demonstration corpus.
- Preserve the pre-existing uncommitted changes in this checkout. A task commit contains only that task's verified files.
- Run the suite with the installed environment: `.venv/bin/python -m unittest discover -s tests -v`.

## Review Focus

- Re-pinned OCR assets must change rendition identity, not merely file names: a rendition prepared under profile version 1 must not be accepted as equivalent under version 2. Task 1 exercises it.
- Mixed PDFs with legitimate blank pages must keep a complete page inventory without accepting failed pages as blank. Task 1 exercises it.
- A committed registration whose parent metadata later changes must replay the saved request without overwriting newer metadata. Task 2 exercises it.
- Model assets replaced through links or mutated during inference must not retain the old identity. Already covered by the existing worker tests; Task 1 keeps them passing against the new pins.
- Policy tightened after module import must block the source before any read or conversion. Tasks 2 and 3 exercise it.
- The clean copy's own policy, not this checkout's, must decide eligibility inside the copy. Task 3 exercises it in both directions.
- A store carrying the superseded `FCAT`/`FREV`/`FCTX` identity must be refused with a message naming that superseded identity. Task 4 exercises it.

## File Structure

Existing files this plan modifies:

- `tools/extraction/docling_worker.py` - re-pin `REQUIRED_ASSETS`, bump `profile()` to version 2, name the selected OCR models in the profile.
- `tools/extraction/test_docling_worker.py` - update profile and asset assertions; keep the opt-in real matrix.
- `tools/catalog/catalog_store.py` - `APPLICATION_ID` rename, superseded-identity refusal.
- `tools/review_events/event_store.py` - same.
- `tools/context_index/context_index.py` - same.
- `tools/schemas/catalog.sql`, `review-events.sql`, `context-index.sql` - regenerated snapshots (never hand-edited).
- `tests/test_kit.py` - clean-copy acceptance extension and superseded-identity regressions.
- `release-files.json` - add the new sources, tests, guide and documents.
- `ROADMAP.md`, `docs/decisions/0001-project-identity.md`, `docs/decisions/0003-store-compatibility.md`, `docs/decisions/README.md`, `README.md` - accurate capability and decision state.
- `docs/superpowers/plans/2026-10-20-docling-ingestion.md` - mark Tasks 1-4 superseded by this plan; remove the one program-identifying reference in its final task.

New files this plan creates:

- `tools/extraction/docling_adapter.py` - common lifecycle: isolated process control, rendition identity, publication, repeat verification, registration.
- `tools/extraction/docling_bridge.py` - this installation's source policy, catalog selection and registration-record adaptation.
- `tools/extraction/docling_extract.py` - explicit `prepare`/`verify`/`register` CLI. No optional dependency is imported in this process.
- `tools/extraction/README.md` - operating guide: environment and model provisioning, commands, limits, failures, what a rendition is not.
- `tests/test_docling.py` - lifecycle, policy and failure regressions against isolated temporary catalogs.
- `docs/decisions/0008-proof-of-concept-closure.md` - generic closure record.

Ignored, never committed:

- `tools/extraction/.local/venv-docling/` - the dedicated interpreter.
- `tools/extraction/.local/models/` - provisioned model assets.
- `.superpowers/sdd/2026-10-20-docling-ingestion/` - execution ledger. Continue using it; do not start a second register.

## Module Boundaries

The worker owns format fidelity, runtime/model identity and in-child resource enforcement. The adapter owns isolation, identity, publication, verification and registration. The bridge owns which bytes this installation will even look at. Each layer calls only the layer below it:

```
docling_extract.py  (CLI, stdlib only)
      v
docling_adapter.py  (stdlib only; spawns the worker)
      |  imports bridge for source/record policy
      v
docling_bridge.py   (policy.py + catalog_store.py)

  [unshare -Urn + dedicated interpreter]
      v
docling_worker.py   (docling, rapidocr, onnxruntime, torch)
```

Nothing in the parent process imports `docling`, `torch`, `onnxruntime` or `rapidocr`. A missing optional environment must produce an actionable error from the adapter, never an `ImportError` traceback.

## Execution Setup

- [x] Current baseline verified before documentation changes; historical baseline artifact naming is not retroactively asserted. Original execution instruction: record the baseline before touching anything: `sha256sum tools/extraction/extract.py`, `git status --porcelain`, `.venv/bin/python -m unittest discover -s tests -v`, and `.venv/bin/python -m unittest discover -s tools/extraction -p 'test_docling_worker.py' -v`. Save all four into the ledger as `task-5-baseline-*`, recording the counts you actually observe rather than any count quoted from an earlier session. Both suites must be green before any change; if they are not, stop and report. The existing uncommitted modified/untracked files are saved work; they are not evidence the owner personally edited them and must not be committed as part of a task.
- [x] Confirm the dedicated interpreter still resolves its pins: `tools/extraction/.local/venv-docling/bin/python -c "import docling, rapidocr, onnxruntime, torch"`. If it does not, rebuild it from `tools/extraction/requirements-docling.txt` following that file's documented two-step installation. Do not loosen a pin and do not touch the MCP environment.
- [x] Reconcile existing task tracking without creating a second register; withdrawn scope is not marked successfully delivered, and completion requires verified evidence. Historical setup instruction: update Kata: parent `k6pr` resumes; `wxj5` (Task 1) is unblocked by Task 1 below; add tasks for this plan's Tasks 2, 3 and 4 with stable idempotency keys and explicit dependencies. Kata is reachable only through `python3 ~/kenn-kopy/scripts/kata-local.py`. If it rejects this project, record that in the ledger and continue; do not invent a second backlog file.

### Task 1: Re-pinned OCR Assets and Real Conversion Acceptance

**Goal:** the worker converts the real fixture matrix faithfully, twice, offline, with assets that are actually obtainable.

**Files:** `tools/extraction/docling_worker.py`, `tools/extraction/test_docling_worker.py`. Assets land in ignored `tools/extraction/.local/models/`.

**Context an engineer needs:** `REQUIRED_ASSETS` is an ordered tuple at `docling_worker.py:28`. Indices 0-4 are the layout-heron and TableFormer-accurate files and are already valid locally. Indices 5, 6, 7 are the OCR detection, classification and recognition models, wired at `_run_conversion` lines 485-487 as `det_model_path`, `cls_model_path`, `rec_model_path`. The currently pinned OCR files resolve through a model host this network answers with an HTML block page, so the three files on disk are 14 KiB block pages that ONNX Runtime rejects; the worker correctly fails closed with code `invalid_assets`. The replacements, their provenance and the measured evidence are recorded in `.superpowers/sdd/2026-10-20-docling-ingestion/task-1r-asset-probe.md`.

**Target asset identity:**

| Index | `REQUIRED_ASSETS` entry | Bytes | SHA-256 |
|---|---|---|---|
| 5 | `RapidOcr/en_PP-OCRv3_det_infer.onnx` | 2423224 | `f139598bc2af4e4b6fe98dec11574e30edfdd91fc94ac1425c18ace3bd5a866b` |
| 6 | `RapidOcr/ch_ppocr_mobile_v2.0_cls_infer.onnx` | 585532 | `e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c` |
| 7 | `RapidOcr/en_PP-OCRv3_rec_infer.onnx` | 8967018 | `ef7abd8bd3629ae57ea2c28b425c1bd258a871b93fd2fe7c433946ade9b5d9ea` |

Source: the `SWHL/RapidOCR` published ONNX conversions (Apache-2.0), revision `1cfba2e90fc938db55889873735088de210cc173`, at repository paths `PP-OCRv4/en_PP-OCRv3_det_infer.onnx`, `PP-OCRv1/ch_ppocr_mobile_v2.0_cls_infer.onnx` and `PP-OCRv3/en_PP-OCRv3_rec_infer.onnx`. The character dictionary is embedded in the recognition graph, so no `rec_keys_path` file is needed.

**Interface change:** `profile()` returns `"version": 2` and replaces the OCR section's `"model_size": "small"` with an explicit `"models"` mapping naming the three selected relative asset paths, so the profile states which models produced a rendition instead of implying them from a size label. Everything else in `profile()` is unchanged. `RapidOcrOptions` still receives `model_size="small"`, because the installed OCR stage ignores that field whenever explicit model paths are supplied; the profile must therefore not report it as if it selected anything.

- [x] Model-identity tests exist and pass; their red-first failures are recorded in `progress.md`. Original instruction: write the failing tests first. In `test_fixed_profile_forbids_remote_and_automatic_backends` assert `profile()["version"] == 2`, assert `profile()["ocr"]["models"]` equals the three relative paths above in det/cls/rec order, and assert `"model_size" not in profile()["ocr"]`. Add `test_profile_version_distinguishes_ocr_model_changes`: build two profiles, one with `REQUIRED_ASSETS` patched back to the superseded OCR names, and assert the canonical bytes of the two profiles differ. Add `test_required_assets_name_the_pinned_ocr_models` asserting `REQUIRED_ASSETS[5:8]` are exactly the three paths above.
- [x] Recorded model-identity red-first evidence inspected. Original instruction: run `.venv/bin/python -m unittest discover -s tools/extraction -p 'test_docling_worker.py' -v`. Confirm the three new assertions fail for the stated reason and that nothing else regressed.
- [x] Make the change in `docling_worker.py`: re-pin indices 5-7, bump the profile version, add the explicit model mapping, remove `model_size` from the profile. Do not relax `_validate_assets`, `_observe_assets`, `assert_assets_unchanged` or any limit. The manifest must still bind observed relative paths, sizes and hashes and must still be re-verified after inference.
- [x] Re-run the lightweight suite. All tests pass, the opt-in real tests still skip.
- [x] Recorded provisioning and hash verification inspected; no assets downloaded during closure. Original instruction: provision the assets as a separate explicit step, documented verbatim in the Task 4 guide: fetch the three files at the pinned revision into `tools/extraction/.local/models/RapidOcr/` under their `REQUIRED_ASSETS` names, then verify each size and SHA-256 against the table above **before first use**. A mismatch or an unreachable publisher is an actionable provisioning failure; stop and report it. Do not fetch through an alternate host or proxy. Delete the three superseded block-page files only after the new ones verify.
- [x] Saved successful five-fixture real acceptance inspected; not re-run during closure. Original instruction: run the opt-in real acceptance matrix with the dedicated interpreter: `DOCLING_REAL_TESTS=1 DOCLING_ARTIFACTS_PATH=$PWD/tools/extraction/.local/models tools/extraction/.local/venv-docling/bin/python -m unittest discover -s tools/extraction -p 'test_docling_worker.py' -v`. All five fixtures (`native.pdf`, `scanned.pdf`, `mixed.pdf`, `image.png`, `image.jpg`) must pass. Each already asserts two independent `unshare -Urn` runs with byte-identical canonical renditions, the negation string, the digit run, two-column reading order, the table's cell associations, the complete page inventory, blank-page handling on `mixed.pdf` and source-located provenance. Capture the printed `REAL PASS` lines, including each rendition's byte count and SHA-256, into the ledger.
- Conditional safety instruction (no current fixture blocker): if any fixture fails on recognition quality rather than mechanism, stop and escalate with the measured text. Do not weaken an assertion, relax `text_score`, enable full-page OCR or substitute a different fixture to make it pass.
- [x] Worker, pinned requirements and tests committed together after exact selected-scope review. Proposed two-file commit/message shape replaced by consolidated delivery.

**Completion:** the five-fixture matrix passes twice with no network access using hash-verified reachable assets; `profile()` version 2 names the models; every error and budget path is still non-successful.

**Status, October 23, 2026 — complete.** The re-pin, profile version bump and
three model-identity tests are done; the assets were provisioned and
hash-verified in place. The original three-line, approximately 30pt-gutter
fixture was found to resemble a row grid: Docling consistently clustered its
top rows across columns even for native text. The replacement fixture has five
lines per column and a visible 120pt+ gutter, and strengthens the assertion to
check all five labels in each column. The complete matrix now passes twice in
isolated networkless children with byte-identical canonical renditions and all
content, table, page, blank-page and provenance assertions intact. No worker
setting or acceptance property was weakened. Exact hashes and evidence are in
`.superpowers/sdd/2026-10-20-docling-ingestion/progress.md`.

### Task 2: Lifecycle, Bridge and CLI

**Goal:** explicit, reproducible, recoverable OCR ingestion through this repository's own catalog.

**Files:** create `tools/extraction/docling_adapter.py`, `tools/extraction/docling_bridge.py`, `tools/extraction/docling_extract.py`, `tests/test_docling.py`.

**Context an engineer needs.** Read `tools/extraction/extract.py` first; it is the working reference for this exact lifecycle over a different engine and must not be modified. Reuse its proven shapes:

- `publish(path, raw)` is write-once: identical bytes are a no-op, different bytes refuse, and a new file is created through `O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW` at mode `0o600` plus `fsync`, `os.link(..., follow_symlinks=False)` and `catalog.fsync_directory(parent)`.
- The register path saves `registration.json` before submitting and `completion.json` after, so a retry replays the exact saved request. On drift it checks record history for the same `request_id` and reuses the saved recipe rather than overwriting newer metadata. A commit followed by a failed completion check raises an error that says the registration committed and must be retried with the same request.
- Identity convention: `run_id` is a digest over source, extractor and format; child artifact ids are `A-extract-<run_id>` and relationships `R-extract-<run_id>`.
- `catalog.writer_lock(home)` serializes writes; `catalog.safe_path`, `private_directory` and `private_file` enforce path and permission rules; `catalog.submit(root, records, *, actor, request_id, expected_state, reason)` is transactional and takes the current state token.

Use `data/extractions/.local` as the extraction home, as `extract.py` does, with `docling-<format>` as the extractor label so Docling renditions never collide with existing ones. Child artifact ids are `A-docling-<run_id>` and relationships `R-docling-<run_id>`.

**Interfaces.**

`docling_bridge.py` - the only installation-specific file:

- `source(root: Path, receipt: str, format_name: str) -> tuple[dict, dict, dict, bytes]` - returns `(catalog_state, original_record, source_identity, raw_bytes)`. Mirrors `extract.source`: load `catalog.receipt_metadata`, require `role == "original"`, `presence == "present"`, `coverage != "excluded"`, require `policy.allowed(relative.as_posix(), "original_roots")`, require the extension matches the selected format (`pdf` -> `.pdf`; `image` -> `.png`, `.jpg`, `.jpeg`), read the bytes through a `O_NOFOLLOW` regular-file read bounded by the worker's 64 MiB source limit, and require the catalog SHA-256 to match the observed digest. Reject any `artifact_id`-style member selection with an actionable message; member selection is not in this release.
- `binding() -> dict` - the policy fingerprint plus the SHA-256 of the bridge and adapter files, re-checked against the policy this process actually loaded, so a policy or code change after import cannot be silently inherited.
- `registration_records(root: Path, original: dict, path: Path, value: dict) -> list[dict]` - parent update, deferred child artifact and `rendition-of` relationship, following `extract.registration_records` but with OCR/layout scope wording and `extraction="existing-rendition"` on the parent. Inherit `audience` and `handling_refs`; never widen them. Preserve the parent's coverage.

`docling_adapter.py` - common, stdlib only:

- `run(root: Path, command: str, receipt: str, format_name: str, artifacts_path: Path, python: Path, actor: str | None = None, expected_state: str | None = None) -> dict` - the `prepare`/`verify`/`register` lifecycle, same result shape as `extract.run` plus the worker's profile, runtime and asset manifest.
- `inspect(artifacts_path: Path, python: Path) -> dict` - run the worker's `inspect` operation and return its runtime/profile/asset report. No source argument.
- `convert(source: bytes, name: str, format_name: str, artifacts_path: Path, python: Path) -> dict` - write a private job file, launch `unshare -Urn <python> docling_worker.py JOB RESULT`, enforce the 600-second wall timeout in the parent, terminate only its own worker process group on timeout, read the bounded result and raise an actionable error carrying the worker's error `code` and a message capped at 64 KiB. Set the offline environment and single-thread variables for the child. The result file must not pre-exist.
- `bundle(...)` - compute the rendition value and `run_id` binding source identity, bridge `binding()`, the worker file hash, and the complete worker-reported runtime, profile and asset manifest. A changed model, profile version or source produces a different `run_id`, never a silent reuse.

`docling_extract.py` - CLI: global `--root`; subcommands `inspect`, `prepare`, `verify`, `register`; `prepare`/`verify`/`register` take a receipt plus `--format pdf|image`, `--artifacts-path` and `--python`; `register` additionally requires `--actor` and `--expected-state`. One canonical JSON object on stdout, an actionable one-line message on stderr with exit 1 on failure. There is no `--artifact` option.

- [x] Lifecycle tests exist and pass; historical red-first ordering not independently evidenced during closure. Original instruction: write failing lifecycle tests in `tests/test_docling.py`: private non-overwriting prepare; `verify` and `register` each independently repeating conversion and requiring the exact prepared bytes; wrong actor; tampered prepared output; stale state token; source mutated between prepare and register; model manifest changed between prepare and register; profile version changed between prepare and register; a source excluded by policy refused before any read; policy tightened after module import; unsupported format; and member selection refused. Use real temporary catalog submission and real filesystem effects; substitute only the inference call.
- [x] Failure-path tests exist and pass; historical red-first ordering not independently evidenced during closure. Original instruction: write failing failure-path tests: missing optional dependency, missing or invalid assets, unavailable namespace, bounded stderr, worker timeout, worker failure and output over the limit. Each must assert that no generated artifact was registered and that no source, review event or selection changed.
- [x] Recovery tests exist and pass; historical red-first ordering not independently evidenced during closure. Original instruction: write failing recovery tests named `test_committed_retry_preserves_newer_parent_metadata`, `test_postcommit_failure_reports_committed_and_recovers` and `test_uncommitted_metadata_drift_requires_reconciliation`, modeled on the saved-request logic in `extract.run`.
- Historical process, not a remaining acceptance gate: red-first lifecycle run was not independently evidenced during closure; current lifecycle tests pass in ordinary discovery.
- [x] Implement the bridge against the existing `policy` and `catalog_store` APIs. Re-check eligibility, bytes, bridge binding and catalog state at every operation boundary, not once at entry.
- [x] Implement the adapter. Keep all optional imports inside the child. Enforce limits before the child does heavy imports. Publish through the same write-once discipline as `extract.publish`; do not import `extract`.
- [x] Implement the CLI. Verify no optional module is imported in the parent: `.venv/bin/python -c "import sys; sys.argv=['x','--help']; ..."` style check, or assert `'docling' not in sys.modules` after importing the CLI module.
- [x] Run the new suite plus the full `tests` suite. Then run one complete real `prepare`/`verify`/`register`/retry through the CLI against an isolated temporary catalog using the dedicated interpreter and the Task 1 assets. Confirm the child artifact is deferred, the relationship scope is honest, review events and selection are unchanged, and `sha256sum tools/extraction/extract.py` still matches the baseline.
- [x] Lifecycle committed with Tasks 3–4; final gate scope expanded as required. Passing review covered every committed path with no exclusions and an exact matching diff.

**Completion:** explicit reproducible OCR ingestion with catalog recovery, no automatic publication, and `extract.py` byte-identical to baseline.

### Task 3: Clean-Copy Installation Boundary

**Goal:** prove, without any second installation, that `docling_bridge.py` is the only installation-specific file and that a fresh copy decides eligibility by its own policy.

**Files:** `tests/test_kit.py`, `release-files.json`.

**Context an engineer needs.** `tests/test_kit.py` already has `clone(destination)`, which copies every path in `package.release_paths(ROOT)` into a directory, and `test_clean_copy_and_policy_freshness`, which clones the kit into a temp dir, runs `tools/initialize.py` as a subprocess with `cwd=kit`, asserts `seeded_records == 0`, writes a `config/policy.local.json` override, asserts `context_index.py status` then refuses as stale, rebuilds, packages, and asserts the archive's names equal the allowlist. Extend that pattern; do not invent a parallel harness. `tools/policy.py` resolves `ROOT` from its own file location, so a cloned kit loads the clone's policy, and `config/policy.local.json` may override only known fields - `original_roots` is one of them.

- [x] Add to `release-files.json` the files that exist by now, so the clone actually contains them: `tools/extraction/docling_adapter.py`, `tools/extraction/docling_bridge.py`, `tools/extraction/docling_extract.py`, `tools/extraction/requirements-docling.txt`, `tools/extraction/test_docling_worker.py`, `tests/test_docling.py`, this plan and the October 22 spec. The guide and the closure record are added in Task 4, when they exist; `release_paths` refuses a missing file, so do not list either one yet. Never add `tools/extraction/.local/**`, model weights, environments or ledger files; `tools/package.py:release_paths` already rejects `.local`, `data`, `sources`, `documents` and `*.sqlite3`, so a mistake there fails loudly.
- [x] Clean-copy CLI/policy regression exists and passes; historical red-first ordering not independently evidenced. Original instruction: write a failing `test_clean_copy_runs_the_docling_lifecycle_under_its_own_policy`: clone the kit, run `tools/initialize.py` and assert empty stores, write the clone's `config/policy.local.json` with `original_roots` set to a directory name this checkout does not use, register a receipt and original in the clone's catalog, then run the clone's `tools/extraction/docling_extract.py prepare` as a subprocess with `cwd=kit`. Assert that the same source placed under this checkout's eligible root is **refused** inside the clone, and that placed under the clone's own eligible root it is **accepted and registered** into the clone's previously empty catalog. Inference may be stubbed; fidelity is proven once by Task 1.
- [x] Bridge-removal regression exists and passes; historical red-first ordering not independently evidenced. Original instruction: write a failing `test_bridge_is_the_only_installation_specific_file`: delete `tools/extraction/docling_bridge.py` from the clone and assert the lifecycle fails with an actionable message that names the missing bridge, while every other cloned file is byte-identical to this repository's copy. This is what makes the single-file claim falsifiable rather than asserted.
- Historical process, not a remaining acceptance gate: pre-implementation clean-copy failures were not independently evidenced during closure; current regressions pass.
- [x] Make them pass by adjusting the production code only where the tests reveal a real coupling - for example a path resolved from the current working directory instead of the installation root, or a constant the bridge should own. Do not special-case the test.
- [x] Re-run the full suite plus `tools/schemas/export_schemas.py --check` and a clean-copy package check. Confirm the clone initializes and runs its existing extraction workflows with the optional Docling environment entirely absent.
- [x] Before committing, extend the pre-commit gate's `ALLOWLIST` in `tools/review/roborev_precommit.py` to cover `tests/test_kit.py` and `release-files.json`. The gate reviews only allowlisted staged paths and reports the rest as excluded; it must not report this task's own changes as unreviewed. Confirm from the stored verdict that they were actually reviewed - a queued job is not a passed review.
- [x] Clean-copy tests, release manifest and gate changes delivered in a consolidated commit with passing review.

**Completion:** the clean copy proves the installation boundary in both directions, in ordinary verification, with no second installation involved.

### Task 4: Store Identity Rename, Closure Record and Guide

**Goal:** persisted identity stops carrying a fossil of another program's name, the record of what happened is generic and accurate, and the documentation describes only what ships.

**Files:** the three store modules, the three regenerated schema snapshots, `tests/test_kit.py`, `tools/extraction/README.md`, `docs/decisions/0008-proof-of-concept-closure.md`, `docs/decisions/0001-project-identity.md`, `docs/decisions/0003-store-compatibility.md`, `docs/decisions/README.md`, `ROADMAP.md`, `README.md`, `release-files.json`, `docs/superpowers/plans/2026-10-20-docling-ingestion.md`.

**Context an engineer needs.** The three SQLite `application_id` constants decode as ASCII `FCAT`, `FREV`, `FCTX`; the leading `F` is a fossil of another program's name. Decision 0001 kept it only because renaming would have cost a migration against a populated store that this repository is no longer coupled to. Every store here is empty, confirmed by direct row count. The constants and their guards are at `tools/catalog/catalog_store.py:26` with the guard at `:403` ("Not a catalog store"), `tools/review_events/event_store.py:20` with the guard at `:171` ("Not a review-event store"), and `tools/context_index/context_index.py:25` with the guard at `:391` ("Refusing non-index database"). `tools/schemas/export_schemas.py` parses these modules with `ast` and regenerates the snapshots; `--check` fails on staleness. `catalog_store.VALIDATOR_SHA256` is the module's own file hash and is checked at `:557`, so editing the module naturally changes it - that is intended, not a problem to work around.

**Target values:** `LCAT` = `0x4C434154` (1279476052), `LREV` = `0x4C524556` (1280460118), `LCTX` = `0x4C435458` (1279480920).

**Explicitly not renamed:** the authority strings `catalog-metadata-only` and `review-events-only`, record locator syntax, receipt identifiers, selection-manifest fields, and the ordinary word "corpus", which names no program and is persisted in retrieval classifications such as `absent-corpus`.

- [x] Closure read-only inspection confirms zero catalog revisions/versions/current records and review events, with current renamed identities. Historical pre-rename ordering is not asserted. Original instruction: verify emptiness by direct row count before changing anything: catalog revisions, versions and current records, and review-event rows, must all be 0. Record the counts in the ledger. A schema snapshot is not a backup; if any count is nonzero, stop and escalate.
- [x] Identity/refusal regressions exist and pass; historical red-first ordering not independently evidenced. Original instruction: write failing regressions in `tests/test_kit.py`: a freshly initialized catalog, review-event store and index each report the new `application_id`; and a store whose `application_id` is the superseded value is refused with a message that names the superseded identity and states that explicit migration or re-initialization is required. Construct the superseded-identity fixture by writing the old `PRAGMA application_id` into a temporary database - do not keep a stale store around for the test to find.
- Historical process, not a remaining acceptance gate: pre-implementation identity failures were not independently evidenced during closure; current regressions pass.
- [x] Change the three constants. Add the superseded-identity refusal to each of the three guards: detect the specific superseded value, name it, and say that explicit migration or re-initialization is required. Add no automatic migration, conversion or in-place rewrite.
- [x] Regenerate the snapshots with `.venv/bin/python -m tools.schemas.export_schemas`, then confirm `--check` passes. Never hand-edit a generated snapshot.
- [x] Required outcome verified: existing local stores carry LCAT/LREV/LCTX and contain no authority records or selected documents. Historical removal/re-initialization steps were not independently evidenced; closure performed no deletion or rebuild. Temporary fresh-store initialization and refusal are covered by passing regressions.
- [x] Run the full suite plus the clean-copy and package checks.
- [x] Write `docs/decisions/0008-proof-of-concept-closure.md`, status `Accepted`, generically: that a first proof-of-concept installation of this toolkit's approach concluded; what it proved and what it cost; which lessons this repository adopted - the re-pinned reachable assets, the clean-copy installation boundary, the freed store identity; and the explicit statement that it is not maintained from here, receives no ports from here, and supplies no content, identity or path to this repository. Name no program, path, ticket, stage or identity. Add its row to the `docs/decisions/README.md` table.
- [x] Amend `docs/decisions/0001-project-identity.md` where it records the fossil as deliberately unchanged, and `docs/decisions/0003-store-compatibility.md` where the new refusal behavior affects its upgrade contract. Keep 0003's commitments intact: never implicitly migrate an authority store, always assume a derived-index rebuild, ship migrations with their version.
- [x] Remove the two remaining references that identify the other installation's program: the schema-identity example in decision 0001 and the intake-stage mention in the October 20 plan's final task. Replace them with the generic description used everywhere else. Then scan the whole repository for remaining traces; finding none is the expected result, not a skipped check. Record the scan command and its output in the ledger.
- [x] Write `tools/extraction/README.md`: the dedicated environment and its two-step installation; the exact model provisioning commands with the pinned revision and the size/hash verification table; every CLI command with real arguments; the resource limits; what each failure code means and what the operator should do; inherited source restrictions; and explicit statements that a rendition is not review, not indexed, not a qualified-reader selection, and that unsupported formats stay unsupported.
- [x] Update `ROADMAP.md` and `README.md` to shipped state. Say what exists: optional local Docling ingestion with explicit prepare/verify/register and catalog registration. Say what does not: other formats, cohort automation, member selection, retrieval selection, automatic review or indexing. Do not describe the withdrawn port as pending. Mark the October 20 plan's Tasks 1-4 superseded by this plan.
- [x] Add the guide and the decision record to `release-files.json`, re-run the package check, and confirm the archive contains no environment, weights, ledger or operational data.
- [x] Extend the gate `ALLOWLIST` to the store modules, generated snapshots and documents this task changes, so none of them is reported as excluded rather than reviewed. Generated snapshots are reviewed as outputs of the changed definitions, not as hand-written code.
- [x] Whole-change staged review passed and binds the exact delivered diff; prior findings resolved. Original instruction: request one fresh whole-change review with the original request, both specs, this plan, the exact task-owned diff, the real acceptance output and the stated limitations. Resolve high-confidence correctness findings through a failing regression first.
- [x] Final handoff: distinguish shipped behavior from unsupported formats and unmeasured real-document accuracy. Do not claim completion if any real conversion, repeatability or registration acceptance case is still blocked. A passing package check is not a privacy certification or publication approval.

**Completion:** renamed identity in code, regenerated snapshots and fresh stores with an actionable refusal of superseded stores; a generic closure record with no remaining trace; documentation that matches behavior; a fresh review with no unresolved important findings.

## Out of Scope

Carried forward from both specs: Office and ODF formats, transcripts, audio and video, additional image formats, VLM conversion, generated picture descriptions, remote inference, arbitrary plugins, archive expansion, member selection, discovery and copy services, watchers, scheduling, automatic review, intake-stage advancement, retrieval selection, automatic indexing of Docling output, graph integration and cohort automation.

Added by the October 22 amendment: porting anything into another installation, rebuilding one, reading its contents, migrating a populated store of any identity, and installing this capability into any other project.

## Plan Self-Review

**Layering holds.** The worker owns fidelity and in-child budgets; the adapter owns isolation, identity, publication and registration; the bridge owns source eligibility; the CLI is argument parsing. The signatures in Task 2 agree with the worker's existing `convert(source, name, format_name, artifacts_path)` and job protocol, and with the `registration_records` and saved-request shapes that `extract.py` already proves.

**Every Review Focus item has an owner.** Profile-version identity and blank pages: Task 1. Committed-retry metadata and post-import policy tightening: Task 2. Clean-copy policy direction: Task 3. Superseded-store refusal: Task 4.

**The amendment is honored throughout.** No task reads, writes, ports into or depends on a second installation. The installation boundary is proven against this repository's own release allowlist, which runs in ordinary verification.

**Scope check.** Four subsystems are touched: the OCR worker's asset pins, the new lifecycle, the clean-copy test harness and persisted store identity. The identity rename is independent of the Docling work and is sequenced last so a rename problem cannot block ingestion acceptance, and so the clean-copy test exercises the renamed stores. Splitting it into its own plan would duplicate this plan's closure and documentation work, so it stays here as the final task.

**Known residual risk.** OCR recognition quality on real scans is unmeasured; synthetic fixtures establish adapter behavior, not accuracy on any particular document, and measuring that needs an explicit source selection and its own authorization. The selected recognition model is English-only by deliberate choice, since the alternative candidate lost inter-word spacing. If the owner later needs another language, that is a new asset pin, a new profile version and its own acceptance run.
