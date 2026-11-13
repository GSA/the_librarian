# The Librarian: current-state design

**Audience:** maintainers, operators and their agents.
**What this is:** a description of how the system works today and why, with the
test or code that backs each claim. It replaces the numbered decision records
(0001-0021), which are in git history (`git log -- docs/decisions`).

**What this is not:** a list of rules. Nothing here is a permanent constraint
because it is written here. Where a statement is backed by a test it says so,
and the test is the thing to keep green or deliberately change. Where it is
backed only by reasoning or a one-off measurement, it says that too. Any of it
may be changed when evidence supports a better approach; change the code and
tests first, then update this file in the same PR. Operator procedures live in
[`context/intake.md`](../../context/intake.md) and the [README](../../README.md);
planned work lives in the [ROADMAP](../../ROADMAP.md) and in Katas.

Safety and trust statements (paths, untrusted text, attestation, authority,
tamper, durability, backup) are kept even where no test pins them, and are
marked **(untested)** so a test can be added; they are not weakened by being
restated here.

## 1. Identity and stores

- **Names.** Checkout `the_librarian`, distribution `the-librarian-mcp`, module
  `the_librarian_mcp`, commands `librarian-mcp`, `librarian-preflight`,
  `librarian-audit`, MCP server name `the-librarian`. The earlier
  `qualified-context-*` names have no aliases. Evidence:
  `tools/context-mcp/pyproject.toml`; `tests/test_kit.py::test_librarian_identity_and_role_documents_are_packaged`.
- **Three kinds of store**, all SQLite under a per-instance data root:

  | Kind | Stores | Rule of thumb |
  |---|---|---|
  | Authority | Catalog (`LCAT`, hash-chained immutable revisions, ABORT triggers on update/delete); review events (`LREV`, append-only hash chain with corrections, schema `contract_hash`) | Cannot be recomputed. Never rebuilt as an upgrade or recovery step (`tests/test_authority_store_upgrade_paths.py`). |
  | Operational | Intake ledger (`LINT`, exact request bytes, stages, attempts, replays; `synchronous=FULL`, rollback journal) | Private work intent. Cannot reconstruct authority. |
  | Derived | FTS5 context index (`LCTX`) with engine fingerprint, manifest hash, policy hash, audience and a logical corpus root | Rebuildable; a stale index refuses reads. |

  Evidence: `tests/test_kit.py::test_new_application_ids_and_superseded_refusal`
  (superseded `FCAT`/`FREV`/`FCTX` identities refuse with a migration message),
  `::test_initialize_refuses_an_unmigrated_legacy_index`,
  `tests/test_catalog_inspect_equivalence.py` (about 100 tamperings of catalog
  rows, triggers, metadata and application id refuse),
  `tests/test_kit.py::test_native_review_correction_retry_and_restore`
  (`DELETE FROM events` aborts), `tests/test_intake.py::test_wrong_identity_modes_and_missing_store_refuse`,
  `tests/test_kit.py::test_stale_content_detected` and `::test_clean_copy_and_policy_freshness`.
  The review-event `contract_hash` refusal is pinned by
  `tests/test_review_event_contract.py`: an out-of-band edit of the stored hash
  and a change to the installed schema each make `status`, `audit`, `append`,
  `backup` and the reader refuse with the explicit-migration message and leave
  the file unchanged, including when the edit preserves inode, size and mtime
  (so a validation cache keyed on file identity cannot bypass it). After a
  schema change, snapshots taken under the old contract also refuse in
  `verified_snapshot` and `restore`; an edit of only the live store's hash does
  not invalidate earlier snapshots (not tested as a recovery path). `init`
  never overwrites, so the tested way forward is an empty re-initialization
  beside the refused store.
  `tests/test_authority_store_upgrade_paths.py` builds populated catalog and
  review-event stores in temporary release copies, bumps `user_version` or
  changes the contract marker (catalog `contract_version`, review-event
  `contract_hash`), and asserts that `initialize` (plain, `--relocate`,
  `--migrate-storage`, `--recover-migration` with and without an interrupted
  migration), each store CLI (`init`, `status`, `audit`, `backup`, `restore`,
  `query`/`export`, `submit`/`append`, `get`), the context-index catalog
  command and the review-event reader refuse and leave every file of the
  instance byte-identical. `init` refuses an existing store before opening it;
  an interrupted-migration recovery rolls store files back to their old location before it refuses, keeps its journal and leaves the database bytes unchanged (it never repairs a store); the
  intake progress view reports the catalog as unavailable; and a derived-index
  build never rewrites an authority store.
  Durability: every write connection the catalog, review-event and intake stores
  open (init, write, backup-init, intake `locked` and `relocate`) runs with
  `synchronous=FULL` on a rollback journal (`journal_mode=delete`), pinned by
  `tests/test_store_durability.py` (Kata 0dz3). `synchronous` is per connection and
  not stored in the file, so the tests read it on each connection that executed a
  write, after starting it at `OFF`; weakening or dropping any of the seven pragma
  sites fails them. `Connection.backup` copy targets are not covered: they are
  fsynced and hash-verified before use rather than relying on the pragma.
