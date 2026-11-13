# 0012 - Bounded validation cost after daily snapshots

**Audience:** maintainers and operators.
**Date:** November 2, 2026.
**Status:** Implemented. Follows [0010](0010-daily-snapshots-and-retention.md).
Amended by [0014](0014-intake-ledger-handoff-and-bounded-waits.md) (ledger),
[0015](0015-trusted-head-catalog-commit.md) (catalog),
[0017](0017-attested-file-identity-for-catalog-receipts.md) (receipt binding),
[0019](0019-cheaper-catalog-inspect.md) (cheaper `inspect`) and
[0020](0020-constant-cost-upload-planning.md) (upload planning); the *Result and remaining
cost* section describes the state before them.

## Context

Decision 0010 removed per-write backups. Throughput still fell as a cohort
grew, so the remaining cost was measured rather than assumed. Evidence comes
from `tools/diagnostics/ingest_benchmark.py`, which ingests deterministic
synthetic markdown files into a temporary instance and reports body-free
timings, CPU, backup count and bytes, p50/p95 per-file latency and record
digests. No real instance or corpus is read.

Three trees ran on identical fixtures: `base` (the writer before 0010, rebuilt
by reverse-applying that change), `main` (0010 plus cohort intake) and `fix`
(this decision). Each size ingests half the files, then resumes over all of
them (already-ingested files take the idempotent path). Runs shared a 14-core
host and the 400-file runs overlapped other work, so compare shapes rather
than third-digit values. The tables were recorded before the harness restored its
timing wrappers between sizes, so later sizes carry a small, equal-per-tree
wrapper overhead; the harness now restores them.

| Tree | Files | Ingest wall (s) | CPU (s) | Ingest p50 / p95 (ms) | Resume wall (s) | Resume p50 / p95 (ms) | Resume first→last decile (ms) | Backups |
|---|---|---|---|---|---|---|---|---|
| base | 100 | 29 | 17.5 | 550 / 999 | 74 | 1060 / 1873 | 78→1873 | 401 / 194 MB |
| base | 200 | 103 | 72.8 | 1057 / 1766 | 303 | 1716 / 4059 | 132→4022 | 801 / 737 MB |
| main | 100 | 18 | 10.2 | 367 / 523 | 43 | 489 / 1008 | 70→976 | 1 / 45 KB |
| main | 200 | 56 | 40.3 | 526 / 989 | 166 | 914 / 1919 | 143→1869 | 1 / 45 KB |
| main | 400 | 205 | 170.4 | 954 / 2073 | 828 | 1906 / 5616 | 249→5558 | 1 / 45 KB |
| fix | 100 | 22 | 8.7 | 414 / 691 | 37 | 558 / 842 | 43→721 | 1 / 45 KB |
| fix | 200 | 56 | 27.4 | 597 / 792 | 105 | 676 / 1194 | 63→1168 | 1 / 45 KB |
| fix | 400 | 154 | 96.2 | 752 / 1182 | 364 | 1133 / 2071 | 128→2084 | 1 / 45 KB |

Disposition totals (`extracted`/`received`/`not-processed`) and the catalog
`records_sha256` are identical between `main` and `fix` at every size.

Findings:

- Snapshots were the dominant cost only before 0010 (hundreds of copies, up to
  737 MB at 200 files). After it the backup cost is one daily copy.
- Removing snapshots did **not** make writes constant-time. On `main`, per-file
  latency still rose roughly 3x over the first-to-last decile and 400-file
  resumes ran 828 s. Profiling shows full-history validation on every
  operation: `catalog_store.inspect` (about 590 of 828 s at 400 files) and the
  intake ledger check `store.validate` (re-decoding every saved request on every
  lock acquisition).
- Inside `inspect`, `validate_graph(current)` ran after every historical
  revision, which is quadratic in the number of records.

## Decision

Make the smallest changes that remove the measured quadratic terms and keep
every check:

1. **Incremental graph replay.** `contracts.GraphValidator` replays revisions
   and, per revision, checks only the written records, their referrers, moved
   path claims and parent chains from changed artifacts. It raises the same
   refusals as `validate_graph` (unit tests compare both on valid and invalid
   histories). `inspect` still decodes and validates every payload, recomputes
   every payload, input and chain hash, and compares the current projection, so
   tamper and stale detection are unchanged. `submit` keeps the full-state
   `validate_graph` on the resulting state.
2. **Request-shape memoization.** `intake.contracts.request_shape` memoizes
   the validated identity (correlation ID, correction parent, stage IDs) by the
   exact request bytes. The ledger check still hashes each row, compares the
   root and identity, and verifies every stage, artifact, attempt and replay
   row; it only skips re-parsing bytes already proven valid. A rewritten
   request is a different key and is refused. Failures are never cached.

   *Amendment (Kata 2vfn):* the fixed-size LRU caches (16,384 / 32,768 /
   65,536 entries) were smaller than a production ledger (36,309 requests,
   72,895 artifacts), so the in-order walk of `store.verify` never hit them
   (about 0.9 s per pass). `verify` now keeps one memo of the previous
   successful pass, keyed by the exact request bytes and artifact text and
   recording the digest they were proven to have, and replaces it with the rows
   it has just verified. A hit needs identical bytes and an identical stored
   digest, so a rewritten row, or a forged digest on unchanged bytes, is
   re-hashed, re-decoded and refused as before. Failures are never cached, the
   memo follows the live ledger rather than history, and it retains at most
   128 MiB of keys (`store.MEMO_BYTES`); rows beyond that are re-verified. A
   repeat pass at 36,309 requests takes about 0.14 s
   (`tools/diagnostics/ledger_verify_benchmark.py`). Attestation and trust
   model are unchanged.

Snapshots, writer locks, the post-snapshot `BEGIN IMMEDIATE` re-inspection,
append-only history, idempotent request identities and daily-backup loss-window
semantics are unchanged.

## Result and remaining cost

The 400-file resume dropped from 828 s to 364 s (ingest 205 s to 154 s) and
catalog `inspect` from about 590 s to about 180 s at that size, with identical
records. Cost per file still grows with the corpus because `inspect`, the
ledger check and the `validated_state` snapshot hash remain linear in history,
and `serialize()`/payload decoding are untouched. At 400 files, last-decile
latency is still about 16x the first decile on resumed runs. Further reduction
needs a design that preserves the same binding, for example an
operation-local validated snapshot bound to the SQLite file hash, and is not
part of this change.

## Reproduce

    python3 tools/diagnostics/ingest_benchmark.py --sizes 100,200 --resume --label fix

Compare trees with `--tree`. Synthetic fixtures only; never point it at a live
instance.
