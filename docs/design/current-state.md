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
  `tools/context-mcp/pyproject.toml`; `tests/test_documentation.py::test_librarian_identity_and_role_documents_are_packaged`.
- **Three kinds of store**, all SQLite under a per-instance data root:

  | Kind | Stores | Rule of thumb |
  |---|---|---|
  | Authority | Catalog (`LCAT`, hash-chained immutable revisions, ABORT triggers on update/delete); review events (`LREV`, append-only hash chain with corrections, schema `contract_hash`) | Cannot be recomputed. Never rebuilt as an upgrade or recovery step (`tests/test_authority_store_upgrade_paths.py`). |
  | Operational | Intake ledger (`LINT`, exact request bytes, stages, attempts, replays; `synchronous=FULL`, rollback journal) | Private work intent. Cannot reconstruct authority. |
  | Derived | FTS5 context index (`LCTX`) with engine fingerprint, manifest hash, policy hash, audience and a logical corpus root | Rebuildable; a stale index refuses reads. |

  Evidence: `tests/test_workspace.py::test_new_application_ids_and_superseded_refusal`
  (superseded `FCAT`/`FREV`/`FCTX` identities refuse with a migration message),
  `::test_initialize_refuses_an_unmigrated_legacy_index`,
  `tests/test_catalog_inspect_equivalence.py` (about 100 tamperings of catalog
  rows, triggers, metadata and application id refuse),
  `tests/test_workspace.py::test_native_review_correction_retry_and_restore`
  (`DELETE FROM events` aborts), `tests/test_intake.py::test_wrong_identity_modes_and_missing_store_refuse`,
  `tests/test_workspace.py::test_stale_content_detected` and `::test_clean_copy_and_policy_freshness`.
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
  The file-level primitives the stores share live in `tools/durable.py`: directory
  and file fsync, exclusive 0600 creation (`reserve`, `write_new_private`) and
  chunked SHA-256, each opened `O_NOFOLLOW` (`tests/test_durable.py`). Each store
  still applies its own path rule and error type first (catalog `safe_path` also
  refuses `..`; catalog modes must be exactly 0700/0600, review-event modes must
  only deny group and other) and keeps its own `connect`, `private_directory`,
  `private_file` and `writer_lock`, whose wait, timeout and journal semantics
  differ. `tools/policy.py` keeps its own directory fsync because its bytes are
  part of the policy and extraction fingerprints.
- **Upgrade behavior.** Code never migrates an authority store implicitly: a
  store whose application id, `user_version`, `contract_version` or
  `contract_hash` differs from the installed contract refuses with an
  explicit-migration message. The derived index is assumed to need a rebuild
  after any code change, because its fingerprint covers the reader modules
  (`context_index.py`, `packets.py`, `renditions.py`, `qualification.py`,
  `compact_records.py`) and policy module bytes (`context_index.engine_fingerprint`,
  `tests/test_workspace.py::test_qualification_code_is_bound_into_engine_fingerprint`).
  Schema snapshots in `tools/schemas/*.sql` are not backups.
- **Store locations** are instance configuration (`storage_roots` in
  `config/policy*.json`), recorded with instance and store ids in
  `.local/instance.json`. `tools/initialize.py --migrate-storage` /
  `--recover-migration` move populated stores transactionally;
  `--relocate` rebinds the intake ledger root after a host-path move. The index
  binds a *logical* root (instance id, store ids, storage roots), so moving the
  instance directory keeps it fresh while substituting another instance's
  database file is refused. Evidence:
  `tests/test_workspace.py::test_portable_roots_relocation_substitution_and_migration_recovery`
  and `::test_portable_roots_edge_cases_and_instance_policy_propagation`.
- **Instance-root validation is memoized per process (Kata ck02).**
  `policy.validate_instance_roots` (policy read, symlink and ambiguity checks,
  binding read, authority-store id probes) used to run on every
  `store.locations`. A success is now cached per root and reused while the
  in-process settings and the stat identity (dev, ino, size, mtime, ctime, mode,
  or absence) of the migration lock, policy files, instance binding, every
  storage-root component and the three store files are unchanged; otherwise it
  revalidates in full. Failures are never cached, and callers get a copy. Not
  covered: a store file replaced with identical stat fields. Evidence:
  `tests/test_instance_root_cache.py` (no store reopen on repeat calls; a
  migration lock, changed storage roots, substituted store and symlinked root
  each still refused after a cached success).
- **Topology.** One instance is one full clone: its own checkout, `.venv`,
  `config/*.local.json`, inbox and stores. Policy is resolved per `--root` on
  every operation; a root without its own policy files gets only the versioned
  defaults (`tests/test_policy_isolation.py`). Instances can run different
  versions. The reason is isolation by filesystem with no cross-instance code
  path; the cost is N environments to upgrade (reasoning, not measured).
  `.local/instance.json` records ids and layout only. It deliberately does not
  record the distribution version, store contract versions or interpreter minor:
  the stores refuse a mismatched contract themselves (section 1, `contract_hash`,
  `user_version`, application id), `git log` in the clone is the distribution
  version, and nothing would read a second copy (Kata 7jkt decision).

## 2. Distribution and runtime

- **Packaging** is an explicit allowlist, `release-files.json`, built by
  `tools/package.py`. It excludes `.local`, `.venv`, databases, `data`, `sources`,
  `documents`, `*.local.*` files and symlinks. Evidence:
  `tests/test_workspace.py::test_clean_copy_and_policy_freshness` (the archive equals
  the allowlist and omits `policy.local.json`), `::test_empty_initialization`
  (the shipped toolkit seeds zero records),
  `tests/test_packaging.py::test_release_paths_rejects_missing_and_symlinked_files`
  and `::test_package_rejects_symlinked_output_parent`.
- **Allowlist coverage.** Every file tracked by Git is either listed in
  `release-files.json` or named in a short exclusion list with a reason
  (`PackageTests.UNPACKAGED_TRACKED`): `.kata.toml` (this repository's Kata
  binding), `.github/` (hosting and CI configuration) and `ruff.toml` (repository
  lint configuration no packaged code or test reads). Toolkit code, tests,
  diagnostics and documentation are packaged. Every manifest entry must be a
  tracked, existing file and none may match an exclusion. The check runs only in a
  Git checkout whose top level is the toolkit root and skips in packaged kits,
  which carry no `.git`. Evidence:
  `tests/test_packaging.py::PackageTests::test_manifest_covers_every_tracked_file`.