- **Upgrade behavior.** Code never migrates an authority store implicitly: a
  store whose application id, `user_version`, `contract_version` or
  `contract_hash` differs from the installed contract refuses with an
  explicit-migration message. The derived index is assumed to need a rebuild
  after any code change, because its fingerprint covers the reader and policy
  module bytes (`context_index.engine_fingerprint`,
  `tests/test_kit.py::test_qualification_code_is_bound_into_engine_fingerprint`).
  Schema snapshots in `tools/schemas/*.sql` are not backups.
- **Store locations** are instance configuration (`storage_roots` in
  `config/policy*.json`), recorded with instance and store ids in
  `.local/instance.json`. `tools/initialize.py --migrate-storage` /
  `--recover-migration` move populated stores transactionally;
  `--relocate` rebinds the intake ledger root after a host-path move. The index
  binds a *logical* root (instance id, store ids, storage roots), so moving the
  instance directory keeps it fresh while substituting another instance's
  database file is refused. Evidence:
  `tests/test_kit.py::test_portable_roots_relocation_substitution_and_migration_recovery`
  and `::test_portable_roots_edge_cases_and_instance_policy_propagation`.
- **Topology.** One instance is one full clone: its own checkout, `.venv`,
  `config/*.local.json`, inbox and stores. Policy is resolved per `--root` on
  every operation; a root without its own policy files gets only the versioned
  defaults (`tests/test_policy_isolation.py`). Instances can run different
  versions. The reason is isolation by filesystem with no cross-instance code
  path; the cost is N environments to upgrade (reasoning, not measured).
  `.local/instance.json` records ids and layout only; it does not record the
  distribution version or store contract versions (an earlier design wanted it
  to, and nothing reads it; Kata 7jkt).

## 2. Distribution and runtime

- **Packaging** is an explicit allowlist, `release-files.json`, built by
  `tools/package.py`. It excludes `.local`, `.venv`, databases, `data`, `sources`,
  `documents`, `*.local.*` files and symlinks. Evidence:
  `tests/test_kit.py::test_clean_copy_and_policy_freshness` (the archive equals
  the allowlist and omits `policy.local.json`), `::test_release_paths_rejects_missing_and_symlinked_files`,
  `::test_package_rejects_symlinked_output_parent`, `::test_empty_initialization`
  (the shipped toolkit seeds zero records).
- **Runtime checks.** `librarian-preflight` probes Python >= 3.11, `mcp==2.2.0`
  and SQLite FTS5 in the interpreter the adapter runs on, and the adapter refuses
  to start on an incomplete clone or a symlinked toolkit path
  (`tests/test_mcp.py`). The runtime probe refuses, with a non-zero exit and an
  explicit diagnostic, Python below 3.11, any `mcp` version other than the pin,
  and missing FTS5, and reports the Python, SQLite and `mcp` versions and
  `fts5: true` when it passes (`tests/test_preflight.py`; the real probe runs in a
  child interpreter whose facts a temporary `sitecustomize` stubs). Only `mcp` is pinned exactly; `anyio` and `jsonschema`
  are ranges and transitive dependencies are unpinned. There is no lock file,
  image or installer. Whether those are
  wanted is an open product question, not a commitment (Kata 7jkt).
- **Optional Docling worker** runs in its own interpreter with pinned assets
  (`tests/test_docling.py::test_changed_profile_or_assets_cannot_match_prepared_identity`).
  The reader needs none of it.
- The first proof-of-concept installation is closed; this repository does not
  maintain or port from it. Its clean-copy tests only prove this repository's
  packaging and policy boundary.

## 3. Intake and review

- **Inbox first.** Content enters through the supported writers: manual upload
  (`tools/intake/upload.py`), read-only cohort intake (`tools/intake/cohort.py`)
  and the durable ledger (`tools/intake/*`). A copied file is not a registered
  receipt until its catalog commit is confirmed; a receipt is not extracted
  text; extracted text is not a review; a review is not selection. Connectors,
  scheduling and hosted sources are not built (ROADMAP). Evidence:
  `tests/test_intake_upload.py::test_missing_or_unmarked_drop_is_never_success`,
  `::test_blocked_receipt_is_reported_without_claiming_success`,
  `tests/test_intake_cohort.py::test_ingest_submits_and_extracts_admitted_files_with_body_free_counts`.
- **Cohort intake never writes the inbox.** Every file gets exactly one counted
  disposition; formats without a reader are `needs-reader`; a missing or
  unlistable inbox fails instead of counting nothing; the inbox tree is
  byte-identical after a run. Evidence:
  `tests/test_intake_cohort.py::test_every_file_gets_one_explicit_disposition`,
  `::test_missing_or_unlistable_inbox_fails_instead_of_counting_nothing`,
  `::test_over_limit_is_counted_not_dropped`.
- **Ingest follows correction-chain heads.** `ingest` and `qualify` report an
  extraction or registration through the head of its validated `correction_of`
  chain, never by request name: a repaired entry is `extracted` with the head's
  evidence, a blocked or ambiguous head is `failed` with its own code, and an
  entry with no correction reports and writes as before. A head must repeat the
  entry's operation, receipt and format (`correction_mismatch`). A registration
  blocked as `changed-extraction-identity` (the extractor code changed after the
  stage pinned it, and replay keeps the pin) is repaired by
  `cohort recover-identity`, which stages validated corrections with a fresh
  pin; the identity, source-hash and catalog-pin checks are not relaxed.
  Evidence: `tests/test_intake_cohort.py::CorrectionChainTests`,
  `::RecoverIdentityTests`, `::QualifyRepairedTests`.
