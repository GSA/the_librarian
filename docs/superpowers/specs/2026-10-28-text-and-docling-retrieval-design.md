# UTF-8 rendition and selected Docling retrieval design

**Audience:** maintainers, operators and implementation/review agents.
**Date:** October 28, 2026.
**Status:** Owner-approved October 28, 2026.
**Katas:** `8a5h`, `zqdh`.
**Planning home:** [development and ingestion roadmap](../../../ROADMAP.md).

## Intent and boundaries

Add two bounded capabilities without changing the authority model:

1. ordinary `.txt` originals can use the existing private extraction and catalog-registration lifecycle; and
2. an explicitly selected, substantively reviewed Docling rendition can enter the qualified reader while retaining exact structural provenance.

This work performs no acquisition, real ingestion, model execution, operational review, selection, store migration or publication. Tests use temporary stores and synthetic or toolkit text. Decoding and conversion remain distinct from review, and review remains distinct from explicit selection.

The Docling qualification rule is deliberately isolated behind a versioned seam. Later workflows may call qualification at a different stage, but no alternate workflow may silently weaken an index built under this strict policy.

## Ordinary UTF-8 text

Extend `tools/extraction/extract.py` with explicit format `text`. It reuses receipt/original resolution, deterministic run identity, private non-replacing publication, repeat verification, saved registration/completion and the supported catalog writer.

Only `.txt` receipt originals are supported. Decode strictly as UTF-8. Accept and record an optional UTF-8 BOM; reject UTF-16/UTF-32 BOMs, malformed UTF-8, NUL bytes and unsupported encodings without guessing or replacement.

The extraction payload retains source identity and one record per physical line. Each record includes its one-based line number, exact decoded content including the original terminator, terminator kind (`lf`, `crlf`, `cr` or `none`) and half-open byte offsets in the original. The payload records BOM presence/length and whether the source ends in a terminator. No line-ending normalization occurs, so blank lines, Unicode and final-newline semantics map directly to original bytes.

A zero-byte or BOM-only file may be faithfully prepared and registered but cannot be selected for retrieval because it has no records. Text records use stable `#line-N` locators. A registered child stays deferred until a supported selection writer explicitly marks it indexed and places it in the manifest.

## Selected Docling qualification

A Docling selection uses the existing explicit rendition entry with `format: "docling"` and a nonempty `review_event_ids` list. Existing GeminiTXT and ODT entries remain unchanged.

A selected Docling rendition qualifies only when configured active, non-superseded review events collectively satisfy the following strict rule:

- receipt and source identity match the registered original;
- the review locator is the exact selected `extraction.json` path and its parent locator is the original path;
- the reported checksum equals the selected rendition SHA-256;
- outcome is `reviewed`;
- scope is file-wide `defined_scope`, not selected ranges;
- content includes `text`, and includes `tables` whenever the rendition contains tables;
- reviewer, review date and attributed evidence satisfy the existing stored review-event contract; and
- every configured event ID exists, is active and belongs to this qualification.

Selection, registration or conversion never creates or chooses a review event. A review of only the original source is insufficient because it does not prove inspection of the exact machine rendition.

Qualification consumes the validated selection, Docling envelope, catalog identities and one active review snapshot. It returns immutable qualification metadata or fails closed. Its version, event IDs and review-store state token become index freshness inputs.

## Docling records and exact packets

The retained Docling JSON remains authoritative; no Markdown copy is created. Index one record per usable textual item and one record per complete table. Do not invent records or claims for textless pages.

Use deterministic ordinal locators `#text-N` and `#table-N`. Each record preserves its JSON pointer or Docling `self_ref`, item kind, page number, bounding box and coordinate origin. Tables additionally preserve complete table structure and cell row/column coordinates. Search returns the same locator accepted by exact reads. Exact reads return complete canonical JSON for the selected item or table, never reconstructed prose.

Every referenced page must exist in the conversion page inventory. Missing or malformed page/table provenance fails the selected rendition closed rather than producing an unknown or invented page claim.

Packets retain source and rendition hashes, receipt/artifact/source IDs, extraction warnings/omissions/confidence/profile, selected review identities and reviewed scope. Search excerpts remain discovery-only.

## Freshness and failure behavior

Build and read checks retain manifest, policy, engine, catalog/source/rendition and original-content freshness. Docling additionally binds the review snapshot. A changed source, rendition, policy, selection, review state or engine makes an old index stale. A failed build preserves the prior database.

Unselected or unreviewed renditions are never retrievable. Malformed locators, envelopes, provenance, review bindings and page references fail visibly. Existing GeminiTXT/ODT serialization, locators and packets remain unchanged.

## Packaging and verification

Reusable implementation, tests, docs and any new qualification module are explicit release files. Operational data, source documents, model assets and private configuration remain excluded.

Verification maps directly to both Kata criteria: line/BOM/encoding/lifecycle failures for `8a5h`; selection/review/source/rendition/freshness/locator and text/table round trips for `zqdh`; regression tests preserve GeminiTXT/ODT behavior. Final work receives Roborev feedback review and a pull request only after verified completion.
