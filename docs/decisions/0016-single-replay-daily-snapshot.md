# 0016 - The first write of a UTC day replays the history once

**Audience:** maintainers and operators.
**Date:** November 11, 2026.
**Status:** Implemented. Amends [0010](0010-daily-snapshots-and-retention.md) and
[0015](0015-trusted-head-catalog-commit.md); leaves [0013](0013-concurrent-intake-writers.md)'s linear chain unchanged.
Amended by [0019](0019-cheaper-catalog-inspect.md) (the replay it relies on is cheaper).

## Context

After [0015](0015-trusted-head-catalog-commit.md) a normal catalog write validates only its
delta, but the first write of each UTC day takes the daily snapshot inside the writer lock, and
that ran **four** full `inspect` replays: the live head, the backup-API copy, the fresh-snapshot
verification and the retention pass's verification of the same newest snapshot (five when the
writer had no receipt). Measured on synthetic catalogs
(`tools/diagnostics/catalog_daily_replay_benchmark.py`; each registration is four records;
shared host, so compare shapes):

| Registrations | DB | First-of-day hold before | After | Same-day write |
|---|---|---|---|---|
| 6,400 | 18 MB | 3.1 s | 1.2 s | 0.16 s |
| 25,600 | 73 MB | 12.9 s | 4.6-4.8 s | 0.42 s |
| 100,000 | 287 MB | 53-58 s | 19-21 s | 1.5 s |

Before this change the hold passed the 10 s `WriterBusy` wait at about 20k registrations. With
three writers, other writers lost a wait at 25.6k (one `catalog-busy` each) and at 100k lost five
each, more than a stage's three attempts. After it, no writer loses a wait at 25.6k and at
100k each loses one or two. A body-free read of a production catalog (63k records, 22.5k
revisions, 92 MB) put one full `inspect` at 4.4 s on a loaded host.

## Decision

Keep the gate, the moment, the retention window and the full replay of the live head; remove only
replays of bytes that were already proven identical.

- **The daily snapshot is the serialized image of the transaction that was just fully inspected**
  (`connection.serialize()` equals the file bytes). The writer writes it to the `.partial`, fsyncs
  it, re-reads it and refuses (`Backup bytes mismatch`, nothing published) unless it hashes to the
  image. The backup-API copy and its second `inspect` are dropped for this path only. When
  `submit` has just replayed the history itself (no receipt), the snapshot does not replay it
  again. Manual `backup`, `init` and restore safety snapshots keep the independent copy and
  inspect.
- **Fresh verification checks the receipt, not a third replay.** `verify_backup(path, replay=False)`
  still compares fields, size, SHA-256 and the receipt state; the default `replay=True` is
  unchanged for restore, `verify-backup`, audit, operator prune and older snapshots.
- **Retention does not re-verify the snapshot that was just verified** (`prune(..., verified=...)`).
  Every older snapshot is still verified before it can count as a keeper; nothing is deleted
  unless a verified snapshot remains in the window, and a failed fresh verification deletes
  nothing.

## Unchanged

Schema, chain, `state_token`, idempotency, rebase, the daily-gate rule (no intact snapshot of the
store for the UTC day), snapshot format, loss-window semantics, the full replay of the live head
at the first write of each day and in `status --audit` (a corrupt catalog, including one behind a
forged receipt, still refuses), and crash behaviour (`.partial`, fsync, link, receipt, then
commit).

## Not adopted

- **Snapshot before the lock or in a periodic job.** A reader on a connection blocks every commit
  for its whole replay, so a pre-lock replay needs a serialized image, and every writer would still
  wait for it (or write without the snapshot, weakening the first-write check). An operator may
  already run `backup` off-peak (a manual snapshot satisfies the daily gate); it holds the same lock
  and drops the first-write check, so it is not made a product path.
- **A configurable catalog wait.** The ledger has one (0014). Revisit when the first-of-day hold
  nears 25 s (about 120k registrations of this shape).
- **Changing the hash chain.** See the analysis in Kata the_librarian#yxc2: the chain costs about 45 ms
  per commit independent of size and is not the limiting cost; sharded chains, a Merkle structure or
  multi-host writers would change `state_token`, snapshots and idempotency. Revisit when commit
  utilization is sustained above roughly a third of the ceiling with writers losing waits, when
  multi-host writers are required, or when the daily hold exceeds the wait even after this change,
  which calls for a checkpointed audit over the same chain.

## Consequences

- The residual cost is one `inspect` (about 0.7 s per 6.4k registrations, 1.8 GB peak memory at 100k)
  plus writing one image of the database; the image is one more copy of the database in memory.
- The snapshot bytes are the inspected bytes, bound by hash; they rely on the same
  `serialize()`-equals-file assumption 0015's binding already makes.

## Reproduce

    python3 tools/diagnostics/catalog_daily_replay_benchmark.py seed --dir D --size 25600
    python3 tools/diagnostics/catalog_daily_replay_benchmark.py profile --dir D --samples 3
    python3 tools/diagnostics/catalog_daily_replay_benchmark.py contend --dir D --writers 3

Synthetic fixtures only; never point it at a live instance. The tool shifts only the in-process
UTC clock, so no waiting is needed.