- **Standing review.** A model reply is recorded as findings exactly as
  reported; the standing policy, not the model, decides qualification. Missing
  inspection, internal contradictions and out-of-scope receipts block; references
  to material outside the rendition are advisory. Model traffic stays within the
  configured provider and redirects are refused. Evidence:
  `tests/test_standing.py` (`test_fabricated_success_without_inspection_is_not_qualified`,
  `test_confidence_never_yields_verification`, `test_prohibited_scope`,
  `test_advisory_references_do_not_block_...`), `tests/test_model_review.py`
  (`test_control_task_must_not_be_accepted` covers a synthetic injection control,
  `test_redirects_are_refused`, `test_usai_transport_refuses_unconfigured_models_and_providers`).
- **Model choice and cost** are the operator's. `model_review.evaluate` scores
  candidate models on a sample and `estimate()` extrapolates token use; nothing in
  the code requires an approved budget ceiling and the current instance runs
  without one. (An earlier record said a ceiling was required before bulk review;
  the owner decided otherwise and the code never enforced it.)
- **Replaying an unchanged inbox** (`ingest`, and the `ingest` inside every
  `qualify`) re-checks every file and trusts no cache file or earlier report: it is
  read and hashed once to classify it, checked again against the ledger's saved
  original at submission, and its stages are looked up, so a file changed since
  intake is still found (a content-salted submission that supersedes the old one, or
  `changed-since-intake`). The cost is kept to three ledger opens per entry: one read of the
  entry's receipt request with its summary, reused for its acknowledgment only when the stage has
  `succeeded` (a terminal status), then the chain heads of the extraction and registration. The storage-root validation is reused for an identical policy, with
  the symlink walk done once per path component. Hashing is about 1% of a replay;
  the MIME HTML and transcript parsers in `classify` are the floor. Measured with
  `ingest_benchmark.py --replay`: 11 to 5 ms per file, identical report digest.
  Evidence: `tests/test_intake_cohort.py::ReplayTests`,
  `tests/test_policy_isolation.py::StorageRootChecksAreCheapButNotWeaker`.
- **`qualify` and shared manifests.** An instance may ingest several inboxes
  (one `--system` label each) into one catalog, manifest and index. A manifest
  entry belongs to a cohort when its rendition was extracted by the current run
  or its receipt's pinned `local-path` sidecar names that system; anything that
  cannot be proven that way is left alone, and orphans are dropped and counted.
  The manifest read-modify-write holds the ledger writer lock and replacement is
  atomic. The manifest holds at most `renditions.MAX_SELECTIONS` (5,000) entries
  across all cohorts. An in-scope entry with no saved review is not sent to the
  model when the manifest has no room for it: `qualify` checks the room (an
  exact read, 2 ms at 5,000 entries) on the main thread just before the call,
  and a refused entry ends `pending-review` with `selection-capacity` with
  nothing staged, so the next run with room reviews it once. Concurrent
  prefetching starts a review ahead only while the free room exceeds the entries
  in the lookahead window; otherwise the entry is checked in its turn, so
  outcomes do not depend on completion order. An entry with a saved review is
  never re-reviewed and is selected when room exists; a select refused by a
  concurrent cohort's write still ends `selection-capacity` with its review
  kept. `counts.selection_capacity` reports `entries` held and
  `model_calls_skipped`. Measured on a synthetic 40-file cohort with the cap
  lowered to 10: 40 calls and 4,230 prompt tokens before, 10 and 1,058 after
  (60 files, cap 20: 60 and 6,350 before, 20 and 2,117 after), same
  qualified/pending counts. Evidence:
  `tests/test_intake_cohort.py::SharedManifestTests` (the `selection_capacity`
  and saved-review tests).
  Evidence: `tests/test_intake_cohort.py::QualifyTests` (about 15 tests from
  `test_a_b_a_b_keeps_every_cohorts_selections_and_publishes_them_all` through
  `test_two_processes_qualifying_different_cohorts_do_not_lose_each_others_writes`).
  Operator notes (restoring cohorts an older toolkit pruned; requalifying after a
  policy bump) are in `context/intake.md`.
- **Standing scope is checked before the model call.** The policy's receipt and
  audience scope used to be applied only by the `standing-review` adapter, after
  the model had been paid, so every out-of-scope receipt cost a call and then
  ended `exception`/`prohibited-scope`. `qualify` now reads `receipt_id` and
  `audience` from the catalog-pinned rendition through the adapter's own
  validation (`standing.refused_scope`, 0.07 to 2.2 ms read cost against 0.7 to
  9.4 ms for the full validation, per rendition of 13 to 4,001 records), and when
  the scope refuses it makes no call: the request is staged with an empty,
  never-written findings pin and the adapter ends it `prohibited-scope` by its
  own, unchanged scope rule (`standing.permits`, also used by publication
  authorization). Any request staged by another path is refused as before; a
  rendition that does not validate is not judged and takes the ordinary path.
  The terminal state is the same blocked `standing-exception-prohibited-scope`;
  the one deliberate difference is that an out-of-scope entry whose call would
  have failed (transport, invalid reply, over budget) now ends there too instead
  of staying `pending-review`. `counts.out_of_scope` reports `entries` and
  `model_calls_skipped`. With no policy file, the generated scope is
  `receipts: ["D*"]` (every upload receipt `D<number>`, audience `source`),
  which is also allowed in a hand-written policy and is not limited by the
  5,000-entry list cap; an existing policy is never rewritten or widened.
  Widening a policy's scope changes its hash, and the hash is part of every
  standing request, so the operator raises `version` with the edit and every
  in-scope entry is then reviewed once under the new policy (one call each,
  including entries already qualified; entries still refused cost nothing; a
  rerun with an unchanged policy costs 0). Editing the scope without raising the
  version ends the new reviews as `conflicting-review`. A policy containing
  `D*` is not readable by a toolkit without this change. Measured on a synthetic
  mixed fixture (40 renditions, half out of scope): 40 calls and 31,610 prompt
  tokens before, 20 calls and 15,770 after, identical outcomes. Evidence:
  `tests/test_intake_cohort.py::ScopePrecheckTests`,
  `tests/test_standing.py::test_any_receipt_scope_entry_covers_upload_receipts_only`.