- **Runtime checks.** `librarian-preflight` probes Python >= 3.14, `mcp==2.2.0`
  and SQLite FTS5 in the interpreter the adapter runs on, and the adapter refuses
  to start on an incomplete clone or a symlinked toolkit path
  (`tests/test_mcp.py`). The adapter never imports the toolkit as a package: it
  runs the reader CLIs with `cwd=--root` and loads `tools/policy.py` and the
  diagnostics writer by file path. The files it relies on are one set,
  `the_librarian_mcp.config.TOOLKIT_FILES`, which the root's `release-files.json`
  must contain (`tests/test_mcp.py::test_paths_are_derived_from_the_toolkit_root`). The runtime probe refuses, with a non-zero exit and an
  explicit diagnostic, Python below 3.14, any `mcp` version other than the pin,
  and missing FTS5, and reports the Python, SQLite and `mcp` versions and
  `fts5: true` when it passes (`tests/test_preflight.py`; the real probe runs in a
  child interpreter whose facts a temporary `sitecustomize` stubs). Only `mcp` is pinned exactly; `anyio` (`>=4.10,<5`)
  and `jsonschema` (`>=4.20,<5`) are ranges whose floors are those `mcp` 2.2.0
  requires on Python 3.14 (the suite passed at both floors and at the latest
  releases, 2026-11-16), and transitive dependencies are unpinned.
  `tools/context-mcp/pyproject.toml` is the only toolkit dependency declaration;
  the review-event and context-index import errors point to it. There is no lock file,
  image or installer, by decision (Kata 7jkt, 2026-11-13). Measured drift: two
  `.venv`s of the same commit already differed in anyio, pydantic, starlette,
  sse-starlette and other transitive patch versions, and the repository history and
  Kata show no incident or test failure traced to dependency resolution. The one
  dependency whose change breaks behavior (`mcp`) is pinned and refused by
  preflight. Revisit if a real instance fails or diverges because of an unpinned
  package; the first step then is to pin that package, not to add a lock file.
- **Optional Docling worker** runs in its own interpreter with pinned assets
  (`tests/test_docling.py::test_changed_profile_or_assets_cannot_match_prepared_identity`).
  Its direct dependencies are exact pins in `tools/extraction/requirements-docling.in`;
  `requirements-docling.txt` is the full Python 3.14 Linux CPU lock the worker
  enforces at runtime, and every direct pin must appear in it unchanged
  (`tests/test_docling.py::WorkerRequirements`).
  The reader needs none of it.
- **Continuous integration** (`.github/workflows/ci.yml`) runs on pull requests
  and pushes to `main` with Python 3.14: it installs `tools/context-mcp` into a
  fresh `.venv`, then runs `ruff check`, the schema snapshot `--check` and the
  unittest suite. `ruff.toml` pins the ruff version and enforces only pyflakes
  (`F`), bugbear (`B`, minus `B007`/`B905`) and `SIM115`; formatting and line
  length are not enforced. Ruff is a CI/development tool, not a runtime
  dependency.
- The first proof-of-concept installation is closed; this repository does not
  maintain or port from it. Its clean-copy tests only prove this repository's
  packaging and policy boundary.

## 3. Intake and review

- **Inbox first.** Content enters through the supported writers: manual upload
  (`tools/intake/upload.py`), read-only cohort intake (`tools/intake/cohort.py`, which drives
  `cohort_ingest.py`, `cohort_recovery.py` and `cohort_review.py`)
  and the durable ledger (`tools/intake/*`). A copied file is not a registered
  receipt until its catalog commit is confirmed; a receipt is not extracted
  text; extracted text is not a review; a review is not selection. Connectors,
  scheduling and hosted sources are not built (ROADMAP). Evidence:
  `tests/test_intake_upload.py::test_missing_or_unmarked_drop_is_never_success`,
  `::test_blocked_receipt_is_reported_without_claiming_success`,
  `tests/test_intake_cohort.py::test_ingest_submits_and_extracts_admitted_files_with_body_free_counts`.
- **Cohort intake never writes the inbox** (apart from an explicit
  `delete-archive-original --apply`, below). Every file gets exactly one counted
  disposition; formats without a reader are `needs-reader`; a missing or
  unlistable inbox fails instead of counting nothing; the inbox tree is
  byte-identical after a run. Evidence:
  `tests/test_intake_cohort.py::test_every_file_gets_one_explicit_disposition`,
  `::test_missing_or_unlistable_inbox_fails_instead_of_counting_nothing`,
  `::test_over_limit_is_counted_not_dropped`.
- **Archives expand only on an explicit run (Kata 12sc).** `cohort expand-archives`
  (dry run unless `--apply`) reads zip and tar archives with stdlib readers into
  `<intake home>/archives/<archive sha256>/`, never into the inbox. It refuses
  unsafe member names, links, devices, encrypted or unsupported members,
  case collisions, and archives over the member-count, expanded-size or ratio caps.
  It writes a member manifest atomically and expands one nesting level per run.
  An operator's sibling `_extracted/<stem>/` folder is verified against the
  archive by hash instead. `classify` gives members their own dispositions and an
  `<archive>!/<member>` locator. `delete-archive-original` is the only inbox write:
  it refuses unless the archive and every member still match the manifest and
  every readable member is registered with the same sha256, and it appends a
  fsynced deletion record before unlinking. Evidence:
  `tests/test_intake_archives.py::ExpandTests::test_tar_links_devices_and_traversal_are_not_written`,
  `::ExpandTests::test_bomb_caps_refuse_the_whole_archive_and_store_no_members`,
  `::ExpandTests::test_nested_archives_expand_one_level_per_explicit_run`,
  `::IngestMemberTests::test_members_are_ingested_with_archive_provenance_and_rerun_is_idempotent`,
  `::DeleteOriginalTests::test_refused_until_expanded_and_every_member_registered`,
  `::DeleteOriginalTests::test_verified_dry_run_then_apply_records_before_deleting_and_is_idempotent`.
