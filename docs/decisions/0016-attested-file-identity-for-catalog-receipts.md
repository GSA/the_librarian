# 0016 - Bind catalog validation receipts to file identity, with a daily full check

**Audience:** maintainers, operators and the owner who approves it.
**Status:** Proposed. Not accepted and not implemented: it weakens a trust
guarantee, so it needs explicit owner approval first. If accepted it amends
[0012](0012-bounded-validation-cost.md), [0014](0014-intake-ledger-handoff-and-bounded-waits.md)
and [0015](0015-trusted-head-catalog-commit.md).

## Context

The catalog validation receipt (`.local/read-validation.json`; 0012 for readers,
0015 for the writer) binds a full validation to the exact database bytes with
`SHA-256(connection.serialize())`, plus the validator-file hash and runtime
versions. Every validated read (`status`, `read_records`, `summarize_records`,
`read_records_by_receipt`, `receipt_metadata`, `upload.catalog_view`) and up to
three points per writer commit (lock entry, the re-check under `BEGIN IMMEDIATE`,
attestation) recompute it. The cost is linear in the file and independent of how
little changed. Decision 0014 already binds the intake ledger hand-off to file
identity (device, inode, size, mtime, ctime, SQLite change counter).

Measured on synthetic catalogs (`tools/diagnostics/catalog_binding_benchmark.py`
and `catalog_lock_benchmark.py`; shared, noisy host; compare shapes):

| Catalog | DB | Validated read: byte binding / streamed hash / identity | Writer hold: byte / streamed hash / identity |
|---|---|---|---|
| 400 regs | 1 MB | 1.7 / - / 0.5 ms | 54 / - / 55 ms |
| 1,600 | 4 MB | 5.9 / - / 0.5 | 69 / - / 58 |
| 6,400 | 18 MB | 25 / 6.1 / 0.5 | 159 / 104 / 75 |
| 25,600 | 73 MB | 84 / 31 / 0.8 | 413 / 219 / 138 |

About three quarters of `serialize()` + hash is the in-memory copy that
`serialize()` makes, not the hash. Reading the same bytes from the file in chunks
is about 3x cheaper and gives an identical digest on these rollback-journal
databases. A 400-file synthetic ingest, byte vs identity (host loaded, wall times
noisy): CPU 46.6 s to 34.5 s, validated-read time 9.0 s to 1.1 s, identical
`records_sha256`. With three reader processes looping on validated reads at
25.6k registrations: 33 reads/s and 5.5 CPU s (byte), 90 reads/s (streamed),
1,379 reads/s and 0.12 CPU s (identity). Reads scale with the reader count and
every read costs full CPU, which matters once parallel intake raises it.
A read-only, body-free timing of the live instance (97 MB, 22,509 revisions,
75,480 version rows; short, single process, noisy) gave `serialize()` about 94 ms
plus SHA-256 about 25 ms per binding, versus about 32 ms to stream the file.

## Options

1. **Streamed byte hash (no trust change).** Hash the file in chunks under the
   same read lock instead of `serialize()`. Same binding strength, about 3x lower
   cost at every size. Needs a (device, inode) check across the read so a
   concurrent `restore` replacing the file cannot bind new bytes to old-inode
   state.
2. **Identity binding with a daily full check (this decision's trust change).**
   The receipt's `binding` becomes
   `{version: 2, identity: [dev, ino, size, mtime_ns, ctime_ns, change_counter], validated_on: <UTC date>, validator, runtime}`.
   A reader or writer trusts the receipt only if the identity equals the live
   file's (taken after the read lock is held, so no commit can slip in) and
   `validated_on` is today's UTC date; otherwise it replays everything (`inspect`)
   and rewrites the receipt. A delta commit carries `validated_on` forward; only a
   full validation advances it. `status --audit`, the first write of each UTC day
   (daily snapshot verification), `verify_backup` and `restore` are unchanged and
   byte-exact. Identity comes from the 0014 code, moved unchanged to a shared
   module rather than copied. Fall back to the byte binding (version 1) when a
   journal file exists, the header is not rollback-journal, or timestamps lack
   sub-second resolution. Keeping the new fields inside `binding` leaves the
   receipt's top-level shape untouched, so a not-yet-upgraded reader sees a
   binding mismatch and replays instead of refusing.

## What the byte hash defends against that identity does not

| Class of change | Byte hash | Identity + daily check |
|---|---|---|
| Edit through SQLite or any writer (counter, mtime, ctime move) | refused at next read/write | refused at next read/write (tested, including raw `pwrite` edits and restored mtime) |
| Raw same-size in-place edit with mtime restored (`utime`) | refused | refused: `utime` itself moves ctime (verified) |
| File replaced or restored from backup/copy | refused | refused: new inode and ctime |
| Older valid file put back | refused (hash differs) | refused (identity differs) |
| Silent media corruption or bit rot, no write through the OS | refused at next uncached read | **not seen until the daily full check or audit** (up to one UTC day) |
| Edit that restores size, mtime, ctime and counter: root changing the clock, writing the block device, or an edit in the same timestamp tick as the commit on a coarse-timestamp kernel | refused | **accepted until the daily full check** |
| Actor who rewrites the database and the unkeyed receipt so they agree | accepted for one write (0015) | accepted for one write, same as today |

The last row is why the unkeyed receipt is the real boundary today: an actor who
can write the database can write the receipt, and the byte hash does not change
that. The first two rows after it are the whole cost of this decision. A page
cache also hides on-disk rot until eviction, so the byte hash sees it only
sometimes.

Bound: such a change can be served for at most one UTC day (until the first
validated read or write of the next day replays everything), and is caught by
`status --audit` at any time. The daily write snapshot, backups and
`verify_backup` keep full-hash and full-replay verification, so the residual does
not propagate into retained backups unverified.

## Callers affected

All go through `validated_state` or the writer's `_binding`/`_trusted_state`:
`status` (cohort, adapters, policy, restore's final live check), `read_records`
(MCP, context index, extraction, standing review), `summarize_records`,
`read_records_by_receipt`, `receipt_metadata`, `upload.catalog_view`, and
`submit`. Intake progress already uses a full inspection and is unchanged. The
first validated read after the UTC date changes pays one full replay (about
4 s at 25.6k registrations); concurrent processes may each pay it once before one
rewrites the receipt.

## Recommendation

Do option 1 first: it costs no trust and removes most of the hashing. Adopt
option 2 only if the owner accepts the two rows above and the remaining cost
justifies it. Extrapolating to the live size (an estimate, not a
measurement): a file costs about 8 validated reads and 2 commits of 3 bindings
each, about 14 bindings, so roughly 1.7 s of CPU per file with the byte binding,
0.45 s with the streamed hash and about 0 with identity. Option 1 takes most of
the gain; option 2 adds about 0.45 s CPU per file and about 100 ms off each
writer hold, in a lock that is nowhere near saturated, so deferring option 2
until reader counts grow is reasonable.

## Quality gates if accepted

Tamper tests that mutate the database, history or a record outside the writer
still refuse; the identical `records_sha256` and graph on the same fixture;
a crash after commit leaves a stale receipt that forces a full validation;
backups and the daily snapshot are unchanged; the new-UTC-day replay and the
documented residual are each covered by a test; an old-format receipt is a miss,
never a refusal.

## Reproduce

    python3 tools/diagnostics/catalog_binding_benchmark.py --sizes 400,1600,6400 --readers 1,2,3
    python3 tools/diagnostics/catalog_lock_benchmark.py --sizes 400,1600,6400 --samples 5

Synthetic fixtures only; never point them at a live instance.