- **Publication is one stage, run at the end of `qualify` or on demand.**
  `qualify` publishes the context index once, after reconciling the manifest;
  `cohort publish` runs the same `standing-publish` stage (policy pin, standing
  authorization, freshness, availability, ledger evidence) for the current
  manifest with no ingest, review or model call, so an interrupted run is
  recovered at the cost of one publication. It is idempotent on the manifest
  hash. There is deliberately no interim publication during a run: the index
  pins the manifest bytes and the review-event store state, so it is stale again
  at the next review a run appends and could not serve progress. Measured cost
  of a first publish (synthetic): about 2.5 s plus 5 to 6 ms per selection at
  1,000 to 2,000 review events, dominated by per-selection validation rather than
  the index build, so an incremental build is not pursued. Evidence:
  `tests/test_intake_cohort.py::PublishTests`.

## 4. Backups

- Catalog and review-event writes take a verified `daily` snapshot before the
  commit only when the store has no intact snapshot for the current UTC day; an
  idempotent retry takes none, post-write snapshots are not taken, and
  `init`, `backup` and restore keep their own snapshots. Snapshots, receipts and
  `.partial` files older than `backup_retention_days` (default 7, minimum 1) are
  removed after the new snapshot verifies; nothing is deleted unless a verified
  snapshot remains in the window; a retention failure never fails the write.
  `python3 -m tools.backups prune` is a dry run unless `--apply`.
- **Loss window.** Between snapshots recovery relies on `synchronous=FULL` (`tests/test_store_durability.py`) and
  the hash-chained history: the worst case restores the day's first snapshot and
  replays that day's requests by their original request ids. This is a deliberate
  trade against per-write snapshots, which in one measured cohort produced 5,815
  backups (35 GB) for a 12.7 MB catalog.
- On the first write of a UTC day the snapshot is the serialized image of the
  transaction that was just fully inspected, written, fsynced, re-read and refused
  unless its hash equals the image (`catalog_store.py`, "Backup bytes mismatch").
  Fresh verification checks fields, size, SHA-256 and receipt state instead of a
  third replay; every older snapshot is still fully verified before it counts.
- Evidence: `tests/test_backups.py` (daily gate, corrupt or same-length-corrupt
  snapshots do not count, window pruning, never deleting the only verified
  snapshot, dry-run default, protected snapshots, restore, review events follow
  the same rule), `tests/test_catalog_daily_snapshot.py`.
- Not covered: offsite copies, scheduled backups, snapshots of the intake ledger
  and the rebuildable index.

## 5. Catalog validation, writers and cost

This section is the product of several performance passes. The current behavior
is what matters; the chain of how it got here is in git history and the Katas
(fg46, yxc2, 2c1h, wjf9, 2r8q, 3re5, 2vfn, 1mbc, 2rye).

### What a catalog write validates

1. **Receipt.** A validation receipt in `<catalog home>/.local/read-validation.json`
   records the state a full validation produced and a *binding*: for a plain
   rollback-journal database, the file identity (device, inode, size, mtime_ns,
   ctime_ns, SQLite change counter) plus the validator-file hash and runtime
   versions; otherwise (journal or WAL present, or no `sqlite_dbpage`) the SHA-256
   of the exact database bytes. An identity-bound receipt is trusted only on the
   UTC date of the full validation it descends from (`validated_on`); the first
   validated read or write of the next UTC day replays all history and rewrites it.
2. **Trusted head.** When the binding matches, the writer takes the state from the
   receipt (it must equal the state re-derived from the database head); otherwise
   it runs a full `inspect`. An unreadable receipt only costs a replay; it never
   blocks a write.
3. **Delta.** `contracts.validate_delta` checks only the batch, referrers of
   rewritten records, moved path claims and parent chains, through indexed lookups,
   and refuses what `validate_graph` would refuse on the same history. Idempotency
   (`request_id`, `input_hash`), the rebase pin lookup and "did a later revision
   write this record" use indexed queries.
4. **Re-check** under `BEGIN IMMEDIATE` compares the binding, not a second replay.
5. **Attestation.** After a successful commit the writer writes the receipt for
   the new bytes (best effort). A crash after COMMIT leaves a receipt that no
   longer matches, so the next reader or writer validates in full and the retry is
   idempotent.
6. **First write of a UTC day** replays the history once (`inspect` with
   `detail=False`, state only) inside the writer lock, then writes the snapshot.
   `inspect` decodes and encodes each payload once, falls back to the original
   step-by-step checks on any irregularity, and streams revisions and versions.
   `status --audit`, `verify_backup`, restore and the daily snapshot always replay
   in full.