- **Cohort PDFs and images read through a hardened, provisioned runtime.** Without the
  pinned Docling interpreter and models (`pdf_image.status`, preflight
  `pdf_image_runtime`) `.pdf`/`.png`/`.jpg`/`.jpeg` stay `needs-runtime` with code
  `docling-not-provisioned`. With them, a PDF uses its embedded text layer
  (`pypdfium2` in the pinned interpreter) when every image-bearing page has at least
  200 alphanumeric characters at a ratio of 0.5 or more and the document has 200, and
  falls back to Docling OCR otherwise; images always use OCR. The rendition payload
  `format` is `pdf-text` or `ocr` (`method.kind` `pdf-text-layer` / `docling-ocr`).
  Each file converts in its own child process with address-space, CPU, wall-clock,
  file-size, page, record, output and header-pixel limits, `--extract-concurrency`
  at a time before the ledger stages; a timeout or crash is retried once, then the
  file is a blocked stage `extract-<code>` and the cohort continues. Evidence:
  `tests/test_pdf_image.py::CohortTests`, `::HardeningTests`, `::RoutingTests`,
  `::ImageLimitTests`; `::RuntimeTests` runs the real runtime when provisioned.
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
  inspection and out-of-scope receipts block; references to material outside the
  rendition are advisory. Contradictions and missing evidence the reviewer reports
  on an `accept` are attached issues, not blocks (owner decision, Kata my2k): the
  rendition qualifies and its review event carries every item verbatim as
  `Reviewer-noted contradiction: ...` / `Reviewer-noted missing evidence: ...`
  evidence statements after a count statement. Event `uncertainties` stay empty
  because native qualification refuses a review that has any, so retrieval reads
  the notes from `qualification.reviewed_scope[].noted_issues` on the selected
  rendition. The review-event contract holds at most 30 evidence statements: if
  the items do not fit, the event says how many it recorded (contradictions
  first) and points at the findings hash, never dropping silently. `reject`,
  `defer` (unless opted in to low confidence, below), incomplete inspection, independent-review, conflicting-review, scope,
  registration, fidelity and budget gates are unchanged and keep their order; the
  policy file format and sha are unchanged. `cohort recover-unresolved-evidence`
  re-evaluates only reviews blocked `standing-pending-review-unresolved-evidence`
  from their saved findings, makes no model call, stages a correction for each
  that now qualifies (the ledger stays append-only) and selects it under the
  shared manifest's capacity. Model traffic stays within the
  configured provider and redirects are refused. Evidence:
  `tests/test_standing.py` (`test_fabricated_success_without_inspection_is_not_qualified`,
  `test_confidence_never_yields_verification`, `test_prohibited_scope`,
  `test_accepted_review_with_reported_issues_qualifies_and_attaches_them`,
  `test_rejected_and_deferred_stay_so_even_with_reported_issues`,
  `test_issues_never_rescue_incomplete_inspection`,
  `test_issues_never_override_a_conflicting_review`,
  `test_every_issue_is_recorded_or_the_truncation_is_stated`,
  `test_retrieval_surfaces_the_issues_a_qualified_document_carries`,
  `test_advisory_references_do_not_block_...`),
  `tests/test_intake_cohort.py::RecoverUnresolvedEvidenceTests`, `tests/test_model_review.py`
  (`test_control_task_must_not_be_accepted` covers a synthetic injection control,
  `test_redirects_are_refused`, `test_usai_transport_refuses_unconfigured_models_and_providers`).
- **Low-confidence information is published and marked, not withheld** (owner direction, Kata
  chwz). Every indexed document has a `confidence-v1` label `{"level": "normal"|"low",
  "reasons": [{"code", "details"}]}`, stored in the index's `document_confidence` table and
  bound into the rendition identity, so a label change makes the index stale. Reason codes are
  stable; `tools/context_index/confidence.py` holds each code's explanation and how to correct it
  ("confirm or correct via a newer document, meeting transcript or answered question"):
  `noted-issues` (an accepted review carrying `Reviewer-noted ...` statements, so existing
  my2k qualifications become low confidence at the next rebuild without re-review),
  `reviewer-deferred` (a `Reviewer-deferred:` statement, see below), `converted-format` and
  `machine-ocr` (from `RENDITION_REASONS`, keyed by `rendition_kind`: `docling-<format>` or the
  extraction payload format; `docling-pdf`, `docling-image` and cohort `ocr` are `machine-ocr`, a cohort PDF text layer
  (`pdf-text`) is `converted-format`, text, MHTML,
  Gemini transcript and ODT renditions add no reason), `agent-generated` (an upload whose
  version-2 descriptor declares `origin.agent_generated: {generator, generated_on,
  source_commit}`; the hash-pinned `submission.json` sidecar is read at build and a missing,
  changed or malformed one fails the build) and `open-correction` (added at read time by
  `with_reason` for an open claim correction, below; never stored in the index). Markdown documents are `normal`. `search` hits carry `{level, reasons:
  [codes]}` and `--low-confidence include|last|exclude` (default `include`, rank order) ranks
  low hits after normal ones or leaves them out while counting them in
  `low_confidence_excluded` (an empty result caused only by that filter reports
  `filtered: low-confidence-excluded` instead of an absence classification); `get` pieces, compact `documents` (`qualified-compact-v2`) and
  `get_many` documents (`qualified-many-*-v2`) carry the full label, and each read has a
  top-level `confidence` block with the worst level and each present reason's explanation and
  correction. `status` (and intake progress) report `confidence: {normal, low, low_reasons}`;
  the catalog has no label. An index built before this change is stale (engine fingerprint, and
  `confidence schema missing; run build`) and is rebuilt once. A reviewer `defer` still holds
  (`standing-deferred-reviewer-deferred`) unless the operator opts in with
  `cohort qualify --deferred qualify-low-confidence` (or `cohort recover-deferred --apply` for
  held ones, from saved findings, no model call). The opt-in is the optional `deferred`
  argument of the standing-review/select stages (absent from requests staged without it, so
  the standing-policy file and its sha are unchanged); a deferral with complete inspected scope
  then qualifies with reasons `complete-inspected-scope`, `reviewer-deferred-low-confidence` and
  an event `Reviewer-deferred:` statement. `reject`, incomplete inspection and every other gate
  still stop it; deferrals reviewed under an older policy are not touched. Evidence:
  `tests/test_standing.py` (`test_opted_in_deferral_qualifies_as_low_confidence`,
  `test_deferral_opt_in_never_rescues_rejection_or_the_other_gates`,
  `test_noted_issues_and_machine_reading_mark_text_low_confidence`,
  `test_opted_in_deferral_publishes_visibly_marked_low_confidence`,
  `test_deferral_opt_in_is_a_contract_checked_stage_argument`,
  `test_text_renditions_are_normal_and_rendition_kinds_map_to_reasons`,
  `test_open_correction_hook_adds_a_reason_to_an_existing_label`,
  `test_an_index_without_the_confidence_table_is_stale`),
  `tests/test_intake_cohort.py::DeferredLowConfidenceTests`,
  `tests/test_intake_upload.py::test_agent_generated_origin_is_declared_in_a_v2_sidecar_and_labels_low_confidence`,
  `tests/test_mcp.py::test_mcp_uses_instance_local_selection_and_storage_roots`.
