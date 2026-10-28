# UTF-8 Rendition and Selected Docling Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a strict ordinary UTF-8 text rendition lifecycle and admit only explicitly selected, exact-rendition-reviewed Docling records to qualified retrieval.

**Architecture:** Extend the existing stdlib extraction lifecycle for text while preserving its catalog and publication contracts. Split retrieval admission into catalog identity validation, format-specific record adapters and a versioned Docling qualification seam backed by an active review snapshot; retain extraction JSON as authoritative packet text.

**Tech Stack:** Python 3.11-3.14 standard library, SQLite FTS5, existing catalog/review-event/context-index modules, `unittest`.

**Spec:** [UTF-8 rendition and selected Docling retrieval design](../specs/2026-10-28-text-and-docling-retrieval-design.md)

## Global Constraints

- Perform no acquisition, real ingestion, model execution, operational review/selection, store migration or publication.
- Keep the distribution data-empty; use temporary stores and synthetic/toolkit content only.
- Strict UTF-8 only: optional UTF-8 BOM is recorded; unsupported BOMs, malformed bytes and NUL fail visibly; never guess or replace.
- Exact source bytes/hash/version and line mapping survive extraction; successful decoding is neither review nor qualified context.
- Docling admission requires explicit selection and active review of the exact rendition bytes; conversion or registration never qualifies content.
- Preserve supported catalog/review writers, private non-replacing publication, append-only authority, atomic index replacement and read-only retrieval.
- Existing GeminiTXT and ODT public behavior remains unchanged.
- Use new commits for fixes; do not amend. Review related Roborev findings before final verification and PR creation.

## Review Focus

- A BOM-only or empty text source must register faithfully but fail retrieval selection because it has no records; Task 2 tests this.
- A lone trailing CR and mixed CR/LF/CRLF sequences must preserve exact byte offsets and final-terminator semantics; Tasks 1 and 2 test this.
- A review of the original or an earlier rendition must not qualify the selected Docling bytes; Task 3 tests both mismatches.
- A Docling item that references a page absent from the page inventory must fail the whole build without replacing the old index; Task 4 tests this.
- A review correction after build must make reads stale even when source and rendition bytes are unchanged; Task 3 tests review-state freshness.

---

### Task 1: Ordinary text extraction contract

**Files:**
- Modify: `tests/test_kit.py`
- Modify: `tools/extraction/extract.py`

**Interfaces:**
- Produces: `extract.text(raw: bytes) -> dict`, payload format `text` with `lines`, `bom_bytes`, `ends_with_terminator`, `warnings` and exact byte offsets.
- Consumes: existing `bundle`, `run`, publication and registration helpers.

- [ ] Add failing parser tests for LF, CRLF, CR, mixed endings, blank/final-unterminated lines, multibyte Unicode, UTF-8 BOM, empty/BOM-only input, malformed UTF-8, UTF-16/32 BOM and NUL.
- [ ] Run `python3 -m unittest tests.test_kit.Workspace.test_text_extraction_contract -v`; expect failure because `extract.text` does not exist.
- [ ] Implement `text(raw)` and explicit `--format text`/`.txt` dispatch with byte-exact records and no normalization.
- [ ] Run the targeted parser test; expect pass.
- [ ] Add lifecycle failure tests for deterministic prepare/verify/register/retry, changed source, stale state, failed publication and unchanged review/selection/original bytes.
- [ ] Run targeted lifecycle tests; expect failure until format-specific registration supports text.
- [ ] Implement text registration coverage/reasons using existing writer and recovery paths.
- [ ] Run `python3 -m unittest tests.test_kit.Workspace -v`; expect pass.
- [ ] Commit `feat: add strict UTF-8 text extraction lifecycle`.

### Task 2: Selected text retrieval and documentation

**Files:**
- Modify: `tests/test_kit.py`
- Modify: `tools/context-index/renditions.py`
- Modify: `tools/extraction/README.md`
- Modify: `release-files.json`

**Interfaces:**
- Consumes: Task 1 `text` extraction payload.
- Produces: text rendition selections with stable `#line-N` records and exact source byte locations.