Measured when written, on synthetic catalogs of four records per registration
(shared host; compare shapes, rerun the benchmarks before relying on a figure):
lock hold for a normal write fell from 5.7 s to 0.33 s at 25,600 registrations
before identity binding, and to about 0.14 s after; a validated read fell from 84
to 0.8 ms; the first write of a day was about 2.8 s of lock hold at 25,600
registrations and 11 s at 100,000; a body-free read of one production-sized
catalog (63k records, 22.5k revisions, 92 MB) put one full `inspect` at about 4.4 s
before the last speedup. Reproduce with the tools in `tools/diagnostics/`
(`catalog_lock_benchmark.py`, `catalog_commit_ceiling.py`,
`catalog_daily_replay_benchmark.py`, `ingest_benchmark.py`,
`ledger_scale_benchmark.py`, `upload_planning_benchmark.py`,
`ledger_verify_benchmark.py`, `progress_benchmark.py`, `catalog_contention_budget.py`). Use synthetic
fixtures only.

Evidence: `tests/test_catalog_delta_commit.py` (randomized and named equivalence
with `validate_graph`, tamper classes, forged receipts, crash after commit,
rebase, daily snapshot on the fast path), `tests/test_catalog_identity_binding.py`,
`tests/test_catalog_daily_snapshot.py`, `tests/test_catalog_inspect_equivalence.py`
(production `inspect` vs a verbatim copy in `tests/reference_inspect.py` on a
valid history and about 100 tamperings: same result or same refusal message),
`tests/test_validation_cost.py`.

### Lookup indexes and the `refs` table

A write needs two lookups into the committed state: the current records that reference a
record the batch rewrites (`referrers`), and the current artifacts that already hold a path
the batch moves (`claimants`). They used to be `instr(payload, ?)` scans over every current
record, so each write cost O(catalog) and a first ingest grew quadratically (at 40,000
synthetic documents the two scans were about half of the time per document).

- **Structures** (`LOOKUP_OBJECTS` in `catalog_store.py`, mirrored in `tools/schemas/catalog.sql`):
  partial expression indexes on `versions` for `$.data.path`, `receipt_id`, `parent_id`,
  `artifact_id`, `from_id` and `to_id`, and `refs(ref, revision, ordinal)`, one row per
  `source_ids` entry of every *receipt version*, kept by an `AFTER INSERT` trigger on `versions`.
  `source_ids` is an array, which an expression index cannot cover. `refs` is keyed by version,
  not record, so it is append-only like `versions` (a rewritten receipt adds rows and deletes none)
  and the lookup keeps only versions that are still current. The probe is one `IN (...)` query per
  field joined to `current_records` by `EXISTS`, not `JOIN`: a join made SQLite scan.
  Nothing in them is authority; they are derived from `versions`.
