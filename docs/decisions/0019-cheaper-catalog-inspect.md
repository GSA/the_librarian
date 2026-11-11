# 0019 - Catalog `inspect` is cheaper and streams its history

**Audience:** maintainers and operators.
**Date:** November 11, 2026.
**Status:** Implemented. Amends [0012](0012-bounded-validation-cost.md) and
[0016](0016-single-replay-daily-snapshot.md).

## Context

After [0016](0016-single-replay-daily-snapshot.md) the first catalog write of each UTC day is one
`catalog_store.inspect` plus writing the snapshot, so `inspect` is now the whole first-of-day lock hold,
the cost of `status --audit`, and the cost of every write that has no receipt. On synthetic catalogs
(four records per registration) it took 1.1 s, 5.0-5.5 s and 21 s at 6.4k, 25.6k and 100k registrations
and peaked at 140 MB, 470 MB and 1.77 GB. A profile at 6.4k put the time in re-encoding each payload
as canonical JSON (twice), per-character Unicode checks in `contracts.text`, the repeated
`re.fullmatch` pattern compiles, and the bookkeeping to keep every record, its full history and every
committed state alive. Only the state is used by the callers that replay the whole history.

## Decision

Make the same checks cheaper; remove only the retained history that no caller of the replay reads.

- **One decode, one encode per payload.** The replay decodes a payload once, validates it, encodes the
  canonical text once and compares that text and its SHA-256 with what is stored. Any failure at all
  (including a duplicate key, a non-finite number or a changed hash) falls back to the original
  step-by-step checks, which raise the original refusal. The memo of validated payloads is keyed by
  the payload's SHA-256 instead of its text; the equality and hash checks are never skipped.
- **The input hash is computed from the verified text.** The revision's `input_hash` is the SHA-256 of
  the canonical text of `{actor, reason, records}`. The records' canonical text is the stored payload,
  which was just verified byte for byte, so it is spliced in rather than re-encoded.
- **Faster, identical primitives.** `canonical` uses one pre-built `json.JSONEncoder` with the same
  arguments; `text` skips the per-character category check for ASCII strings (where `str.isprintable`
  is exactly "no C0 control or DEL"); identifier patterns are compiled once.
- **Streamed history.** The revisions and versions are read through two cursors in step, so the rows
  are not materialized. `GraphValidator` keeps only the fields graph checks read. `inspect(connection,
  detail=False)` additionally keeps no records, histories, revision rows or per-revision states
  and returns only the state; every caller that used only the state (the writer, `verify_backup`,
  restore, snapshots, `status`, audit, `progress`, policy) asks for that. The default still returns the
  five-tuple.
- **`rebase_index` no longer materializes history.** After a full `inspect` it derives the same token
  and last-writer maps from indexed queries (the way `submit` already does).

## Unchanged

Per-payload validation, every payload, input and chain hash, trigger checks, `integrity_check`,
`foreign_key_check`, the revision-membership check, the projection comparison, every refusal message,
the full-state `validate_graph` on a write that had to replay, the schema, and the trust model of 0015/0016.

## Evidence

`tests/test_catalog_inspect_equivalence.py` compares the production `inspect` (both modes) with a
verbatim copy of the previous one (`tests/reference_inspect.py`) on a valid history and on about
forty named and sixty randomized tamperings (edited payloads, hashes, ordinals, rewritten or deleted
revisions, forged chain, edited or deleted projection rows, metadata, dropped triggers, wrong storage
types): the result or the refusal message is identical. A second group compares `text` with the
per-character check over every low code point and random strings, and `canonical` with `json.dumps`.

Measured on the same synthetic catalogs (`tools/diagnostics/catalog_daily_replay_benchmark.py`; shared
host, so compare shapes; median of 3 fresh processes):

| Registrations | `inspect` before | after (state only) | after (full detail) | Peak RSS before / after state-only / full detail |
|---|---|---|---|---|
| 6,400 | 1.1-1.2 s | 0.65 s | 0.8-0.9 s | 139 / 57 / 124 MB |
| 25,600 | 5.0-5.5 s | 2.6-2.9 s | 3.7-4.1 s | 473 / 138 / 409 MB |
| 100,000 | 20.7-22.6 s | 11.8-12.3 s | 15.0-15.3 s | 1766 / 448 / 1513 MB |

First write of a UTC day, lock hold: 1.0 s to 0.6 s (6.4k), 4.2 s to 2.8 s (25.6k), 20.4 s to 11.0 s
(100k; process peak 1.99 GB to 0.97 GB including the snapshot image). A same-day write is unchanged.

## Consequences

- The first-of-day hold at 100k is still above the 10 s writer wait (one retry per waiter), so the
  deferred configurable wait and checkpointed audit remain deferred with their triggers.
- A write that has no receipt still replays with full detail and runs `validate_graph` on the whole
  state; switching it to `validate_delta` is a separate, smaller change.
- The default `inspect` return is unchanged, so a caller that needs the records still gets them.

## Reproduce

    python3 tools/diagnostics/catalog_daily_replay_benchmark.py seed --dir D --size 25600
    python3 tools/diagnostics/catalog_daily_replay_benchmark.py inspect --dir D --samples 3
    python3 tools/diagnostics/catalog_daily_replay_benchmark.py inspect --dir D --samples 3 --detail true

Synthetic fixtures only; never point it at a live instance.
