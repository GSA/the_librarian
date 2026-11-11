# 0020 - Upload planning reads the highest ID per prefix, not every ID

**Audience:** maintainers and operators.
**Status:** Implemented. Amends [0012](0012-bounded-validation-cost.md); the ledger trust model of
[0014](0014-intake-ledger-handoff-and-bounded-waits.md) and the catalog receipts of
[0015](0015-trusted-head-catalog-commit.md) and [0017](0017-attested-file-identity-for-catalog-receipts.md)
are unchanged.

## Context

Planning one submission (`upload.records_for`) allocates catalog IDs, so it must run serially, and it
was O(catalog + ledger) per file: it materialized every current record ID in Python
(`SELECT id FROM catalog_current`), decoded the JSON of every `upload-*` request in the ledger to
collect the IDs they promise, and scanned the union for the largest `S` and `D` number. Measured on
synthetic instances (`tools/diagnostics/upload_planning_benchmark.py`, a 36,000-request ledger, median
of 5, shared host):

| Catalog registrations | `records_for` before | of which `reserved_ids` | of which ID scan |
|---|---|---|---|
| 1,600 | 34 ms | 29 ms | 2 ms |
| 6,400 | 118 ms | 105 ms | 11 ms |
| 25,600 | 516 ms | 444 ms | 50 ms |

In a serial `cohort ingest` at 25,600 registrations planning was about 44% of an item (507 of
1,160 ms), and it was the one per-item term that kept growing with history.

## Decision

Change how the same answer is computed; keep the allocation rule and every check.

- **Highest number from the index.** The catalog side reads `max(CAST(substr(record_id,2) AS INTEGER))`
  over the `S` and `D` ranges of the `current_records` primary-key index instead of every ID, and probes
  only the three submission-specific IDs (and a correction parent's records) with an indexed `IN`
  lookup. IDs whose digit run is too long for SQLite's integers fall back to the exact Python rule over
  the same indexed range, so the result equals the old scan for every current ID.
- **Reserved IDs memoized by request digest.** `reserved_ids` still reads every `upload-*` row under the
  ledger lock, whatever the stage status, so a staged, retrying, blocked or dead-lettered request keeps
  its IDs reserved and another process's request is seen at once. It decodes a request only when the
  stored `request_sha256` is not in a per-process, bounded (65,536 entries), never-persisted memo, and
  checks that digest against the row's bytes before memoizing it. The memo holds each request's promised
  IDs and its highest `S`/`D` number.
- No allocation, naming, schema or catalog-format change; no new table, column or index; no migration.

## Results

Identical output, faster per file (`ledger_scale_benchmark.py --matched --warm`, 36,000-request ledger,
40 new files, `ids_sha256` and outcome totals equal between trees at every size):

| Catalog registrations | s/file before -> after | `records_for` ms/file before -> after | CPU s before -> after |
|---|---|---|---|
| 1,600 | 0.43-0.48 -> 0.39-0.49 | 40 -> 18 | 5.9 -> 5.1 |
| 6,400 | 0.61 -> 0.51-0.55 | 126 -> 35 | 11.4 -> 7.7 |
| 10,500 | 0.70 -> 0.52-0.56 | 203 -> 50 | 16.0 -> 9.9 |
| 25,600 | 1.09-1.16 -> 0.78 | 490 -> 96 | 33 -> 18 |

The first `reserved_ids` call in a process still decodes the whole backlog (about 1.8 s at 25,600
registrations, as one old call did) and the memo then holds about 80 MB at that size; every later call
costs about 50 ms (a row listing, no decoding).

## Trust and residual

The memo keys on the digest the ledger already stores. A hit is correct for any row carrying that
digest; a row whose bytes no longer match its digest is refused when the memo is not yet warm for it and
by the ledger's full validation whenever the writer lock is taken without a valid hand-off attestation
(0014). As in 0014, a raw edit that preserves the file identity and change counter, or a digest-preserving
rewrite, is not seen by a warm process before the next full validation; the only effect is a stale
reservation for IDs the catalog itself validates, since planning is advisory to the catalog's own
`expected_state`, rebase and conflict checks.

## Not adopted

- **Incremental reservations by rowid or an indexed column.** That makes the remaining row listing
  O(new rows), but it trusts unvalidated state or adds a column that needs a migration and schema
  snapshot; the listing costs about 50 ms at 36,000 requests.
- **Planning several inbox items per view.** Allocation is already cheap enough that amortizing it would
  change the ledger request/idempotency shape for little gain.

## Reproduce

    python3 tools/diagnostics/upload_planning_benchmark.py --catalog 1600,6400,25600 --ledger 36000
    python3 tools/diagnostics/ledger_scale_benchmark.py --ledger 36000 --catalog 25600 --files 40 --matched --warm --profile

Synthetic fixtures only; never point them at a live instance. Timings come from a shared host.
