# 0010 - Daily store snapshots with bounded retention

**Audience:** maintainers and operators.
**Date:** November 2, 2026.
**Status:** Implemented. Supersedes the retention bullet in [0003](0003-store-compatibility.md).

## Context

Every catalog `submit` and review-event `append` took a full verified SQLite
snapshot before and after the commit, plus one on every idempotent retry, and
kept all of them. Cost grew with the square of the record count. In a
1,569-file cohort ingest (two catalog writes per file) a 12.7 MB catalog
accumulated 5,815 backups totalling 35 GB and ingestion slowed from about 1.8 s
to about 10 s per file. The append-only, hash-chained revision history already
records every write, so per-write full copies mostly duplicated it.

## Decision

1. **One snapshot per UTC day.** A write takes a verified `daily` snapshot,
   before it commits, only when the store has no intact snapshot dated the
   current UTC day. Store initialisation, manual `backup` and the restore
   safety snapshots (`pre-restore`, `post-restore`) are unchanged. Post-write
   and retry snapshots are removed; an idempotent retry takes none.
2. **One shared implementation.** `tools/backups.py` holds the daily gate,
   retention and inventory; the catalog and review-event stores supply only how
   to take and verify a snapshot.
3. **Seven days of good backups.** After a new daily snapshot verifies,
   snapshots, receipts and `.partial` leftovers older than the window are
   deleted. Only verified snapshots count as good: nothing is deleted unless a
   verified snapshot remains inside the window, and a failed verification of the
   new snapshot deletes nothing. Manual and restore snapshots follow the same
   window. The window is a versioned default of 7 days with an optional
   `backup_retention_days` override in `config/policy.local.json`; values below 1
   are refused. A retention failure never fails the write that triggered it.
4. **Explicit prune for existing instances.** `python3 -m tools.backups prune`
   is a dry run by default; `--apply` keeps the newest verified snapshot per UTC
   day in the window and deletes the rest. `python3 -m tools.backups status`
   reports body-free count, size, newest, oldest and oldest retained revision.
5. **Result contract.** `submit` and `append` return `backup` (the daily
   snapshot taken, or `null`) and `retention` (the prune report, or `null`);
   `pre_backup` is removed.

## Trade-off

Recovery between snapshots relies on SQLite `synchronous=FULL` durability and
the hash-chained history. The worst case is restoring the day's first snapshot
(the state before that day's first write) and replaying the day's requests by
their original request IDs. Operators who need a tighter loss window run
`backup` more often, or raise the retention window.

## Not decided

- Offsite copies and scheduled `backup` runs.
- Snapshots of the intake ledger and the rebuildable context index, which are
  unchanged.
