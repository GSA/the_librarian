# 0003 - Data-store compatibility and upgrade contract

**Audience:** maintainers and operators.
**Date:** October 21, 2026.
**Status:** Accepted and implemented. The store-home configuration, logical
corpus-root binding, per-instance policy evaluation, and explicit migration and
recovery paths described below are active in `tools/policy.py` and
`tools/initialize.py`.

## Context

An updateable application must be backwards compatible with data an earlier
version accumulated. The stores are not equivalent, and the distinction is the
whole basis of the upgrade story.

**Authority stores** hold what cannot be recomputed:

- Catalog: immutable hash-chained revisions, `application_id` `0x4C434154`,
  `user_version` 1, `catalog-metadata-only` authority, ABORT triggers on
  update and delete, verified pre-write and post-write backups.
- Review events: append-only hash chain with corrections,
  `application_id` `0x4C524556`, `review-events-only` authority, a
  `contract_hash` derived from the published event schema and checked against
  stored metadata, and append-only triggers verified by exact SQL text.

**Operational recovery stores** retain private work intent, not corpus authority:

- Intake: `.local/intake/intake.sqlite3`, `application_id` `0x4C494E54` (`LINT`),
  `user_version` 1; exact request bytes/root binding, prepared stage intents,
  attempts, replay cycles and acknowledgments. FULL synchronous transactions
  and a local process lock serialize effects. Missing, incompatible or detected
  corrupt ledgers refuse operation; they cannot reconstruct catalog/review
  authority. No automatic migration or cross-root portability is implemented.

**Derived stores** hold what can be rebuilt from the authority stores and the
retained sources:

- The FTS5 context index, which records in its metadata an engine fingerprint
  over the reader's own code bytes, a manifest hash, a policy hash, the
  audience, a rendition identity and the absolute corpus root.

The derived index is deliberately brittle. Observed during the rename: moving
the checkout changed the recorded `corpus_root`, and the reader refused every
read as stale until the index was rebuilt. Equally, because the fingerprint
covers the reader's and the policy module's bytes, *any* code change
invalidates the index. That is the designed behavior and it is the correct
behavior, but it dictates the shape of an upgrade.

Two further observations constrain the design.

First, store locations are code constants, not configuration. This repository
uses `.local/catalog` and `.local/review-events`; the originating installation
uses `REFERENCE/catalog` and `REFERENCE/review-events`. The same store can
therefore live at different paths depending on which installation's code reads
it, and nothing in the file format records which layout it belongs to.

Second, backups are retained without bound by design: in the originating
installation, roughly 21 GB of catalog backups and 1.2 GB of review-event
backups stand behind a 140 MB catalog across 414 retained backup files. The
no-automatic-deletion rule is correct for a system of record, but unbounded
growth is an operational fact that per-project instances multiply.

## Decision

### The upgrade contract

1. Upgrading code **never** migrates an authority store implicitly. A version
   whose persisted structure or application identity differs from a store's
   recorded contract refuses to operate and reports that explicit migration or
   re-initialization is required. The superseded `FCAT`, `FREV` and `FCTX`
   identities receive this specific refusal. The review event store also binds
   `contract_hash`; the catalog binds `user_version` and `contract_version`.
2. Upgrading code **always** assumes the derived index must be rebuilt. A
   rebuild is a normal, expected, side-effect-free step of an upgrade, and the
   stale-read refusal is the mechanism that keeps a half-upgraded installation
   from answering questions. Never repair staleness by relaxing the check.
3. A migration ships with the version that needs it, together with its
   recovery path, and is rehearsed against a copy before it touches a
   populated store. Schema snapshots are not backups.
4. Retained originals, verified backups and the append-only chains survive
   every upgrade. Rebuilding an authority store is never an upgrade or
   recovery action.

### Implemented portability and store-layout contract

- **Store home is instance configuration, not a code constant.** An instance
  may declare canonical relative `storage_roots` (`catalog_home`,
  `review_events_home`, `context_index_db`, `intake_home`, `extractions_home`,
  `diagnostics_log`, and `selection_manifest`) in `config/policy.json` or
  `config/policy.local.json`, and may override the active selection manifest via
  ignored `config/selection.local.json`. `tools/initialize.py` records the
  initialized store identities and layout in private `.local/instance.json`
  (`0600`). Changing `storage_roots` on an initialized instance requires
  `python3 tools/initialize.py --migrate-storage`, which uses private
  `.local/migration.json` (`0600`) to move populated stores transactionally and
  `--recover-migration` to roll back an interrupted move and verify pre-migration
  authority state tokens.
- **The derived index binds logical instance identity rather than host path.**
  `tools/policy.py:logical_corpus_root(root)` binds `instance_id`,
  `catalog_store_id`, `review_store_id`, and `storage_roots` into the index's
  `corpus_root` metadata. Moving an entire instance directory to a new host path
  or mounting it into a container volume preserves index freshness, while
  substituting `catalog.sqlite3`, `events.sqlite3`, or `index.sqlite3` from
  another instance is immediately refused. After an approved host-path move,
  `python3 tools/initialize.py --relocate` rebinds `requests.root` in the intake
  recovery ledger when no stage is `running`.
- **Policy is evaluated per instance.** `tools/policy.py` resolves
  `config/policy.json` and `config/policy.local.json` against the target `--root`
  instance directory on every operation, fail-closed on symlinks, unknown fields,
  or overlapping storage roots.
- **Backup retention needs a stated policy, not automatic deletion.** Report
  backup count, total size and oldest retained revision; let an operator prune
  deliberately with verification. Do not add automatic retention deletion.

### Backwards compatibility statement to publish per release

Each release states, explicitly: the catalog contract version it reads, the
event-schema contract it reads, whether the derived index must be rebuilt
(assume yes), and whether any migration is required. An operator must be able
to answer "can this version read my data?" without reading the source.

## Consequences

- Upgrades are cheap in the common case and loud in the uncommon case, which
  is the correct asymmetry for a system of record.
- Until store homes and index path binding are configurable, an instance is
  effectively pinned to its host path and to its own copy of the code. Every
  container design must respect that until it is fixed.
- Per-project instances multiply backup volume. Capacity planning is an
  operator responsibility and should be surfaced by the tooling.

## Not decided

- The configuration format for declaring a data root and store layout.
- Whether existing installations' differing store homes are reconciled, or
  each instance simply records the layout it already uses.
- How a migration is rehearsed in a container.