- **Two valid shapes.** `inspect` accepts a catalog with none of the structures (`legacy`, the
  scans, slower and decision-identical) or with all of them exactly as specified (`indexed`; the
  `sqlite_master` SQL of every object is compared with the code's text, and `refs` must equal the
  rows the replayed payloads imply). Anything between, a changed definition or a missing,
  extra, altered or moved `refs` row is refused ("Catalog lookup structures are incomplete or
  differ ..."). `PRAGMA integrity_check` already covers the index pages. A forged receipt cannot hide this
  past the daily replay or `audit`, like any other edit.
- **Schema version unchanged.** The authority data, the hash chain, every state token and the
  `contract_version` metadata are the same, so `VERSION` stays 1. Bumping it would refuse every existing
  catalog and invalidate every backup receipt (`backup_version == VERSION`) for a change that
  adds no authority data. The cost of this choice is that two binaries disagree about shape only in speed:
  a catalog written by older code keeps working (the trigger keeps `refs` current for any writer, including
  an older binary), and older `inspect` ignores the extra objects.
- **Migration.** New catalogs get the structures at `init`. An existing catalog is migrated explicitly
  and never implicitly: `python -m tools.initialize --migrate-catalog-indexes` (or
  `python -m tools.catalog.catalog_store migrate-indexes [--check|--rebuild]`). Under the writer lock and one
  `BEGIN IMMEDIATE` transaction it fully inspects, publishes a verified `pre-index-migration` snapshot,
  creates the indexes, fills `refs`, inspects again and requires the identical state, then commits and
  attests the new bytes. Any failure rolls back. A killed process leaves SQLite's rollback journal, which
  `connect` refuses like any other unsupported sidecar; `migrate-indexes --recover` (writer lock held; WAL and
  shared-memory sidecars still refused) lets SQLite roll it back to the earlier, `legacy` file, fully verifies it and
  migrates. A second run only verifies. `--rebuild` drops damaged or half-present structures in the same transaction and recreates them,
  and succeeds only if the rest of the catalog verifies. `--check` writes nothing.
- **Restore.** A snapshot taken after the migration carries the structures and restores as `indexed`; a
  snapshot from before it restores as `legacy` and works unchanged (slower) until `migrate-indexes` is run
  again. `audit` and `verify-backup` verify whichever shape they find.
- Measured on synthetic production-shaped catalogs (4 and 3 records per document; shared host, CPU time
  per document of a first ingest into a catalog already holding N documents): 130 to 103 ms at 2,000,
  296 to 126 ms at 10,000, 977 to 260 ms at 40,000; the catalog grows 10% at 40,000 (291 to 311 MB) and the migration
  takes about 20 s there. Rerun with `tools/diagnostics/ingest_benchmark.py` on a seeded instance.
- Evidence: `tests/test_catalog_lookup_indexes.py` (randomized decisions equal with and without the
  indexes and equal to `validate_graph`; referrer and claimant results equal brute force on every step; a
  receipt whose `source_ids` reference an existing source; only current receipt versions count;
  query plans use indexes, no scan; tamper tests for each dropped or altered index, `refs` table, trigger
  and row; a corrupted index page; migration idempotency, snapshot, failed and killed migration,
  rebuild, restore of an older snapshot, command line), and `tests/test_catalog_inspect_equivalence.py`
  (`inspect` equals the reference on both shapes).

### Trust model and residual risk (kept)

- The catalog receipt and the ledger hand-off attestation are **unkeyed**. For the
  **catalog**, an actor who rewrites the database *and* the receipt so that they
  agree, or who rewrites the file while restoring size, mtime, ctime and the change
  counter, can get one write or read accepted without a replay; the next UTC day's
  full check, `status --audit` and backup verification replay all history and
  refuse. For the **intake ledger** there is no daily bound: the attestation is
  checked against file identity only, with no date, so an identity-preserving
  rewrite is detected only when a full ledger validation actually happens (a writer
  with no valid attestation, or a validating `progress` read). Catalog audit and
  backup verification do not validate the ledger. Neither is a defense against that
  actor.
- What the byte-hash binding saw and the identity binding does not: silent media
  corruption with no write through the OS, and an identity-restoring edit. Both
  are now caught at the next daily full check or audit, a window of at most one UTC
  day. Everything else refuses at the next read or write: SQLite edits, raw
  same-size `pwrite`s (even with mtime restored, because `utime` moves ctime), a
  replaced file, a restored older file.
- Evidence: `tests/test_catalog_identity_binding.py` (`test_raw_same_size_edit_is_refused`,
  `test_raw_edit_with_restored_mtime_is_refused_because_utime_moves_ctime`,
  `test_residual_identity_restoring_edit_is_served_until_the_next_utc_day`,
  `test_audit_always_replays`), `tests/test_catalog_delta_commit.py`
  (`test_forged_receipt_for_other_bytes_does_not_vouch_for_tampered_bytes`,
  `test_first_write_of_each_day_replays_in_full_even_with_a_matching_forged_receipt`,
  `test_documented_residual_forged_receipt_is_trusted_until_the_next_daily_snapshot`).
  The ledger's equivalent residual (a file rewrite that preserves identity and
  change counter) is stated but not pinned by a test **(untested)**, Kata yy1k.
- **Never close a descriptor on a database file while a connection to it is open
  in this process.** On POSIX, closing any descriptor drops all of the process's
  fcntl locks on that file, including SQLite's, which let a concurrent commit tear a
  validating reader's snapshot (observed as spurious "Current records differ" and
  "database disk image is malformed"). `tools/file_identity.py` therefore reads
  stat fields from the path and the change counter from page 1 through the open
  connection. Evidence: `tests/test_file_identity_locks.py`.

### Review-event validation

Every read of the review-event history (`event_reader.read_snapshot`, `event_store.status`,
`append`) used to validate all of it: SQLite integrity, hash chains, schema, correction
index. `qualify` did that about nine times per entry, so its serial toolkit cost grew about
1.1 ms per prior event (Kata r71k, from efz3).

- **Memo.** `event_store.inspect(..., reuse=True)` keeps the last validated result per
  database file **in process memory** (never written to disk, at most eight files). It is
  reused only when the file's identity (device, inode, size, mtime_ns, ctime_ns, SQLite
  change counter, from `tools/file_identity.py`), the installed contract hash and the UTC
  date all equal those of the validation, the connection was opened by
  `event_store.connect` on that same device and inode, and the chain heads and correction
  count re-read from the file equal the remembered state. A journal or WAL beside the
  file, a build without `sqlite_dbpage`, or any other connection validates in full and
  stores nothing. A result is stored only when the identity is the same before and after
  the pass, so a commit that lands during validation is never remembered; a failed
  validation is never stored. Callers receive fresh state and event-list containers (the
  event dictionaries are shared and read-only by contract).
- **Invalidation.** Any commit (by this or another process), restore, swapped file,
  same-size edit (even with a restored mtime, because `utime` moves ctime), changed
  contract or new UTC day misses and revalidates everything with the unchanged
  refusals. `event_store.py audit` (and `status(audit=True)`) always replays
  and first forgets the file, so a refusing audit leaves nothing to serve.
- **Cost.** Synthetic, zero-latency stub, concurrency 1, `nice`d on a shared host
  (`qualify` per-entry serial ms, first decile to last decile): 400 documents 89 to 432
  before, 66 to 149 after (mean 257 to 104); 1,200 documents 168 to 1,425 before, 120 to 388
  after (mean 770 to 242; qualify total 937 s to 303 s). Results, event ids and the manifest are unchanged (the `qualify` tests
  are the same). What remains is what the memo does not touch: each event append still
  validates `existing + incoming` once, each new commit costs one full validation at the
  next read, and `qualify` still reads a snapshot per entry, so the cost still grows with
  event count (about 0.25 ms per prior event after, against about 1.1 ms before). Removing
  that term needs an O(delta) check (Kata m0tg).
