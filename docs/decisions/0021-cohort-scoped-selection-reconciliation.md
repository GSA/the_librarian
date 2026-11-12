# 0021 - `qualify` reconciles only its own cohort's selections

**Audience:** maintainers and operators.
**Status:** Implemented. Changes the documented `qualify` semantics of
[0009](0009-read-only-cohort-intake.md); the pinning, evidence and freshness rules of
[0009](0009-read-only-cohort-intake.md) and of the standing-policy stages are unchanged.

## Context

An instance may ingest several inboxes, one `--system` label each, into one catalog with one
selection manifest and one published index. `cohort qualify` treated the manifest as belonging to
one cohort: it pruned it to the renditions the current run extracted and then reconciled it to
exactly that run's qualified renditions. Qualifying cohort B therefore removed every selection of
cohort A (observed on an instance: A's 1,060 selections became 0 and the index served only B's 96).
The reviews were intact in the ledger and review store; only the manifest, and so the index, lost
them.

## Decision

- **Cohort membership comes from catalog evidence, not paths.** A manifest entry belongs to cohort X
  when its rendition is one this run extracted, or when its receipt's `preserved-metadata` artifact
  (whose SHA-256 the catalog pins) names a `local-path` submission sidecar with `origin.system == X`
  and agrees with the receipt (submission ID, original hash, parent and receipt links). Anything
  that cannot be proven this way (an operator's upload, a changed sidecar, a receipt too large to
  page) is *unattributed* and treated as someone else's. The `--system` label is the cohort; the
  standing request's submission ID and the review event do not name a system, and the review event
  describes an event, not the submission, so they are not authoritative.
- **Prune and reconcile touch only the cohort's entries.** `qualify` replaces the cohort's slots in
  place (so a single-cohort manifest has exactly today's bytes and order) and leaves every other
  entry byte-identical. Within its cohort all earlier semantics are kept: pruning of renditions the
  cohort no longer has, reconciliation to exactly the qualified ones, superseded-review handling,
  operator-kept selections, policy-version replacement.
- **Orphans are still dropped.** An entry whose receipt is not in the catalog, or whose rendition
  file is gone, belongs to no cohort and nothing can publish it; it is dropped and counted as
  `orphaned`. A catalog that cannot be read stops the run before the manifest is touched.
- **The read-modify-write holds the ledger writer lock.** `standing-select` already writes the
  manifest inside a ledger stage, which holds that lock; `qualify`'s prune, reconcile and
  first-time manifest creation now take it too. Concurrent runs for different cohorts, even in
  different processes, serialise their manifest writes and cannot lose each other's. Replacement
  stays atomic (temporary file, fsync, rename), so an interrupted run leaves the manifest from
  before or after it.
- **Capacity is shared.** The manifest holds at most `renditions.MAX_SELECTIONS` entries. A
  cohort may use what other cohorts leave; a qualified rendition that does not fit stays
  `pending-review` with `selection-capacity` instead of producing an invalid manifest.
- **Reported, body-free.** When other cohorts' entries exist, `counts.other_cohorts` gives
  `selections` left alone, `unattributed` among them, and `orphaned` entries dropped.

### Alternatives rejected

- *Per-cohort manifests composed at publish time.* `standing-publish`, the index metadata and the
  freshness check pin one manifest hash. A composed manifest would be a new, unpinned artifact and
  every cohort run would need all other cohorts' manifests to publish, so it adds a coordination
  surface and weakens nothing less than the chosen design does.
- *Scoping by submission IDs of the current run only.* It cannot find a cohort's earlier selections
  once the inbox changes (deleted files keep their selections until pruned), which today's prune
  handles by path.
- *Attribution by the rendition path or the submission ID prefix.* Paths and IDs are not evidence;
  the pinned sidecar is.

### Operational note: policy bumps

`standing-publish` refuses a manifest holding a selection that cites a review of an older policy
version (`standing-selection-unauthorized`), so after bumping a policy version every cohort must be
requalified before the next publication succeeds. Cohorts are no longer lost meanwhile, and the
refusal is a safety property that is kept.

## Per-entry cost of re-qualifying

With saved reviews, `qualify` still walks every extracted entry, and each walk looked up the
correction chain of its standing request with a table scan of the ledger and rebuilt the set of
live review events, so the review loop was quadratic in the ledger. The run now reads correction
links once (`store.Heads`, extended incrementally under the ledger lock), takes the correlation
IDs with one query, and builds the live-event set once per run. The decisions made for each entry
are unchanged. Measurements are in the pull request and the Kata.

## Consequences

Qualifying one cohort no longer changes another's selections, and re-running `qualify` for a
cohort whose selections were previously pruned restores them from its saved reviews with no model
calls. Selection of renditions beyond the shared 5,000-entry cap, and the first-time cost of
`ingest` at tens of thousands of files, are outside this decision.