- [ ] Add failing selection/index tests for exact LF/CRLF/Unicode/final-line packets and empty/BOM-only selection refusal.
- [ ] Run the targeted tests; expect format validation failure.
- [ ] Extend rendition validation, identity checks, record generation and metadata for `format: text` without changing GeminiTXT/ODT branches.
- [ ] Run targeted retrieval tests and existing ODT/Gemini tests; expect pass.
- [ ] Document supported encoding, BOM, endings, empty-input and non-qualification behavior.
- [ ] Run `python3 -m unittest tests.test_kit.Workspace -v`; expect pass.
- [ ] Commit `feat: retrieve selected UTF-8 text renditions`.

### Task 3: Strict Docling qualification seam

**Files:**
- Create: `tools/context-index/qualification.py`
- Modify: `tools/context-index/context_index.py`
- Modify: `tools/context-index/renditions.py`
- Modify: `tests/test_kit.py`
- Modify: `release-files.json`

**Interfaces:**
- Produces: `qualification.docling(selection, value, identity, snapshot, engine) -> dict` or fails closed; result contains policy version, review IDs/state and reviewed scope.
- Consumes: one validated `event_reader.Snapshot`, selected Docling envelope and catalog identities.

- [ ] Add synthetic Docling selection tests for no review, original-only review, wrong hash/receipt/source/path, partial/ranged review, missing table scope, superseded/missing event, and successful exact text/table review.
- [ ] Run targeted qualification tests; expect manifest format failure.
- [ ] Implement Docling-only `review_event_ids` validation and the strict qualification module.
- [ ] Load one review snapshot during build, pass it to rendition input validation and bind qualification policy/event IDs/state into rendition identity.
- [ ] Add and run review-correction freshness and prior-index-preservation tests; expect pass after freshness binding.
- [ ] Run targeted qualification suite; expect pass.
- [ ] Commit `feat: require exact review for Docling selection`.

### Task 4: Docling structural records and exact packets

**Files:**
- Modify: `tools/context-index/renditions.py`
- Modify: `tools/context-index/context_index.py`
- Modify: `tests/test_kit.py`
- Modify: `tools/context-mcp/README.md`

**Interfaces:**
- Consumes: Task 3 immutable qualification metadata and Docling `conversion.document/pages`.
- Produces: deterministic `#text-N` and `#table-N` sections with canonical JSON text and page/table source locations.

- [ ] Add failing discovery/get/get-many tests that assert stable locators, complete canonical item/table JSON, page/bbox/cell coordinates, source/rendition identities and qualification metadata.
- [ ] Add failure tests for malformed locator, missing page inventory/provenance, changed rendition/source and stale policy/index; assert prior index remains.
- [ ] Run targeted tests; expect no Docling records.
- [ ] Implement strict envelope/document/page/provenance validation and text/table record generation; do not synthesize textless-page records.
- [ ] Extend packet/search line-basis metadata for Docling while leaving existing rendition output unchanged.
- [ ] Run targeted Docling and GeminiTXT/ODT regression tests; expect pass.
- [ ] Commit `feat: index reviewed Docling text and tables`.

### Task 5: Package, verify, review and deliver

**Files:**
- Modify: `README.md`
- Modify: `ROADMAP.md`
- Modify: `release-files.json`
- Modify: `context/project.md`
- Modify: `context/status.md`
- Modify: `context/tools.md`

**Interfaces:**
- Consumes: Tasks 1-4 complete behavior.
- Produces: documented, packaged and criterion-mapped delivery.

- [ ] Update capability/boundary documentation and explicit release allowlist; regenerate schema snapshot only if DDL changed.
- [ ] Run extraction/context-index/review targeted tests, schema checks, clean-copy/package tests and `python3 -m unittest discover -s tests -v` in the installed environment; record any environment blocker exactly.
- [ ] Add Kata comments mapping each acceptance criterion to tests and commits; close only after verified completion.
- [ ] Inspect related Roborev feedback for every branch commit; reproduce relevant correctness findings with failing tests, fix in new commits and rerun verification.
- [ ] Request a fresh whole-branch code review and resolve Important/Critical findings under TDD.
- [ ] Commit `docs: document qualified rendition workflows` and any separately justified review-fix commits.
- [ ] Create the pull request with Kata links, strict qualification rule, evidence and limitations; register PR and Kata workspace targets when Forge target tools are available.
