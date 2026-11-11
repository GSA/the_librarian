# 0014 - Intake ledger writers hand off without re-validation and never wait unbounded

**Audience:** maintainers and operators.
**Date:** November 10, 2026.
**Status:** Implemented. Amends [0012](0012-bounded-validation-cost.md) and
[0013](0013-concurrent-intake-writers.md); amended by
[0018](0018-catalog-contention-outside-the-fault-budget.md).

## Context

[0013](0013-concurrent-intake-writers.md) lets several intake agents share one
instance. At production size (a ~110 MB ledger, ~30k requests) a cohort ingest
and a repair job running together collapsed: the ingest blocked in `flock` for
19+ minutes with no progress, the repair slowed by half and hit
`ledger_unavailable` three times. Run one at a time, both were healthy. Causes:

1. The "already validated" shortcut was per process, so with two alternating
   writers every lock acquisition ran the full O(ledger) check (`integrity_check`
   plus digest and shape of every request, stage and attempt).
2. The writer lock was a blocking `flock` with no timeout.
3. `progress` held one read transaction across the same full validation, so a
   writer's commit could exceed SQLite's default 5s busy timeout and surface as
   `ledger_unavailable` (a hard error rather than a retry).

## Decision

- **Attested hand-off.** A writer that finishes cleanly (no foreign commit while
  it held the lock) writes its file identity (device, inode, size, mtime, ctime,
  SQLite change counter) and bound root into `writer.lock`. The next writer, in
  any process, reads and truncates that record while holding the lock and skips
  full validation only when the live file still matches. A writer that crashes
  or fails leaves the file truncated, and any commit not followed by a fresh
  attestation changes the identity, so those cases validate in full. Validation
  cost per hand-off no longer depends on ledger size. Tamper detection is the
  same as before for every change that moves the identity; like the earlier
  per-process shortcut (added after 0012 without a decision record of its own), the
  attestation does not defend against an actor who
  rewrites the file and restores size, timestamps and change counter.
- **Bounded waits.** The writer lock is polled non-blocking with jitter for at
  most `LIBRARIAN_INTAKE_LOCK_WAIT_SECONDS` (default 30, max 3600; invalid values
  refuse as `invalid_lock_wait`), and the same value is the SQLite busy timeout.
  A timeout, or SQLite BUSY/LOCKED during a stage, raises `LedgerBusy` with the
  retryable code `ledger_busy`. Nothing was written, so retry is safe. The
  runner records it as a retryable attempt; `settle` and cohort ingest retry up
  to three times and then surface it (cohort marks only that entry failed, and
  the next run resumes it). The CLI prints `"retryable": true`.
- **Short readers.** `progress` reads rows inside a short read transaction and
  verifies digests and shapes after it ends (`store.snapshot` then
  `store.verify`, the same rules as `store.validate`), so it cannot hold a read
  lock across O(ledger) CPU work.

## Amendment: read-only refusals still hand off (Kata 2rye)

Cohort ingest probes for requests that do not exist yet; `store.inspect` then raises
`unknown_request` inside the locked block. That raise skipped the clean-unlock
attestation, so the next writer replayed the whole ledger: on a synthetic ledger of
36,000 requests, 61 full validations per 30 files (about 92% of ingest wall time,
2.25 s per file against 0.14 s on an empty ledger). A block that raises now also
hands off, but only when the file identity (device, inode, size, mtime, ctime,
change counter) equals the identity the writer validated on entry, so nothing
was written. A failure after a partial write, a foreign commit during the lock or a
crash still leaves no attestation and the next writer validates in full. Measured
with `tools/diagnostics/ledger_scale_benchmark.py`: 81 full validations fell to 1
for 40 files, and CPU for the same ingest fell from about 170 s to 79 s.

## Consequences

Writers taking turns validate once, not per hand-off. A starved writer reports
a retryable busy outcome within a known time. `ledger_unavailable` now means a
non-transient SQLite failure. A reader that is itself blocked by a writer still
reports `ledger_unavailable` on the progress page; that is a display state, not
a writer failure. The attestation adds no schema change.
