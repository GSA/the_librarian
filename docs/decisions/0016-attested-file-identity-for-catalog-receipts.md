# 0016 - Catalog validation receipts bind to file identity, with a daily full check

**Audience:** maintainers and operators.
**Status:** Accepted by the owner. Amends [0012](0012-bounded-validation-cost.md),
[0014](0014-intake-ledger-handoff-and-bounded-waits.md) and
[0015](0015-trusted-head-catalog-commit.md).

## Context

The receipt in `.local/read-validation.json` (0012 for readers, 0015 for the writer)
bound a full validation to `SHA-256(connection.serialize())`. Every validated read
and up to three points per writer commit recomputed it: about 84 ms at 25.6k
registrations (73 MB) and about 120 ms on a 97 MB production catalog, per call, with
CPU growing in step with the number of reader processes. Decision 0014 already binds
the intake ledger hand-off to file identity.

## Decision

- **Identity binding.** `_binding` is now `{version: 2, identity: [device, inode, size,
  mtime_ns, ctime_ns, SQLite change counter], validator_sha256, runtime}`, taken after
  the connection holds its read snapshot. The identity code is `tools/file_identity.py`,
  moved unchanged from the ledger (`tools/intake/store.py` imports it). When a journal
  or WAL exists or the header is not rollback-journal, the binding is the previous
  byte hash (`version: 1`).
- **Daily full check.** The receipt records `validated_on`, the UTC date of the last
  full validation it descends from. An identity-bound receipt is trusted only on that
  date; the first validated read or write of the next UTC day replays all history and
  rewrites it. A delta commit carries the date forward; only a full validation (or the
  daily snapshot's verification) advances it.
- **Unchanged.** `status --audit`, the daily snapshot, `verify_backup`, `restore`,
  backups, the schema and every refusal stay byte-exact and full. A receipt written
  before this decision has no date and is a miss (one full validation), never a refusal.

## Accepted residual

The byte hash saw two things the identity does not; they are now seen at the next
daily full check or by `status --audit`, whichever is first, so a window of at most one
UTC day:

- silent media corruption with no write through the operating system;
- an edit that restores size, mtime, ctime and the change counter (root changing the
  clock, writing the block device, or a same-timestamp-tick edit on a coarse-timestamp
  filesystem).

Everything else still refuses at the next read or write: edits through SQLite, raw
same-size `pwrite` edits (even with mtime restored, since `utime` moves ctime), a
replaced file, a restored older file. An actor who rewrites the database and the
unkeyed receipt together is accepted for one write, as in 0015.

## Consequences

A validated read no longer scales with the catalog: 84 to 0.8 ms at 25.6k
registrations, and three concurrent readers use about 0.1 CPU s rather than 5.5 s per
20 reads each. Writer hold at 25.6k fell from about 413 to 138 ms in the same
synthetic runs; a 400-file synthetic ingest used about 12 s less CPU with identical
`records_sha256`. Timings are from a shared, noisy host. The first validated read of
each UTC day pays a full replay (about 4 s at 25.6k registrations), possibly once per
process before one rewrites the receipt.
