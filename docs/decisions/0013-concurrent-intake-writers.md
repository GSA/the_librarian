# 0013 - Concurrent intake writers retry and rebase instead of blocking

**Audience:** maintainers and operators.
**Date:** November 9, 2026.
**Status:** Implemented. Amended by
[0014](0014-intake-ledger-handoff-and-bounded-waits.md) (bounded ledger waits) and
[0015](0015-trusted-head-catalog-commit.md) (the catalog lock hold is no longer
O(catalog)).

## Context

Two intake agents running against one instance raced for the catalog. Each
registration pins `expected_state` when staged; the writer lock refused
immediately; SQLite readers used `timeout=0`. Every lost race was classified as
`blocked native-input`, indistinguishable from bad input, and needed manual
replay. On one instance every blocked upload/register stage was such a race.

## Decision

- The catalog raises typed errors: `StaleState` (pin outdated), `WriterBusy`
  (lock still held after a bounded 10s wait across processes; same-thread
  re-entry fails at once) and `ConflictingWrite`. Readers wait up to 10s for an
  in-progress commit.
- `submit(..., rebase=True)` accepts an outdated pin from this catalog's history
  when no later revision wrote any record in the request, and still validates
  the full graph against current state. Idempotency is checked first, so a
  retry with the same request ID cannot duplicate a write. Intake registration
  stages use it; overlapping writes raise `ConflictingWrite`.
- Intake maps `StaleState`/`WriterBusy` to retryable `catalog-stale-state` and
  `catalog-busy`, and `ConflictingWrite` to blocked `catalog-conflict`. Upload
  and cohort ingest run retryable stages to settlement within the request's
  attempt budget; cohort reports retried conflicts separately from failures.
- `intake recover-conflicts [--apply]` replays legacy blocked registrations.

A single-writer queue was rejected: it adds a daemon and ordering state for a
problem optimistic retry resolves with existing request identities.

## Consequences

Agents can share an instance; throughput is still serialized by the catalog
lock. Review-store writes keep their non-blocking lock.