- **Claim corrections mark indexed claims shaky or confirmed without editing them** (owner
  direction, Kata 5dr5). `python -m tools.review_events.corrections` keeps an append-only store at
  `<review_events_home>/corrections/corrections.sqlite3` (contract `claim-correction-v1`, schema
  `tools/schemas/corrections.sql`; hash-chained records and batches, immutable triggers, full
  validation on every read, writer lock, `--expected-state`, idempotent `--request-id`, actor
  attribution, daily and manual snapshots, `verify-backup`, same-lineage `restore` with a
  safety backup). It is a separate store, not a review-event kind: a new kind would change the
  review-event contract hash existing stores pin, and review-event appends change the review
  state Docling selections bind, so every correction would force a rebuild. A `correct` (with
  the corrected statement) or `dispute` quotes a claim at an indexed locator; recording needs a
  fresh index and the claim in that section's text (whitespace-collapsed), and stores the
  locator, path, coordinates, source hash, rendition receipt, claim and its SHA-256. Every record
  except `retract` cites a source: a `document` or `transcript` receipt that is in the catalog and
  differs from the corrected document's receipt, or a `question` with answer, `answered_by` and
  `answered_on`. `confirm` and `retract` resolve open records on the same locator and claim (a
  record resolves once; a new correction may supersede earlier ones). `intake FILE` records a
  `claim-corrections-v1` JSON file (shared `source`, up to 100 corrections) that accompanies a
  newer document or transcript in one all-or-nothing transaction. Retrieval applies the store at
  read time (`tools/context_index/corrections.py`), so recording needs no rebuild: `get` and
  `get_many` pieces whose text overlaps the corrected section and contains the claim list
  `corrections` (claim, statement, summary, source, status, resolver, `drift`), and an open
  record adds `open-correction` with its summary and source to that piece's confidence and the
  packet's top-level block. Compact and `get_many` documents keep the document label; a piece
  carries its own label only when a correction lowers it. `search` hits (which index a section's
  lead text) list open and resolved correction IDs when that lead text holds the claim, and
  `--low-confidence last|exclude` treats those sections as low. A confirm restores normal
  confidence only for the claims it resolves; other reasons stay. Drift is reported, never
  hidden: `source-changed` (informational), `claim-not-found` (the claim left the section; the
  correction then applies to all text overlapping it), `locator-missing` (e.g. a renamed heading;
  it follows the claim within the same document), `locator-and-claim-missing` (it marks the
  whole document) and `document-missing` (shown only in status).
  `status` reports `claim_corrections` counts including open drifted ones. No store means reads
  are unchanged; an invalid store refuses the read. Evidence: `tests/test_corrections.py`.
- **Only an absent registration is `unregistered-rendition`.** The standing check reads a
  receipt's registration from one catalog state. The catalog raises `MissingRecord` for an absent
  record or receipt, and only that, or a malformed receipt ID, yields the `unregistered-rendition`
  exception. A commit landing between the two reads is re-read up to three times, then is retryable
  `catalog-stale-state`. A SQLite or filesystem error that the runner would retry (lock contention,
  a transient errno) stays retryable. Any other catalog failure raises
  `standing.CatalogUnavailable`, for example `Catalog validator changed after import` when the
  checkout is updated under a running process, a malformed database or a permission error. It is not a `CatalogError`, so the adapters do not save it as a
  `native-input` failure: it stops `qualify` (`{"error": "catalog_unavailable"}`, exit 2) and a
  restarted run re-checks the same stage. `qualify` restages a review saved as
  `unregistered-rendition` as a correction once `standing.registered` confirms the binding. The
  correction reuses the saved findings, so it makes no model call, and a still-absent
  registration is left as it is. Evidence: `tests/test_standing.py::test_only_an_absent_registration_is_unregistered`,
  `tests/test_intake_cohort.py::test_unreadable_catalog_stops_qualify_without_saving_an_outcome`,
  `::test_malformed_catalog_stops_qualify_without_saving_an_outcome`,
  `::test_unpermitted_catalog_stops_qualify_without_saving_an_outcome`,
  `::test_review_saved_as_unregistered_is_corrected_once_the_catalog_confirms_it`.