- **Residual** (the catalog receipt's, 0017). The memo is unkeyed. An edit that restores
  size, mtime, ctime and the change counter *and* leaves the last event and batch hashes
  and the correction count unchanged is served from the memo until this process's first
  validation of the next UTC day, a restart of the process, or an audit, which replay
  everything and refuse. It is not a defense against an actor who can write the file.
  Evidence: `tests/test_review_event_memo.py` (reuse, same-size edit with and without
  restored mtime, swapped and swapped-tampered file, a connection opened before a swap,
  a concurrent writer process's commit, an overlapping commit, the stated residual and its
  expiry, contract change, audit).

### Concurrent writers

- The catalog raises typed errors: `StaleState` (pin outdated), `WriterBusy`
  (writer lock still held after a bounded 10 s wait across processes; same-thread
  re-entry fails at once) and `ConflictingWrite`. Readers wait up to 10 s for an
  in-progress writer's journal and refuse an abandoned one.
  `submit(..., rebase=True)` accepts an outdated pin from this catalog's history
  when no later revision wrote any record in the request, and still validates the
  full graph; intake registration stages use it. There is one linear revision chain
  and no single-writer queue.
- Intake maps `StaleState`/`WriterBusy` to retryable `catalog-stale-state` /
  `catalog-busy`, `ConflictingWrite` to blocked `catalog-conflict`. A lost race
  wrote nothing, so it does not spend a stage's `max_attempts`. It is counted
  against a separate bound (`CONTENTION_BUDGET` = 30 per stage cycle, then
  dead-letter) and one `settle` call retries at most `CONTENTION_RETRIES` = 6 in a
  row (about 60 s at the 10 s wait) before leaving the stage `retryable`. Input
  faults and transient errors are still charged. `recover-conflicts [--apply]`
  replays legacy blocked registrations.
- Evidence: `tests/test_intake_concurrency.py` (rebase, busy, reader behavior,
  two processes commit every receipt once, the whole contention-budget group,
  `recover-conflicts`).
- Throughput is still serialized by the catalog writer lock. The commit ceiling at
  a 1,600-registration catalog was about 10 registrations/s with one writer when
  measured; batching many registrations per revision was tried on paper and not
  adopted because it changes idempotency, partial-failure and rebase semantics.

### Intake ledger

- A writer fully validates the ledger (SQLite integrity, digests, stage and
  attempt shape) unless the previous clean writer attested the file. On clean
  unlock a writer records file identity and SQLite change counter in `writer.lock`;
  the next writer, in any process, consumes it while holding the lock and skips
  full validation only if the live file matches. A crash, failure after a partial
  write, or a foreign commit leaves no attestation. A block that raises a
  read-only refusal (for example `unknown_request`) still hands off when the file
  identity equals what the writer validated on entry.
- The writer lock is polled non-blocking with jitter for at most
  `LIBRARIAN_INTAKE_LOCK_WAIT_SECONDS` (default 30, max 3600; invalid values refuse)
  and the same value is the SQLite busy timeout. Exceeding it, or a transient SQLite
  BUSY/LOCKED during a stage, raises retryable `ledger_busy`; nothing was written.
  `settle` and cohort ingest retry up to three times. `progress` reads the ledger in
  keyset pages of 5,000 requests, each in its own short read transaction, and
  verifies each page afterwards (`store.verify_page`, sharing `store.check_request`
  with `store.verify`), so it cannot hold a read lock across O(ledger) work and its
  memory is a page plus counters. It reduces the ledger to counts per distinct
  request signature and expands only the newest `--limit` items; each page is
  consistent, the pass as a whole is not one global snapshot.
- `progress` keeps its finished summary in memory keyed by the ledger's file identity
  (the writer hand-off's `file_identity`), `--limit`, the run set and the UTC date,
  stored only when the identity is unchanged from before the first to after the last
  page. A commit, a tampered row or a new UTC day re-verifies in full; the residual
  is that of the catalog receipt (an edit that restores size, times and change
  counter is seen at the next UTC-day check). Digests of requests and artifacts
  already proven are kept by SHA-256 (no raw bytes, no failures) and a hit still
  hashes the bytes in hand and compares the stored digest. Its catalog read never
  writes a receipt: it trusts a receipt covering this exact file or this process's
  own full inspection today. `progress-serve` renders one page at a time, shares the
  result with requests that arrive meanwhile and reuses one younger than a second;
  the report adds finished-attempt rates (1/5/15 min) and attention grouped by
  cause. A 125,000-request synthetic ledger rendered in 11 s and 1.5 GB before and
  renders in 0.15 s unchanged, about 2 s after a commit and 4.5 s cold, at about
  140 MB (`tools/diagnostics/progress_benchmark.py`).
- `store.verify` keeps one memo of the previous successful pass keyed by exact
  request bytes and artifact text with the digest they were proven to have, capped
  at `store.MEMO_BYTES` (128 MiB of keys). A hit needs identical bytes and an
  identical stored digest; failures are never cached. Upload planning
  (`upload.records_for`) reads the highest `S`/`D` number from the catalog's
  primary-key index and the IDs promised by `upload-*` requests via a bounded
  (65,536) per-process memo keyed by request digest; staged, retrying, blocked and
  dead-letter requests keep their IDs reserved.
- Evidence: `tests/test_intake_ledger_scaling.py` (alternating writers, tamper and
  replaced-ledger cases, bounded waits, progress, forged-digest memo cases, memo
  cap), `tests/test_progress_scale.py` (reference-equal counts across page sizes,
  tamper after a cached render, digest-keyed memo, single-flight), `tests/test_upload_planning.py`.
- A measured production-sized ledger (about 130 MB, 36k requests) made the
  original 16k/64k-entry caches useless for the in-order walk of `verify`; the
  memo above is the fix (about 0.9 s per pass down to about 0.14 s).

## 6. Observation: audit log and diagnostics

- **Diagnostics** (`--diagnostics-log`, off by default): a closed, body-free field
  set. Query text, locators and excerpts are unrepresentable and records that
  contradict themselves are refused. Search is recorded with its absence
  classification (`absent-corpus`, `absent-partial`, `present-uncombined`); `get`
  and `get_many` are recorded only as a bare read count with no locator. No
  read-to-search ratio is reported, because the counts cannot tell an
  unconvincing excerpt from an agent that read enough in the excerpt. Nothing reads
  the log to change selection, ranking or qualification. Evidence:
  `tests/test_kit.py` (`test_absence_never_echoes_query_terms_or_source_text`,
  `test_refuses_query_text_and_unknown_fields`, `test_reads_are_counted_but_never_attributed`,
  `test_report_counts_reads_without_deriving_a_ratio`, `test_rejects_unsafe_log_paths`),
  `tests/test_mcp.py::test_diagnostics_*`.
- **Audit log** (`--audit-log [PATH] --agent-label L`, off by default): one record
  per tool call with arguments, full returned text, character count, SHA-256 and the
  locators and record ids mentioned. Files are 0600 in a 0700 directory below the
  ignored `<root>/.local`, opened `O_APPEND|O_NOFOLLOW`, locked and fsynced per
  record. If the record cannot be written the call returns an error and not the
  result. Rotation renames at 64 MiB and never truncates, overwrites or deletes.
  The audit log is never indexed (`.local` is excluded), searched or returned by a
  tool. `librarian-audit report` prints counts and hash mismatches without text.
  Evidence: `tests/test_mcp_audit.py` (about 20 tests, including
  `test_audit_failure_withholds_the_result`, `test_rotation_preserves_every_record_without_overwriting`,
  `test_paths_outside_local_symlinks_and_unsafe_modes_are_refused`); `.local`
  exclusion: `tests/test_kit.py` (`index.eligible(".local/database.md")` is false).
  The diagnostics log and the audit log are deliberately separate files and formats.
- **The MCP adapter is read-only** and exposes six tools (`status`, `search`,
  `get`, `get_many`, `catalog`, `review_events`); `tests/test_mcp.py` asserts that
  set. Adding a write tool would be a change to that test and a design discussion.
- **Agent findings** persist through the contribution/disposition contract
  (`agents/contributions.md`), which ends in a review, not through a trace store.
  No trace endpoint exists. The reasoning (an agent's own unsourced recollection
  retrieved next to sourced material is hard for a model to distinguish, and
  compounds) is an argument, not a measurement; if a recorded case shows repeated
  work the contribution route could not capture, revisit it.

## 7. Relationship traversal

No graph capability exists (the earlier ontology and entity graph were removed,
Kata 7eme). If traversal is ever wanted, the evaluation to date favors a
rebuildable, freshness-bound SQLite projection over a graph server: it keeps the
file-based, single-writer, offline shape and cannot become an authority. The one
installation that had such a prototype measured its limit as interface
expressiveness, not storage engine, and found its candidate path queries were
hypothetical rather than failed real questions. Embedded graph engines checked in
October 2026 (Kuzu: archived upstream; Apache AGE: server extension, release
candidates only) were not adoptable then; that table dates quickly, so re-check
before relying on it. This is a weakly evidenced preference (small scale, no real
failed question), not a prohibition.

## 8. Assumptions worth revisiting

Items below were held as constraints in earlier documents and are reasoning or
owner preference rather than measured or test-enforced facts.

- One full clone per instance is the right topology (isolation without new code;
  N upgrades). Nothing measures N or the upgrade burden.
- Pinning the complete runtime and recording the interpreter minor version in the
  instance record; neither is built.
- A per-instance record of the distribution and store contract versions, and a
  per-release compatibility statement; neither is built.
- Daily snapshots with seven-day retention; seven is a default, not a derived
  number.
- Declining agent memory and the dead-weight pruning signal (sections 6): reasoned,
  not measured.
- The 5,000-entry manifest cap is an implementation limit, not a design goal. Past
  it `qualify` no longer pays for reviews it cannot select, but the cap itself is
  unmeasured above 800 real selections. Each select rewrites and revalidates the
  whole manifest (20 ms at 1,000 entries, 93 ms at 5,000, 274 ms at 20,000, so
  filling a manifest is quadratic), publish grew faster than linear from 200 to
  800 selections, and index build, reader memory and publication authorization
  were not measured at a larger size. Raising it needs those measurements.
- A single linear catalog hash chain (revisit triggers were: commit utilization
  above about a third of the ceiling with writers losing waits, multi-host
  writers, or a first-of-day hold above the 10 s wait even after the speedups).
  At 10.6k registrations on one production catalog the first of those had not been
  met.
