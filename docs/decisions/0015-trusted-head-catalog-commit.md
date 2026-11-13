# 0015 - Catalog writers validate only their delta on a trusted head

**Audience:** maintainers and operators.
**Date:** November 11, 2026.
**Status:** Implemented. Amends [0012](0012-bounded-validation-cost.md) and
[0013](0013-concurrent-intake-writers.md). Amended by [0016](0016-single-replay-daily-snapshot.md)
(the first write of a UTC day) and [0017](0017-attested-file-identity-for-catalog-receipts.md), which
replaces the whole-file SHA-256 binding described below with a file-identity binding and a daily
full check.

## Context

[0012](0012-bounded-validation-cost.md) removed the quadratic terms but left every
catalog write O(catalog): `submit` ran a full `inspect` (decode, validate, re-hash
every revision) before the snapshot check and a second one under `BEGIN IMMEDIATE`.
[0013](0013-concurrent-intake-writers.md) accepted that "throughput is still
serialized by the catalog lock" without measuring it. Measured on synthetic
catalogs (`tools/diagnostics/catalog_lock_benchmark.py`; each registration is four
records), 93-97% of the lock hold was those two replays:

| Registrations | Hold before | `inspect` x2 | Hold after |
|---|---|---|---|
| 400 | 92 ms | 69 ms | 18 ms |
| 1,600 | 324 ms | 302 ms | 38 ms |
| 6,400 | 1,318 ms | 1,265 ms | ~110 ms |
| 25,600 | 5,727 ms | 5,525 ms | 330 ms |

In a 400-file cohort ingest the catalog lock was held 43% of the wall time. The
linear, hash-chained history is the fundamental constraint; the work done inside it
was not.

## Decision

Keep one linear revision chain, `state_token` pins, rebase and every existing
refusal. Change only how a writer proves the head is sound:

- **Trusted head.** The writer computes the same binding readers use (SHA-256 of the
  exact database bytes, validator-file hash, Python/SQLite/Unicode versions) and
  consults `.local/read-validation.json`. On a match, the state comes from the
  receipt (and must equal the state re-derived from the database head); otherwise
  the writer runs the existing full `inspect`. An unreadable or invalid receipt only
  costs the full replay; it never blocks a write.
- **Delta validation.** `contracts.validate_delta` refuses exactly what
  `validate_graph` would refuse on the committed records overlaid with the batch,
  looking only at the batch, referrers of rewritten records, moved path claims and
  parent chains from changed artifacts, through indexed SQL lookups. A randomized
  equivalence test compares its accept/refuse decision with `validate_graph`.
- **Revision facts from SQL.** Idempotency (`request_id` lookup, `input_hash` still
  compared), the rebase pin lookup (walk back from the head over revision hashes and
  first-write counts) and the "later revision wrote this record" check use indexed
  queries instead of a history replay.
- **Re-check under `BEGIN IMMEDIATE`** compares the database binding with the one
  validated, instead of a second full `inspect`.
- **Attestation.** After a successful commit the writer writes the receipt for the
  new bytes (best effort). A crash after COMMIT leaves the old receipt, which no
  longer matches, so the next reader or writer validates in full and the retry is
  idempotent.

Unchanged: the daily snapshot (it verifies the snapshot with a full `inspect`, so the
first write of each UTC day always replays the entire history and refuses on
corruption), retention, the append-only triggers, `status --audit`, backup verify,
restore, the schema, and the decision not to add a single-writer queue.

## Not adopted

- **Batching many registrations per revision.** At a 1,600-registration catalog the
  commit ceiling is about 10 registrations/s with one writer (about 2 before),
  so batching (65 and 117 registrations/s at batch 4 and 16) is not needed for the
  observed rate of a few files per minute, and it would change idempotency (one
  request ID per many items), partial-failure and rebase semantics. Revisit only if
  commits approach the ceiling.
- **Moving pure work before the lock.** Validation, canonicalization and the input
  hash already run before the lock.

## Consequences

- Lock hold falls 8-17x at 1.6k-25.6k registrations. A 400-file synthetic ingest
  went from 193 s to 90 s wall and 144 s to 39 s CPU, with identical
  `records_sha256` and outcome totals. The catalog lock share fell from 43% to 21%,
  so a parallel intake front end can scale further.
- Remaining cost grows linearly through hashing the database file (about 80 ms for
  73 MB, done at entry, at the re-check and at attestation).
- **Residual risk.** The receipt is unkeyed, like the reader receipt in
  `catalog_store.validated_state` (which predates these records and has none of its
  own) and the ledger hand-off in 0014. An actor who rewrites the database and the
  receipt so that they agree can get one write accepted without a replay. The next daily snapshot,
  `status --audit` and backup verification still replay all history and refuse. This
  is not a defense against that actor.
- The writer now writes the reader receipt; both need a private `.local/` directory
  under the catalog home, as before.

## Reproduce

    python3 tools/diagnostics/catalog_lock_benchmark.py --sizes 100,400,1600 --samples 5
    python3 tools/diagnostics/catalog_commit_ceiling.py --size 1600 --workers 1,2,4
    python3 tools/diagnostics/ingest_benchmark.py --sizes 100,200,400

Synthetic fixtures only; never point them at a live instance. Timings come from a
shared host and are noisy; compare shapes.