- **Review throughput is bounded by the operator's cap and changes no outcome.** `qualify
  --concurrency N` is the most model calls (HTTP attempts: a retry takes a slot again after its backoff wait and
  none is held while waiting) ever in flight. Every attempt of a run passes one
  `throttle.Limiter` (AIMD): it starts at `--initial-concurrency` (default N), halves on a 429
  or 503 (a burst of simultaneous signals costs one halving), and adds one back per window of
  clean replies, never above N or below 1; other errors do not move it. The limiter only
  decides how many calls overlap: staging, review, selection, manifest and usage accounting
  stay on the calling thread in inbox order, and `standing.evaluate` is still the only
  authority on every entry. `report["throughput"]` is the effective rate and what the limit did
  (timing-dependent, so apart from `counts` and `entries`). With `--inspected-form ranges` the
  reviewer is asked to write `inspected_records` as `"first-last"` strings; `_parse` expands them
  (bounded by the rendition's record count; overlaps, backwards ranges and out-of-range indices
  are an invalid reply) to the same sorted explicit list that is pinned, so the findings bytes,
  the standing check and the events are identical to the list form, and a gap stays an
  uninspected record. The list is self-attested in either form. The default stays `list`
  pending the owner-run A/B (`tools/diagnostics/inspection_form_ab.py`). Evidence:
  `tests/test_throttle.py`, `tests/test_intake_cohort.py::ThroughputTests` (identical entries,
  findings, events, ledger and manifest across adaptive/fixed and list/ranges against a counting
  stub; restart with zero calls), `tests/test_model_review.py::RangeFindingsTests`.
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
- **A derived `docs` table answers "what is left" and lets a replay skip stat-identical files
  (Kata e22r).** The intake ledger holds one `docs` row per cohort document (system and
  inbox-relative path; format, submission id, SHA-256, receipt, retained original, rendition and its
  hash, state, conflict count, the heads of its extraction and registration chains, the file's
  `(dev, ino, size, mtime_ns, ctime_ns)` and the last run). The requests stay authoritative: every
  ledger-derived column is a function of requests, stages and attempts. `runner` refreshes the rows
  that follow a request in the same transaction as each stage update (running, failed, succeeded,
  replay), `store.stage` repoints them when a correction is staged, and `ingest` writes a row only
  when it differs from what it would write, so replaying an unchanged inbox writes nothing.
  `cohort docs-status` is `GROUP BY state` on an index; `cohort audit-docs` recomputes every row from
  the requests (the cohort and path come from the catalog-pinned submission sidecar) and reports
  `missing`, `extra` and `differing` rows without writing; `cohort backfill-docs [--apply]` creates the
  table and adds the missing rows for an existing ledger. The table is additive and optional: the
  ledger schema version stays 1, the five authoritative tables are never written by the backfill, a
  ledger without `docs` runs exactly as before and is never migrated by `ingest`, and dropping the
  table loses nothing (`backfill-docs --apply` rebuilds it). Stat identity is an observation, so the
  backfill leaves it empty and the first default replay fills it, and a row's stat is kept only when
  the hash read at that time equals the retained original's.
  **Change detection is unchanged by default:** every file is still read and hashed. Only
  `ingest --trust-stat` (and `qualify --trust-stat`, which passes it to its ingest) reports an extracted document straight from its row, without reading,
  hashing or looking it up in the ledger, when its stat identity equals the row's; a same-size edit
  that restores the mtime still changes `ctime`, but a tool that also resets `ctime` is outside what
  stat can show, which is why the mode is opt-in (efz3 decision D8). A row never vouches for a chain
  `ingest` would refuse: a head that does not repeat the document's operation, receipt and format is
  state `mismatch`, a branching chain is `ambiguous`, and an upload correction repoints the row in the
  staging transaction and drops the old stat identity; none of these is trusted. Evidence:
  `tests/test_intake_docs.py` (recording, same-transaction rollback, correction repointing, backfill
  and audit, same-size edit with restored mtime, rename-over, truncate, append and touch with and
  without `--trust-stat`, report equality with and without it),
  `tests/test_intake_cohort.py::test_a_trusted_qualify_replay_skips_unchanged_files_and_qualifies_alike`.
- **Extraction parses each document once per ingest and re-checks it four times.**
  `extract.bundle` reads the receipt's catalog snapshot, reads and hashes the original,
  recomputes the extractor identity (code and policy) and parses. A first ingest ran it six
  times per document (each stage's prepare, its changed-identity guard and `run`'s own first
  call). It now runs four: per stage, the pinning `prepare` (or the saved intent on a retry) and
  one guard bundle that `extract.run` then reuses (`prepared=`). The parse is kept for the newest
  document only, keyed by format, observed source SHA-256 and extractor SHA-256, so a changed source
  or extractor is a different key and is parsed again; a failed parse is never kept. The re-checks
  that remain, each with its own fresh read of the catalog, file and extractor, are: (1) `prepare`
  pins the identity; (2) the stage's guard refuses a changed source, catalog record or extractor
  (`changed_extraction_identity`) before anything is published or submitted, because the intent may
  be hours old on a retry; and under the catalog writer lock `unchanged` runs (3) before
  `prepare` publishes the rendition, (4) after that publication (for `register`, instead, once
  before the saved registration is read), (5) before the registration is submitted and (6)
  after its completion record is written. (3) to (6) re-read and re-hash the original and
  recompute the extractor identity and never use the reused bundle, because the lock is the
  boundary at which another writer or editor can act, and each stands between two durable
  effects. Dropped: `run`'s own
  first `bundle`, which re-read what the guard had read microseconds earlier and was followed
  by (3) or (4) anyway. Evidence: `tests/test_extraction_reuse.py`
  (one parse, four bundles, a source or extractor changed between prepare and register, after the
  intent was saved, during publication or before submission still blocks),
  `tests/test_intake_cohort.py::BlockedCase`.
- **`qualify` and shared manifests.** An instance may ingest several inboxes
  (one `--system` label each) into one catalog, manifest and index. A manifest
  entry belongs to a cohort when its rendition was extracted by the current run
  or its receipt's pinned `local-path` sidecar names that system; anything that
  cannot be proven that way is left alone, and orphans are dropped and counted.
  The manifest read-modify-write holds the ledger writer lock and replacement is
  atomic; adding one new selection is a constant-time in-place append
  (`tools/intake/selection_manifest.py`, Kata m0tg): a view (count, selected paths, running
  SHA-256) is kept in memory for the file's identity after one validated read, the new entry
  alone is validated, and its canonical bytes plus the fixed closing tail are written at the end
  of the last selection, giving exactly the bytes a full rewrite would. An fsynced intent file
  written first lets the next writer roll a crashed append forward (`recover`, called under
  the lock and before `qualify`/`publish` read the manifest). A replacement, an empty list, a
  non-canonical file or a stale view takes the full read, validate and atomic rewrite. The
  manifest holds at most `renditions.MAX_SELECTIONS` (50,000) entries
  across all cohorts (sized for a 36,000-document cohort at the observed 37%
  acceptance, about 13,000 selections, with headroom; Kata jysk). An in-scope entry with no saved review is not sent to the
  model when the manifest has no room for it: `qualify` checks the room (an
  exact read, 2 ms at 5,000 entries and linear) on the main thread just before the call,
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
- **Publishing a cohort-sized manifest (Kata jysk).** Publishing validates every
  selection (`renditions.inputs`: catalog records, original and rendition
  bytes, review binding) and runs it several times per publish. Three
  per-selection costs that did not need to be per selection are gone: the
  review-event id map was rebuilt per selection (an O(selections x events)
  build, now once per snapshot, `Snapshot.event_index`); the storage policy was
  re-read and re-validated for every original-path check (now once per
  operation, `policy.allowed_in`); and every selected file re-walked the
  instance root's own ancestors for symlinks (now the components below the root
  are checked each time, the root's ancestors once per call). Publication also
  derived the same index metadata twice more than needed: the index-build stage
  reuses the metadata `prepare` has just derived and pinned while the manifest
  hash still equals the pinned one (`adapters.pinned_metadata`), and
  `availability` derives it once for the freshness check and the evidence
  comparison. `renditions.inputs` ran 9 times per publish and now runs 7; an
  unchanged re-publish ran it twice and now once. Nothing a publish verifies is
  skipped: every selection is still fully re-validated by each remaining call.
  Measured with `tools/diagnostics/publish_benchmark.py` on synthetic text
  selections (host shared, ratios more reliable than seconds): first publish
  44 s before and 34 s after at 5,000 selections (a loaded run: 143 s before,
  34 s after); unchanged re-publish 9.7 s before and 1.9 s after; after, at
  10,000 / 15,000 / 20,000: 26 s / 39 s / 53 s first publish (about 2.6 ms
  per selection, linear) and 3.6 s / 5.5 s / 7.7 s re-publish; peak memory 0.7 /
  1.0 / 1.4 GB. Budget at the cap: a first publish under one minute and a
  re-publish under ten seconds on that host (those budgets were set at the
  former 20,000 cap). At the 50,000 cap (Kata q4ks, same synthetic benchmark, shared host): first
  publish 220 s, unchanged re-publish 32 s, peak memory 3.6 GB. The cap change and the code edits
  change `engine_fingerprint` (it hashes `renditions.py`, `qualification.py`,
  `context_index.py` and `policy.py`), so every existing index is stale once
  and is rebuilt by the next publish; no manifest, store or published output
  changes. Evidence: `tests/test_publish_scale.py` (constant policy reads and
  one event index regardless of selection count, the cap boundary, a
  publish that serves every selection).
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
  50,000-entry list cap; an existing policy is never rewritten or widened.
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

- Catalog, review-event and claim-correction writes take a verified `daily` snapshot before the
  commit only when the store has no snapshot for the current UTC day whose receipt is
  well formed, names this store and database, and whose file size equals the receipt's `bytes`;
  the per-write gate never reads the snapshot body, so it does not detect same-size
  corruption or tampering (the SHA-256 and validator are checked when the snapshot is taken,
  before retention deletes anything, and by `restore`/audit); an
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
  unless its hash equals the image (`tools/catalog/backups.py`, "Backup bytes mismatch").
  Fresh verification checks fields, size, SHA-256 and receipt state instead of a
  third replay; every older snapshot is still fully verified before it counts.
- Evidence: `tests/test_backups.py` (daily gate reads no snapshot body, truncated or
  malformed/foreign-receipt snapshots do not count, a same-size corrupt snapshot passes the gate
  but fails `verify_backup`, window pruning, never deleting the only verified
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
   ctime_ns, SQLite change counter) plus the validator-file hash (`VALIDATOR_FILES`:
   `catalog_store.py`, `storage.py`, `backups.py` and `contracts.py`) and runtime
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
fixtures only. Each runs as a script and, through `tools/diagnostics/_harness.py`, imports `tools`
from the checkout given by `--tree` (default: its own), so two revisions can be compared on the same
fixtures.

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

- **Structures** (`LOOKUP_OBJECTS` in `tools/catalog/storage.py`, mirrored in `tools/schemas/catalog.sql`):
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
- **Receipt-scoped reads.** On an `indexed` catalog, `read_records(receipt=...)` (the standing
  registration check reads one per review and select stage) computes the receipt's scope with the same
  `EXISTS` probes on the lookup indexes and filters by that id set; a `legacy` or partial catalog keeps
  the scan form. Both forms return the same records and totals. Measured on a 236,084-record production
  catalog: about 450 ms per scoped read in the scan form, under 1 ms with the probes. Evidence:
  `tests/test_catalog_lookup_indexes.py::test_a_receipt_scoped_read_probes_the_indexes` and the
  receipt-scoped comparison in `test_random_histories_decide_alike_with_and_without_the_indexes`.
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
  connection. The catalog and review-event `connect` share its `KeyedConnection`,
  which remembers the (device, inode) opened so a memo is never bound to a file
  moved into the path later. Evidence: `tests/test_file_identity_locks.py`.

### Review-event validation

Every read of the review-event history (`event_reader.read_snapshot`, `event_store.status`,
`append`) used to validate all of it: SQLite integrity, hash chains, schema, correction
index. `qualify` did that about nine times per entry, so its serial toolkit cost grew about
1.1 ms per prior event (Kata r71k, from efz3).

- **Memo.** `event_history.inspect(..., reuse=True)` (`tools/review_events/event_history.py`,
  which also holds the store's SQL and validation) keeps the last validated result per
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
- **Read locks and busy waits (Kata xeb8).** Review-event connections used SQLite's busy
  timeout 0, and a full replay (a cold `progress` render, a publication or the index
  freshness check's `read_snapshot(full=True)`) held its read transaction, and so its
  SHARED lock, for the whole O(history) validation (6.5 s at 13,000 events). A running
  `progress-serve` therefore made concurrent review-event appends fail at once with
  `sqlite3.OperationalError`, which intake records as retryable `sqlite-operational`.
  Now `event_store.BUSY_TIMEOUT_SECONDS` (10 s, as the catalog's) is the busy wait of
  every review-event connection, and `event_reader.read_snapshot` passes `release=True` to `event_history.view`,
  so a full pass reads every row inside one transaction and commits it before any
  validation; its longest hold at 13,000 events fell from 6.5 s to 0.04 s. The result is
  still one snapshot, memoized only if the file identity is unchanged afterwards (the
  rule above). Journal mode, `synchronous` and the identity proofs are unchanged. Tests:
  `tests/test_progress_contention.py` (a writer with a 0.4 s wait commits throughout a
  1.5 s replay, in `read_snapshot(full=True)` and in the cold `progress` read; a writer
  waits out a reader's brief hold). Measured holds for the other stores, which did not
  cause the failures, are under "Intake ledger" and `progress` below.
- **Invalidation.** Any commit (by this or another process), restore, swapped file,
  same-size edit (even with a restored mtime, because `utime` moves ctime), changed
  contract or new UTC day misses and revalidates everything with the unchanged
  refusals. `event_store.py audit` (and `status(audit=True)`) always replays
  and first forgets the file, so a refusing audit leaves nothing to serve.
- **Cost.** Synthetic, zero-latency stub, concurrency 1, `nice`d on a shared host
  (`qualify` per-entry serial ms, first decile to last decile): 400 documents 89 to 432
  before, 66 to 149 after (mean 257 to 104); 1,200 documents 168 to 1,425 before, 120 to 388
  after (mean 770 to 242; qualify total 937 s to 303 s). Results, event ids and the manifest are unchanged (the `qualify` tests
  are the same). The memo left each commit costing one full validation at the next read, each
  append validating `existing + incoming`, and several snapshot reads per entry (Kata m0tg
  removes those; below).
- **Delta verification** (Kata m0tg). When the file's identity moved only the way SQLite's own
  commit moves it (same device and inode, not smaller, a new change counter) and the remembered
  chain heads are still in the file, `event_history.view` verifies only the rows past them:
  their hash chain from the remembered head, batch chain and membership, payload hashes,
  the contract and the correction links (`validate_delta`, which checks the new events against
  the memoized `History` index through the `known`/`taken` hooks of `review_events.validate`),
  and that the correction table grew by exactly the new links. `append` likewise checks only
  its incoming events (duplicate ID, imported origin, correction targets, conflicting branches)
  against that index. Any failure of the delta falls back to the full pass, which refuses with
  its exact message. **Rows before the remembered head are not read again until the next full
  check**: `audit`, a backup, publication (`read_snapshot(root, full=True)`, used by
  `standing_publication.authorize_publication` and by index build and freshness, so publication still re-validates
  everything it serves), the first check of a new UTC day, a different contract, a swapped file,
  or any identity change SQLite did not make (an out-of-band write leaves the counter alone and
  is refused at once, as before). The residual below widens by one case: an edit to an old
  row that also bumps the change counter and leaves the heads alone is served until that next
  full check. `History` indexes events by ID and by source locator, so `Snapshot.event` and
  `Snapshot.at` are constant time, and `qualify`'s standing adapters share one snapshot per
  stage (`adapters.OneRead`) instead of reading four to five times. A snapshot builds its `events` list and `successors` map only on first use, so state, indexed lookups and `append` do no work proportional to the history. Measured with `tools/diagnostics/review_event_scale_benchmark.py` (synthetic, `nice`d): per-entry read, lookup and one-event append stay flat at about 1 ms, 5 microseconds and 15 ms from 1,000 to 13,000 events; replaying the whole history (`--no-delta`, the earlier behaviour) costs 72 ms and 215 ms at 1,000 events and 317 ms and 697 ms at 4,000. Evidence:
  `tests/test_review_event_memo.py::DeltaVerification` and the memo tamper cases (every
  existing refusal still refuses), `tests/test_standing.py`.
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
  measured. Cohort `ingest` therefore batches (next section): the lock is taken, the head
  validated and the daily snapshot checked once per chunk of documents.

### Batched registration (`catalog.submit_batch`)

`catalog_store.submit_batch(root, members, actor=, reason=, rebase=True)` commits many documents'
records as ONE ordinary revision (one `revisions` row, `record_count` the sum of the members' records,
the usual hash chain and input hash; `submit` and `submit_batch` share `_writer` and `_commit_revision`).
Cohort `ingest` (`--batch-size`, default 50; 1 keeps `submit`, two revisions per document) uses it for
the receipt stage and again for the extraction-registration stage (`extract.register_batch`).

- **Member idempotency is a marker record.** Each member names a `marker`: a record it creates whose ID no
  other member or request creates (`A-upload-<submission>-original` for a receipt, `A-extract-<run>` for a
  registration). A member whose marker already has a first version equal to the member's own marker record is
  `present`: reported with the revision that wrote it and that revision's state, never written again. The same
  marker with other content is refused (`ConflictingWrite`). Batch revisions get the request ID
  `batch-<hash of the written markers>`, so a retry that writes a different subset never reuses one. `submit(...,
  marker=)` gives the one-at-a-time path the same property, so a document the batched path committed is
  idempotent under a stale pin there too. Only stages whose records contain exactly one receipt naming an
  original artifact (`adapters.receipt_marker`) batch; any other `catalog-submit` keeps its request ID.
- **Each member is judged as if committed alone, in order.** Its records must validate; no earlier accepted member
  may write one of its record IDs; its pin must be the head or (with `rebase`) a state in this history after which
  no revision wrote one of its records (`ConflictingWrite` or `StaleState` otherwise, for that member only).
  Accepted members go through one `validate_delta` over the union; only if the union is refused is each member
  validated in order over a layer of the earlier accepted members, which finds the refused ones (bisection by one
  pass, not by repeated commits). The write is one transaction: all accepted members or none; a guard or SQLite
  failure rolls the whole batch back, and a failure after COMMIT reports the committed revision for a retry.
- **Ledger.** `runner.run_batch` / `settle_batch` give every stage its own attempt, intent, evidence and status
  (`failure_status` is the one rule shared with `run`); a chunk's intents and attempts are one ledger commit
  before the native work and evidence is one commit after, a refused stage is recorded on that stage only, and a
  lost catalog race stays within the contention budget. Evidence names the committed revision, which the
  chunk's documents share, so a chunk's stages settle together.
- **Crash model.** A kill before the catalog COMMIT leaves SQLite's rollback journal and the prior revision
  (recovered like any killed catalog writer); a kill after it leaves the batch committed with stages still
  `running`, and the re-run finds each marker present and records its evidence; a kill between ledger members leaves
  an unfinished attempt that is reused. Retained copies, `registration.json` and `completion.json` are written
  without replacement and checked by content, so they too are completed, never duplicated.
- **Snapshots and receipts.** A chunk is one commit: one first-write-of-the-UTC-day replay and snapshot, one
  validation receipt, one lock hold. The lock is held for the chunk's validation and write (about the cost of one
  `submit` plus per-record encoding), not for the model-free extraction.
- **Compatibility.** The schema, `VERSION`, state tokens and revision hash chain are unchanged; `inspect` replays
  batch and single revisions identically, and history written before batching stays valid.

Evidence: `tests/test_catalog_batch.py` (one revision, idempotent markers, per-member rebase, refusal isolation
and equivalence with committing one by one on random inputs, kills before COMMIT/after COMMIT/after the receipt,
a failure between members, two-process contention) and `tests/test_intake_batch.py` (batched equals one-at-a-time
end state, revisions per chunk, failed documents isolated, a kill at 14 points of the chunk completed by a re-run
with no duplicate or lost document, two processes ingesting one instance). Measure with
`tools/diagnostics/batch_ingest_benchmark.py`.

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
  `settle` and cohort ingest retry up to three times. `progress` (`progress.read_ledger`) reads the ledger in
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
  writes a receipt (`catalog_store.observed_state`): it trusts a receipt covering this exact file or
  this process's own full inspection today, and an unreadable receipt is ignored, never reported,
  so the full inspection decides. `progress-serve` (`progress_serve.Latest`) renders one page at a time, shares the
  result with requests that arrive meanwhile and reuses one younger than a second;
  the report adds finished-attempt rates (1/5/15 min) and attention grouped by
  cause. A 125,000-request synthetic ledger rendered in 11 s and 1.5 GB before and
  renders in 0.15 s unchanged, about 2 s after a commit and 4.5 s cold, at about
  140 MB (`tools/diagnostics/progress_benchmark.py`).
- `progress` counts only correction-chain heads. It reads the set of requests that
  some other request names in `correction_of` once per pass (`SELECT DISTINCT`, no
  per-row probe), still verifies those rows with their page, and leaves them out of
  the funnel, stage matrix, `exceptions`, `retrying`, per-run aggregates, attention
  groups and items and `recent`. They are reported as `superseded` in `overall` and
  each run, and in the text and HTML output, so counts reconcile with the ledger
  (the ledger itself is unchanged). Test: `SupersededTests` in
  `tests/test_progress_scale.py` (a chain of three, one uncorrected block).
- `store.verify` keeps one memo of the previous successful pass keyed by exact
  request bytes and artifact text with the digest they were proven to have, capped
  at `store.MEMO_BYTES` (128 MiB of keys). A hit needs identical bytes and an
  identical stored digest; failures are never cached. Upload planning
  (`upload.records_for`) reads the highest `S`/`D` number from the catalog's
  primary-key index and the IDs promised by `upload-*` requests. Those come from a
  per-process index that, like `store.Heads`, follows the ledger by row id and reads
  only the `upload-*` requests added since its last call (each checked against its
  stored digest and decoded once); another ledger file (device and inode) or a smaller
  highest row id resets it, and a ledger holding more than `upload.INDEX_LIMIT`
  promised IDs falls back to the earlier bounded (65,536) digest-keyed memo scan.
  Staged, retrying, blocked and dead-letter requests keep their IDs reserved.
- `requests` has a partial index `requests_correction` on `correction_of WHERE correction_of IS NOT
  NULL` (Kata 5m1h), so every "who corrects this request" lookup (`docs.repoint`, `_descendants`,
  `store.head`, the cohort and runner head checks) is a search, not a scan of the whole table. A new
  ledger gets it from `DDL`; a ledger created earlier gets the same `CREATE INDEX IF NOT EXISTS` from
  the next writer to open it, inside that writer's lock and after validation, as one small committed
  transaction. The schema version stays 1, no row is written, a ledger that already has the index is
  not touched, and a reader or writer that holds the file past the lock wait makes the upgrade a
  retryable `ledger_busy` that a later writer completes. Evidence:
  `tests/test_intake_ledger_scaling.py::CorrectionIndexTests` (query plans, new and old ledger,
  idempotence, attestation hand-off, busy reader).
- Evidence: `tests/test_intake_ledger_scaling.py` (alternating writers, tamper and
  replaced-ledger cases, bounded waits, progress, forged-digest memo cases, memo
  cap), `tests/test_progress_scale.py` (reference-equal counts across page sizes,
  tamper after a cached render, digest-keyed memo, single-flight), `tests/test_upload_planning.py`
  (including `IncrementalTests` for the row-id index).
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
  `tests/test_retrieval_diagnostics.py` (`test_absence_never_echoes_query_terms_or_source_text`,
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
  exclusion: `tests/test_workspace.py` (`index.eligible(".local/database.md")` is false).
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
- Not building a full runtime pin or lock file, a per-instance version record or a
  per-release compatibility statement (Kata 7jkt). Drift between instances was
  measured (transitive patch versions differ) without any recorded harm; store
  contract mismatches are refused by the stores, not by a note. Reasoning plus a
  small measurement, not a guarantee.
- Daily snapshots with seven-day retention; seven is a default, not a derived
  number.
- Declining agent memory and the dead-weight pruning signal (sections 6): reasoned,
  not measured.
- The 50,000-entry manifest cap is an implementation limit, a runaway guard, not a design
  goal. It replaced 20,000 (Kata q4ks, when about 11,600 reviewed selections sat staged
  behind a full manifest), which had replaced 5,000 (Kata jysk) once publishing was linear
  in selections (see "Publishing a cohort-sized manifest" in section 3). Measured at 50,000:
  see that section. Not measured above 50,000.
  Before Kata m0tg each select rewrote and revalidated the whole manifest (20 ms
  at 1,000 entries, 93 ms at 5,000, 274 ms at 20,000, so filling a manifest was
  quadratic); the append is flat.
- A single linear catalog hash chain (revisit triggers were: commit utilization
  above about a third of the ceiling with writers losing waits, multi-host
  writers, or a first-of-day hold above the 10 s wait even after the speedups).
  At 10.6k registrations on one production catalog the first of those had not been
  met.
