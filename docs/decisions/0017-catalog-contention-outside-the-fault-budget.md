# 0017 - Lost catalog races do not spend an intake stage's fault budget

**Audience:** maintainers and operators.
**Date:** November 12, 2026.
**Status:** Implemented. Amends [0013](0013-concurrent-intake-writers.md) and
[0014](0014-intake-ledger-handoff-and-bounded-waits.md).

## Context

Registration stages (upload and cohort) carry `retry: {max_attempts: 3, backoff_seconds: 0}`.
[0013](0013-concurrent-intake-writers.md) made a lost catalog race a retryable attempt
(`catalog-busy` after the catalog's 10 s `WriterBusy` wait, `catalog-stale-state` after a
newer commit), and each such attempt consumed one of the three. Such an attempt wrote nothing
(like a busy ledger, which [0014](0014-intake-ledger-handoff-and-bounded-waits.md) retries
without charging the stage), yet three of them sent a stage to `dead-letter` with no input
fault. Before [0016](0016-single-replay-daily-snapshot.md) that happened at 100k registrations
(a 58 s first-of-day hold is five lost waits); after it the first-of-day hold fits, but any
other long catalog hold (a restore, a manual backup, a slow disk, more concurrent writers)
can do the same.

Measured with `tools/diagnostics/catalog_contention_budget.py` (a stage under `runner.settle`
while another process holds the catalog writer lock; the 10 s wait is scaled to 0.3 s, so holds
are in multiples of the wait; synthetic, shared host):

| Lock held for | Before | After |
|---|---|---|
| 0.5 waits | succeeded, 1 attempt | same |
| 2.5-3.5 waits | 2.5: succeeded after 2 lost waits; 3.5: **dead-letter**, 3 attempts | succeeded after 2 lost waits |
| 6.5 waits | **dead-letter**, 3 attempts | succeeded after 5 lost waits |
| 13-25 waits | **dead-letter**, 3 attempts | `retryable` after 6 lost waits (one `settle` call), resumes on the next call |

A real first-of-day write on a 6,400-registration seed (`catalog_daily_replay_benchmark.py`, the
wait scaled to 0.3 s) dead-lettered a contending stage in 2 of 2 trials before, and settled in 2 of
2 after. The live production ledger has no `catalog-busy` or `catalog-stale-state` outcome and no
stage dead-lettered by contention (counts of closed-vocabulary outcome codes only); its only
retryable failure code, `sqlite-operational` (435 attempts), is not a catalog race and is
unchanged.

## Decision

- **A lost catalog race is not charged to `max_attempts`.** A retryable attempt coded
  `catalog-busy` or `catalog-stale-state` is counted separately; `max_attempts` bounds every
  other attempt. Input faults (`native-input`, `catalog-conflict`, `policy-changed`, a changed
  source or extractor), transient OS or SQLite errors and every permanent failure are charged and
  block or dead-letter exactly as before, whether or not races occurred between them.
- **The contention budget is separate and bounded.** One cycle of a stage records at most
  `CONTENTION_BUDGET` (30) lost races, then the next one dead-letters like any exhausted stage
  (5 minutes of lost 10 s waits), so a lock stuck forever cannot retry forever or grow the ledger
  without limit. `replay` and `recover-conflicts --apply` resume it as before.
- **One `settle` call stops after `CONTENTION_RETRIES` (6) consecutive lost races** (about 60 s of
  waiting at the default wait, longer than the 58 s hold measured before 0016) and returns the
  stage as `retryable`, as it already does for a long backoff. The next call (the next cohort run,
  upload submission or `run`) resumes it. Nothing waits unbounded; no sleeping scheduler is added.
- **No backoff is added**: the catalog wait is already the delay, and a stale pin is re-read at
  once.

Rejected: leaving it as is (a non-fault dead-letter, and `recover-conflicts` exists only to undo
it); making the catalog wait follow the ledger's 30 s (a longer stall for the same lost attempts;
tracked separately as a configurable catalog wait); a per-stage `max_attempts` bump (changes the
request shape and the request digest of every staged request).

## Unchanged

The ledger schema and request shape (the attempt rows already carry `category` and `code`),
request IDs and idempotency, `rebase`, `ConflictingWrite` (blocked `catalog-conflict`),
`recover-conflicts` candidates and semantics, the 0014 bounded ledger wait and `LedgerBusy`
retry, and `conflicts()` reporting (cohort `retried_after_conflict` counts the same attempts).

## Consequences

- A stage that cannot commit for a non-fault reason settles when the lock frees or stays visibly
  `retryable`; real input faults still block. A stage can now show more than `max_attempts`
  attempts, all but `max_attempts` of them lost races; progress reports them by code.
- A stuck catalog lock now takes 30 recorded attempts, not 3, to dead-letter one stage; per
  stage that is bounded ledger growth (30 small rows).

## Reproduce

    python3 tools/diagnostics/catalog_contention_budget.py --wait 0.3 --holds 0.5,2.5,3.5,6.5,13

Synthetic fixtures in a temporary workspace only. Compare shapes, not wall seconds.
