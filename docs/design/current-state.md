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
  | Authority | Catalog (`LCAT`, hash-chained immutable revisions, ABORT triggers on update/delete); review events (`LREV`, append-only hash chain with corrections, schema `contract_hash`); claim corrections (`LRCO`, optional, hash-chained records and batches, ABORT triggers, `synchronous=FULL`) | Cannot be recomputed. Never rebuilt as an upgrade or recovery step (`tests/test_authority_store_upgrade_paths.py`). |
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
- **Policy and path-safety checks are reused within an ingest chunk (Kata ajmk).**
  `tools/validation_scope.py` opens a thread-local scope around the chunk loop of
  `cohort_ingest._process_all` and `boundary()` empties it at the start of every
  chunk; outside a scope nothing is cached and every check runs in full as before.
  Inside one: (1) `safe_path`, `store.safe` and the policy symlink checks always
  `lstat` the path and its parent, and stop walking up at an ancestor whose
  `lstat` (device, inode, mode) equals the one recorded when it was found clean;
  a path that does not exist yet (the target of a write that creates it) gets the
  full walk, and only existence probes of SQLite sidecars and journals use the
  cache; (2) `policy.for_root` returns a copy of the policy it loaded for that
  root while the (device, inode, size, mtime_ns, ctime_ns, mode) identity of every
  policy file, the policy directories' symlink checks and the in-process
  `SETTINGS` are unchanged, and `policy.fingerprint` / `extract.fingerprint` are
  remembered under those identities plus the source files' identity;
  (3) `validate_instance_roots` reuses a result confirmed in full earlier in the
  chunk after comparing the migration lock, policy files, instance binding and
  store files, and skips the storage-root directory components until the next
  boundary. Refusals are never cached, nothing is shared across roots, threads or
  processes, and `O_NOFOLLOW` opens, permission checks and store probes are
  unchanged. What a chunk can miss: a symlink swapped into an unchanged higher
  ancestor, or a storage-root component, after it was confirmed within the same
  chunk; it is refused at the next chunk boundary (at most `BATCH_SIZE` documents
  later), before any further chunk writes. Evidence: `tests/test_validation_scope.py`
  (symlinked or replaced ancestor, root swapped for a symlink between chunks, policy
  edited between chunks and mid-scope, migration lock, substituted store, loosened
  permissions, no sharing across roots, threads or processes, identical catalog and
  report with and without the scope). Measurements: Kata ajmk and its PR.
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
- **License.** The repository takes GSA's default project license: `LICENSE.md`
  (US-government public domain plus the CC0 1.0 Universal waiver), with matching
  `CONTRIBUTING.md` and `SECURITY.md`, all packaged. The adapter declares
  `license = "CC0-1.0"` and ships an identical `tools/context-mcp/LICENSE.md`
  (setuptools cannot read above the package directory). The repository vendors no
  third-party code; licenses of the fetched Docling environment and model assets
  are listed in `tools/extraction/README.md`. Evidence: `tests/test_license.py`.
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
- **Fresh-start check.** `librarian-preflight --verify-start` (`preflight.fresh_start`)
  starts a new server process as a client does (default client environment, so no
  `PYTHONPATH` or inherited venv; working directory outside the instance; the
  console script beside the interpreter unless `--server-command` names the
  client's), calls `status` and one `search`, and fails with the server's stderr
  on a failed start, an error result, or any step over `--client-timeout-seconds`
  (default 60); it reports `initialize`/`status`/`search`/`total` timings. The
  upgrade procedure in the README requires it, because the store audits never start
  the server and running servers keep old code (the nfkc upgrade passed every audit
  while a fresh start failed with `ModuleNotFoundError: tools`; Kata 90t5).
  A fresh process is not a cold page cache, so timings are cold only when the host's
  cache is. Evidence: `tests/test_preflight.py::FreshStart` (working server,
  import failure, timeout, clean environment and cwd, CLI exit status) and the
  `package-smoke` job, which runs it on the unzipped package.
- **Optional Docling worker** runs in its own interpreter with pinned assets
  (`tests/test_docling.py::test_changed_profile_or_assets_cannot_match_prepared_identity`).
  Each conversion reads a private 0700/0400 copy of the assets made inside its own
  operation directory (about 0.15 s for 379 MB), so swapping the shared tree during
  inference, even with a restore, cannot change what the worker reads, and concurrent
  conversions share nothing
  (`tests/test_docling.py::AdapterProcess::test_conversion_reads_a_private_copy_that_later_swaps_cannot_change`,
  `::test_concurrent_conversions_get_distinct_private_copies`). The worker still hashes the
  copy against the pins and fails on any change to it. The PDF/image conversion children
  that make the copy set their file-size limit to admit the largest pinned asset (203 MB),
  not only their own output
  (`tests/test_pdf_image.py::HardeningTests::test_conversion_children_can_write_the_largest_pinned_model_asset`).
  Its direct dependencies are exact pins in `tools/extraction/requirements-docling.in`;
  `requirements-docling.txt` is the full Python 3.14 Linux CPU lock the worker
  enforces at runtime, and every direct pin must appear in it unchanged
  (`tests/test_docling.py::WorkerRequirements`).
  The reader needs none of it.
- **Continuous integration** (`.github/workflows/`) has three kinds of workflow.
  `ci.yml` runs on pull requests and pushes to `main` with Python 3.14 as three
  independent jobs, each with a timeout, a pip cache and least-privilege
  permissions (`contents: read`; a newer push to a PR cancels its superseded run).
  `lint` runs `ruff check` and the schema snapshot `--check`. `test` runs the
  unittest suite through `.github/scripts/run_tests.py` with `-X dev`, but with
  `PYTHONMALLOC=pymalloc` (the debug allocator `-X dev` implies cost about 70% of
  the run and `ResourceWarning` detection does not need it), and fails when the
  skipped-test set differs from `.github/expected-skips.txt`, in either direction.
  `package-smoke` (`.github/scripts/package-smoke.sh`) packages the toolkit, unzips
  it, builds a fresh venv from the copy, runs `tools.initialize`,
  `librarian-preflight` and an MCP `status`/`search` handshake, which is the path an
  adopter follows (`tests/test_mcp.py::MCPConfig::test_config_loads_in_a_clean_install_where_tools_is_not_importable`
  pins the import boundary it depends on). `docling.yml` provisions the pinned
  Docling runtime and models (cached; `.github/scripts/provision-docling.sh`) and
  runs the PDF/image `RuntimeTests` and the opt-in `RealWorkerTests` with an empty
  skip set, nightly, on demand and on pull requests touching the extraction code.
  `nightly.yml` runs the full suite under the debug allocator and, as an
  informational job, on Python 3.15. Every action is pinned to a commit SHA and
  Dependabot proposes updates. `ruff.toml` pins the ruff version and `py314`
  target and enforces only pyflakes (`F`), bugbear (`B`, minus `B007`/`B905`) and
  `SIM115`; formatting and line length are not enforced. Ruff is a CI/development
  tool, not a runtime dependency. The required checks for `main` are `lint`,
  `test` and `package-smoke`; the other jobs are informational.
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
  `docling-not-provisioned`. With them, images use Docling OCR (`pdf_image`, payload
  `format` `ocr`), each in its own limited child process, retried once on a timeout
  or crash, then a blocked stage `extract-<code>`.
- **Cohort PDFs are read page by page with per-page retry.** `pdf_pages` lists the
  pages in an isolated child (`pdf_page_worker.py`, the locked `pypdfium2`); a page
  keeps its text layer when it has at least 200 alphanumeric characters at a ratio of
  0.5 or more (or draws no image in a document with 200), else just that page is
  rendered (216 DPI, reduced to fit the pixel cap) and OCRed through the unchanged
  Docling adapter. Inventory and OCR pages are units in their own limited child
  processes, at most `--extract-concurrency` at once; a unit that times out or
  crashes is retried once, then its page is `failed` with a code and a placeholder
  line, and the rest of the document is kept. The payload (`format` `pdf-pages`)
  records each page's outcome; confidence is the union of page kinds
  (`converted-format`, `machine-ocr`, `incomplete-rendition` for failed or unread
  pages). Per-document budgets (5,000 pages, 200,000 lines, 7,200 OCR seconds)
  record what they leave unread with explicit codes; a document is refused only when
  it cannot be opened, every page failed or nothing has text. PDF renditions made by
  `pdf_image` keep their identity and are not converted again. Evidence:
  `tests/test_pdf_pages.py::RoutingTests`, `::AssemblyTests`, `::HardeningTests`,
  `tests/test_pdf_image.py::CohortTests`, `::HardeningTests`, `::ImageLimitTests`;
  `::RuntimeTests` in both run the real runtime when provisioned.
- **Office and RTF documents convert to derived Markdown; disguised text is read as text (Kata
  khky).** `cohort_ingest` decides by content, not suffix. Zip packages holding `word/`, `xl/`,
  `ppt/` or `visio/` parts become `docx`/`xlsx`/`pptx`/`vsdx`, `{\rtf` becomes `rtf`, and legacy
  compound files (`.doc`/`.xls`/`.ppt`/`.vsd`) become `office-legacy` only when a sandboxed
  LibreOffice is available (otherwise `needs-reader` with `needs-converter-runtime`, or
  `needs-converter-sandbox` when the host has no confinement). Other zips, encrypted packages,
  bad containers and non-matching suffixes stay `needs-reader` with `unsupported-container`,
  `encrypted-document`, `container-invalid` or `suffix-mismatch`. Strict UTF-8 text under any
  other suffix (or none) is admitted as `text` and kept as `<name>.txt` (locator unchanged);
  `unsupported-encoding`, `not-utf8-text`, `binary-content` and `control-characters` keep the
  rest visible. Text-suffixed files the text reader already admitted keep their format and
  identity. `tools/extraction/convert.py` runs `convert_worker.py` (stdlib zip/expat only) per
  file in a separate `python -I` process with a 300 s wall clock, an 80 MiB output cap, rlimits
  (address space, no file writes) and the source sha256 re-checked. The worker refuses
  DTDs/entities, unsafe, duplicate or encrypted members, zip-bomb ratios and size, element and
  depth caps, and caps output lines, sheet rows, columns and cell text, recording truncation and
  dropped images in `fidelity`. Legacy files go through `soffice --headless --convert-to` first, in the sandbox of
  `tools/extraction/office_runtime.py` (Kata qr1d): the runtime is found by `LIBRARIAN_SOFFICE`, the
  user-space install `tools/extraction/provision_libreoffice.py` makes without root (pinned URL, size
  and SHA-256 checked before unpacking), or `soffice` on `PATH`. Each conversion has a throwaway
  directory with a read-only verified input copy, a private profile (macros and updates off), a
  scrubbed environment, rlimits, a 300 s wall clock and a process-group kill, and runs under
  `bwrap` (own namespaces, no network, only the runtime, system libraries and the work directory
  visible), else `unshare` (no network, PID namespace, host filesystem visible), else not at all
  (`needs-converter-sandbox`) unless the operator sets `LIBRARIAN_CONVERTER_SANDBOX=none`. The
  rendition's `conversion.runtime` records the LibreOffice version, sandbox mode and install record.
  A hang, crash or refusal is a per-file blocked stage (`conversion-timeout`,
  `legacy-conversion-crashed`, `legacy-conversion-failed`) that the ledger keeps, so a rerun neither
  halts the cohort nor launches LibreOffice again. The stdlib worker opens no sockets. The rendition is a derived line rendition of the registered original
  with `payload.format: "converted"` and `payload.conversion: {source_format, via, converter,
  version, code_sha256, lock_sha256, runtime, fidelity}`; its engine identity hashes both
  converter files (and the `soffice` version for legacy input), and cohort request names carry a
  `-c<12 hex>` suffix of it (as MHTML carries `-mN`), so a converter change extracts again; a
  `--trust-stat` row with an older suffix is not trusted. Each conversion failure is a blocked stage with its code. Evidence:
  `tests/test_convert.py::CohortConversionTests::test_classify_routes_documents_to_converters_and_disguised_text_to_the_text_reader`,
  `::CohortConversionTests::test_converted_documents_are_derived_renditions_reviewed_selected_and_retrievable`,
  `::CohortConversionTests::test_conversion_failure_stays_visible_with_its_code`,
  `::CohortConversionTests::test_legacy_binary_converts_through_soffice_when_present`,
  `::CohortConversionTests::test_a_hung_legacy_conversion_is_a_durable_disposition_and_not_repeated`
  (and its `crashing` and `failing` siblings),
  `::CohortConversionTests::test_without_a_sandbox_legacy_files_wait_with_their_own_code`,
  `tests/test_office_runtime.py::BwrapTests`, `::UnshareTests` (read-only input, private profile, no
  network, scrubbed environment, timeout with whole-tree kill, crash and size-limit codes),
  `::LocateTests`, `::ProvisionTests` (checksum, size, escaping members, plain HTTP refused),
  `::RealRuntimeTests` (the pinned LibreOffice when provisioned, `libreoffice.yml`),
  `::HostileInputTests`, `::IsolatedWorkerTests::test_wall_clock_timeout_and_output_cap_kill_the_child`,
  `::IsolatedWorkerTests::test_converter_identity_changes_with_converter_code`,
  `::CohortConversionTests::test_converter_change_extracts_again_even_under_trust_stat`.
- **Ingest follows correction-chain heads.** `ingest` and `qualify` report an
  extraction or registration through the head of its validated `correction_of`
  chain, never by request name: a repaired entry is `extracted` with the head's
  evidence, a blocked or ambiguous head is `failed` with its own code, and an
  entry with no correction reports and writes as before. A head must repeat the
  entry's operation, receipt and format (`correction_mismatch`). A registration
  blocked as `changed-extraction-identity` (the extractor code changed after the
  stage pinned it, and replay keeps the pin) is repaired by
  `cohort recover-identity`, which stages validated corrections with a fresh
  pin; the identity, source-hash and catalog-pin checks are not relaxed. An
  `extract-prepare` blocked with a code a toolkit fix has since removed is
  restaged by `cohort recover-blocked-extractions --code CODE` as an unrun
  correction that the next ingest converts and registers.
  Evidence: `tests/test_intake_cohort.py::CorrectionChainTests`,
  `::RecoverIdentityTests`, `::QualifyRepairedTests`,
  `::IngestTests::test_extractions_blocked_by_a_fixed_toolkit_are_restaged_for_the_next_ingest`.
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
  (`test_v2_criteria_prompt_records_embedded_instructions_and_sparse_records`,
  `test_control_task_must_not_be_accepted` covers a synthetic injection control,
  `test_redirects_are_refused`, `test_usai_transport_refuses_unconfigured_models_and_providers`).
- **Low-confidence information is published and marked, not withheld** (owner direction, Kata
  chwz). Every indexed document has a `confidence-v1` label `{"level": "normal"|"low",
  "reasons": [{"code", "details"}]}`, stored in the index's `document_confidence` table and
  bound into the rendition identity, so a label change makes the index stale. Reason codes are
  stable; `tools/context_index/confidence.py` holds each code's explanation and how to correct it
  ("confirm or correct via a newer document, meeting transcript or answered question"):
  `noted-issues` (an accepted review carrying `Reviewer-noted ...` statements, so existing
  my2k qualifications become low confidence at the next rebuild without re-review),
  `reviewer-deferred` (a `Reviewer-deferred:` statement, see below), `embedded-instructions`
  and `sparse-record` (`Reviewer-flagged ...` statements, below), `converted-format` and
  `machine-ocr` (from `RENDITION_REASONS`, keyed by `rendition_kind`: `docling-<format>` or the
  extraction payload format; `docling-pdf`, `docling-image` and cohort `ocr` are `machine-ocr`, a cohort PDF text layer
  (`pdf-text`) and `converted` (office/RTF conversion, below) are `converted-format`, a cohort
  `pdf-pages` PDF is the union of its pages' kinds plus `incomplete-rendition` for pages not read
  (`confidence.page_kinds`), text, MHTML,
  Gemini transcript and ODT renditions add no reason), `agent-generated` (an upload whose
  version-2 descriptor declares `origin.agent_generated: {generator, generated_on,
  source_commit}`; the hash-pinned `submission.json` sidecar is read at build and a missing,
  changed or malformed one fails the build) and `open-correction` (added at read time by
  `with_reason` for an open claim correction, below; never stored in the index) and
  `integrity-changed` (read-time, document-level, from the recorded integrity audit; see the
  [integrity audit](#index-freshness-from-ingest-pins-and-the-integrity-audit-kata-xv0n-earlier-t5df-17b0)). Markdown documents are `normal`. `search` hits carry `{level, reasons:
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
  safety backup, `synchronous=FULL` on a rollback journal; covered by `tools.backups`, the
  scheduled `tools.store_audit`, and the durability and upgrade-path tests like the other
  authority stores, Kata 6jwh). It is a separate store, not a review-event kind: a new kind would change the
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
- **Inbox exclusion rules and `deselect`** (Kata bvvc). `cohort classify|ingest|qualify` read an
  optional private `config/intake-exclusions.local.json` (or `--exclusions FILE`;
  `tools/intake/exclusions.py`, version 1, `rules[{id, reason?, globs?, extensions?}]`, first match
  wins, globs over the whole inbox-relative path including `ARCHIVE!/MEMBER`). `classify` judges a
  file as before and `exclusions.mark` then reports it `excluded` with `rule` and `underlying`
  (never during `--agent-origin`, whose own `exclude` stays; naming both is
  `exclusions_not_for_agent_origin`). A file that would have been `admit` still gets a receipt
  (`cohort_ingest._receipt_only_batch`, the receipt half of `_process_batch` that ingest already
  splits out as `_receive_batch`; outcome `receipt-only`, ledger `docs` state `received`); it gets no
  prepare/register stage, so it is never extracted, reviewed or selected, `--limit` does not defer
  its receipt, and `--trust-stat` cannot send it down the extraction fast path. The standing policy
  is not read or rewritten by any of this. `qualify` keeps the existing selection of a file a rule
  matches (`_reconcile_qualified(..., keep)`, by receipt or by the inbox path the receipt's
  sidecar names, so a changed or no-longer-admittable file keeps it too): an exclusion never
  removes anything by itself.
  `tools/intake/deselect.py` removes selections: dry run by default; selectors are receipt IDs
  (or a dry-run report), exclusion rules matched against the `docs` row's inbox path, and
  `--dedupe` over groups of selections whose originals share a SHA-256 (from the `docs` row, else the
  catalog's original, so the optional table is not needed; groups are always
  reported; the lowest receipt left after the other selectors is kept, so no group is emptied).
  `--apply` holds the ledger writer lock and recovers an interrupted manifest append, then writes
  in this order: a fsynced `begin` record in `<intake home>/deselections.jsonl`, one superseding
  `dispositioned` review event per removed entry's live review (`rev-deselect-<hash of the
  superseded id>`, batched, idempotent by request ID), the atomic validated manifest rewrite
  (`standing_publication._replace_validated`, not the constant-time append, which only adds) and a
  `done` record. A `begin` without `done` is completed by the next `--apply` whatever selectors it
  gets; a rerun with nothing left to remove writes nothing. Superseding the review is what keeps a
  removed entry removed: `cohort_review._superseded` reports it `review-superseded` and
  `qualify` does not restore it, and publication refuses a manifest entry that cites a
  superseded review. Receipts, originals and renditions are not touched; `publish` is run
  afterwards. Evidence: `tests/test_intake_exclusions.py`
  (`test_excluded_files_are_never_sent_to_the_model_nor_selected_but_are_reported`,
  `test_ingest_gives_excluded_files_receipts_and_never_extracts_them`,
  `test_dry_run_is_the_default_writes_nothing_and_reports_by_rule`,
  `test_apply_removes_only_the_matching_selections_and_keeps_every_receipt`,
  `test_apply_is_idempotent_and_a_rerun_changes_nothing`,
  `test_a_crash_after_the_log_but_before_the_events_is_finished_by_a_rerun`,
  `test_a_crash_after_the_events_but_before_the_manifest_rewrite_is_finished_by_a_rerun`,
  `test_a_deselected_entry_is_not_selected_again_by_a_rerun_or_a_policy_bump`,
  `test_publish_after_deselect_serves_only_what_is_left`,
  `test_byte_identical_selections_across_receipts_are_reported_as_groups_without_removing`,
  `test_dedupe_never_removes_the_last_copy_when_a_rule_removes_the_others`,
  `test_a_selection_survives_its_file_changing_or_becoming_unadmittable_while_excluded`,
  `test_dedupe_finds_groups_from_the_catalog_without_the_docs_table`).
- **Agent-written context is a cohort with a declared origin** (Kata 2ket). `cohort
  classify|ingest|qualify --agent-origin FILE` (`tools/intake/agent_origin.py`) reads a private
  version-1 origin file (`generator`, optional `source_repository`, `provenance` `git|none`,
  `include`, `exclude`); only included paths are walked, excluded files are counted `excluded`
  unread, a missing include refuses the run and a symlinked include is `unsafe-path`. Every
  admitted file is submitted with `origin.agent_generated: {generator, generated_on,
  source_commit, source_repository?}` (`source_repository` is an optional field of the
  version-2 descriptor, also `submit-file --agent-source-repository`). `git` provenance requires
  the inbox to be a work-tree top level and runs git read-only (`--no-optional-locks`,
  `core.fsmonitor=false`, no signatures or pager): a tracked, unmodified file gets the last
  commit touching it and that commit's author date, a modified or untracked one neither
  (`uncommitted`), as does a file no longer in HEAD's tree; an expanded archive member gets its
  archive file's, and exclusions apply to members before they are read. `--trust-stat` is
  ignored with an origin so that every declaration is checked.
  Review, selection and publication are the unchanged standing path; what
  qualifies is low confidence `agent-generated` with detail `Generated by G on DATE from
  REPO@COMMIT:PATH`, and search hits with that reason carry `origin: "agent-generated (...)"`.
  A different declaration for unchanged bytes is `receipt/submission_conflict`. The label
  detail and search field change the index engine fingerprint, so an existing index is rebuilt
  once; no extraction identity changes. Evidence: `tests/test_intake_agent_origin.py`
  (`test_origin_selects_included_files_and_reports_excluded_ones_unread`,
  `test_a_symlinked_include_is_refused_not_followed`,
  `test_git_provenance_declares_last_commit_and_date_and_leaves_the_tree_untouched`,
  `test_expanded_archive_members_are_declared_with_their_archive_files_commit`,
  `test_a_deleted_then_recreated_file_is_uncommitted`,
  `test_git_provenance_needs_the_repository_root`,
  `test_qualified_agent_context_is_low_confidence_and_labelled_in_search_and_get`,
  `test_classify_cli_reports_the_declared_origin_for_a_dry_run`),
  `tests/test_intake_upload.py::test_agent_declaration_may_name_its_source_repository`.
- **Markdown renditions are searched by heading span as well as by record** (Kata q886). The
  index stores one section per extraction record (a line of a text, converted, MHTML or page
  rendition), and literal search needs every query word in one section, so a passage whose words
  sit on neighbouring lines never matched. For a `converted` rendition (converter output is
  Markdown) and a `text` rendition of a `.md`/`.markdown` original, `renditions.heading_sections`
  adds a `rendition-heading` section per heading span: from a heading outside fenced code to the
  next heading of any level (so spans never overlap), title the heading text, anchor
  `heading-<first record>`, text the exact physical JSON lines of its records, and
  `source_location` the first/last JSON pointer and original or converted line. A span over
  `MAX_HEADING_SECTION_CHARS` (24,000 JSON characters, inside the MCP 30,000 source budget) is
  cut into whole-record chunks; a one-record span or chunk is skipped. Line records are unchanged,
  so citations, review inspection (`renditions.records`) and claim placement are unchanged.
  `search` returns a passage once: a hit overlapping a better-ranked hit of the same document is
  dropped when either is a heading span (`_without_overlaps`, fetching more rows until `limit`
  are kept), and `low_confidence_excluded` counts a heading span containing a matching record once
  (`_passages`). The change alters the engine fingerprint, so an existing index is rebuilt once.
  Evidence: `tests/test_workspace.py::WorkspaceTests::test_markdown_rendition_heading_section_matches_terms_on_different_lines`,
  `::test_heading_hit_is_not_repeated_by_its_own_line_hits`,
  `::test_overlap_filter_keeps_nested_markdown_document_hits`,
  `::test_low_confidence_exclusion_counts_a_heading_passage_once`,
  `::test_plain_text_rendition_gets_no_heading_sections`,
  `::test_long_heading_span_is_split_into_whole_record_chunks`.
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
  candidate models on a sample under a chosen `--criteria` (default: the `--policy`
  file's, else the newest), optionally with body-free per-rendition decisions,
  bounded `--concurrency` (adaptive limiter, deterministic order) and
  `--max-output-tokens`
  (`tests/test_model_review.py::EvaluationOptionTests`, `EvaluateCommandTests`,
  `RetryTests::test_max_tokens_is_sent_only_when_configured`), and `estimate()`
  extrapolates token use; nothing in
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
- **Parse worker processes (Kata q868).** `ingest` reads, hashes and parses text-family files in
  `--parse-workers` spawned processes (`tools/intake/parse_pool.py`) while the one writer commits; the
  single-process path is `--parse-workers 1`. A worker is given bytes, a bare file name and the policy
  settings (never a path to read, and no ledger, catalog or network handle) and returns a pure result:
  `classify` judges each file (format, strict decoding, MIME and transcript parsing) from bytes this process
  read and hashed, and each chunk's parse is remembered under the (format, source SHA-256, extractor SHA-256)
  key the prepare stage and its guards already re-check against the source they re-read, so no stale or
  foreign parse is used and no changed-input check moved. Results are delivered in submission order, so the
  report, ledger and catalog are identical at any worker count. The queue is bounded by job count and bytes.
  A worker that dies fails only its file (`unreadable/parse-worker-died` in `classify`, `failed
  prepare/parse-worker-died` in a chunk's parse) and the run continues. Evidence:
  `tests/test_intake_parse_workers.py` (`IngestEquivalence::test_one_and_many_workers_end_in_the_same_report_catalog_and_ledger`,
  `::test_a_document_whose_worker_dies_fails_alone_and_a_rerun_completes_it`,
  `::test_a_changed_source_is_never_served_a_worker_parse`, `ClassifyEquivalence`, `PipelineTests`).
- **One hostile file never stops a cohort run (Kata e275).** Source bytes are untrusted, so every
  per-file read is contained and ends in a recorded, counted disposition. `extract.parse_payload` and
  `extract.odt` report any failure that is not already an `ExtractionError` (a bad `text:c`, a corrupt
  member, an unsupported compression method such as 99, `zlib.error`, `RecursionError`) as
  `ExtractionError`; the ODT walkers (`walk`, `paragraph`) are iterative, `text:c` must be 1 to 5
  decimal digits and at most 10,000, and XML nesting is capped at `MAX_XML_DEPTH` (256) before any walk.
  The MIME reader parses with `message_from_bytes` inside a guard, so a deeply nested multipart is
  `mime-malformed` (`needs-reader`) in `classify`, as nesting past `MAX_DEPTH` (16) already was.
  `cohort_ingest.judge` never raises: an unanticipated fault in a worker or inline is `unreadable` with
  code `parse-failed`, and a job error delivered by the pool does the same instead of re-raising. In the
  ledger, a refused extraction is the existing `blocked` `native-input` stage (reported as
  `failed prepare/native-input`), and a fault no adapter anticipated is `native-unexpected`, a `permanent`
  failure that dead-letters the stage on its first attempt; the adapters' single and batch paths and the
  PDF/image `prepare_many` catch it per file. A blocked or dead-letter stage is a decided disposition: a
  rerun reports it again without running it (attempts stay at one) and the rest of the cohort proceeds;
  `replay` or `recover-blocked-extractions` retries it after a toolkit fix, and a transient cause
  (timeout, busy catalog) stays `retryable` within the request's attempt budget. Evidence:
  `tests/test_hostile_input.py` (`ExtractorContainmentTests`, `MimeContainmentTests`,
  `CohortContinuesTests::test_ingest_records_each_bad_odt_and_a_rerun_neither_crashes_nor_repeats`,
  `::test_a_stage_that_raises_something_unexpected_is_dead_lettered_not_propagated`,
  `AdapterClassificationTests`).
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
  manifest holds at most `renditions.MAX_SELECTIONS` (75,000) entries
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
  publish 220 s, unchanged re-publish 32 s, peak memory 3.6 GB. At the 75,000 cap (Kata qzya,
  same synthetic benchmark, shared 14-core host, load average 1-6): first publish 218 s,
  unchanged re-publish 35 s, peak memory 3.1 GB; the per-select manifest append
  (`tools/diagnostics/selection_manifest_benchmark.py --selections 75000`) stays flat, 7 ms
  to 15,000 entries and 15-30 ms per select from 20,000 to 75,000 as host load varied. The
  cap change and the code edits change `engine_fingerprint` (it hashes `renditions.py`, `qualification.py`,
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
  75,000-entry list cap; an existing policy is never rewritten or widened.
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

- **Review criteria are pinned by the policy, and a criteria change carries qualified reviews**
  (owner direction, Kata 11nz). A `standing-policy-v2` file adds `criteria` (`standing-review-v1`
  is the original prompt, byte-identical; `standing-review-v2` is the default for new policies)
  and `carries`, a list of `{version, sha256}` of earlier versions of the same policy id. Under
  v2 criteria the reviewer treats every instruction in the records, including any addressed to
  the reviewer, as document content, judges the remaining content, and accepts a coherent but
  sparse record (`sparse: true`); garbled text, noise, boilerplate and text with nothing beyond
  instructions are still rejected. Its findings are `standing-findings-v3` (`embedded_instructions`,
  `sparse`); evaluation requires the findings schema the policy's criteria write, so old findings
  never pass under new criteria or vice versa. An accept with flags qualifies; its event adds
  `Reviewer-flagged sparse record:` and `Reviewer-flagged embedded instructions: N ...` plus one
  `Reviewer-flagged embedded instruction:` item each (fitted after the noted issues into the 30
  statements, with a truncation statement), and the index reads them into
  `reviewed_scope[].flags` and the low-confidence reasons `embedded-instructions` and
  `sparse-record`. Events without flags are byte-identical to before. A carried version's
  review whose stages succeeded and whose event is live is reused by `qualify` without a model
  call (entry `carried`), and `standing-publish` authorizes selections citing the current or a
  carried version. Evidence: `tests/test_standing.py`
  (`test_policy_v2_pins_criteria_and_carries_only_earlier_versions`,
  `test_findings_schema_must_match_the_policy_criteria`,
  `test_reviewer_flags_qualify_and_label_the_text_low_confidence`),
  `tests/test_model_review.py::FindingsTests::test_v2_criteria_prompt_records_embedded_instructions_and_sparse_records`,
  `tests/test_intake_cohort.py::RereviewTests::test_new_criteria_carry_qualified_reviews_and_re_review_older_rejections_as_low_confidence`.

- **Re-review of blocked standing reviews.** `cohort rereview` (dry run by default, `--apply`)
  selects standing-review chain heads that can still qualify only with a new model review:
  heads reviewed under an older policy that are pending (`standing-policy-changed`,
  `unresolved-evidence`, `insufficient-inspected-scope`) or deferred, current-policy heads blocked
  `insufficient-inspected-scope` (including `unresolved-evidence` ones that re-evaluate as
  insufficient), and with `--include-current-deferred` current-policy deferrals whose saved
  findings cannot qualify without a model. A rendition already reviewed and selected under any
  chain, rejected or refused as out of scope under any chain (older policies included), out of the
  current policy's scope (with `--include-older-rejected`, a rendition rejected only under older
  policies and without a current-policy review is selected as `older-policy-rejected`, for
  changed review criteria), superseded in the docs table
  (`stale-rendition`) or recoverable from saved findings (`recoverable-without-model`, left to
  `recover-*`) is skipped with that reason; a rejected or out-of-scope rendition is never
  selected. The dry run counts selected/skipped heads by reason and estimates model calls and
  prompt size (about four characters per token, plus `by_count` from a `--cost-estimate` file's
  per-document averages); `--limit N` bounds model calls for a staged rollout (`over_limit`).
  `--apply` reviews each selected head with qualify's plumbing (models config, the policy's context,
  adaptive concurrency, throttle/backoff, `--inspected-form`, usage and throughput reporting) and
  stages the review as an append-only correction of the old head (an older-policy head under the
  current-policy chain name, so a later `qualify` finds it), then selects. `--deferred` defaults
  to `qualify-low-confidence` here, so a head still deferred qualifies as `reviewer-deferred` low
  confidence; a new rejection stays out. A staged but unfinished re-review, or a current-policy review whose
  selection is unfinished (interrupted, or the manifest was full), is resumed without a new model
  call, so an interrupted run is rerunnable. Nothing is published until `cohort publish`.
  Insufficient-scope reviews on a live cohort were accepts over long renditions (median about
  2,200 records) whose inspected-record list was abbreviated; `--inspected-form ranges` lets the
  reviewer state full coverage compactly while the completeness gate is unchanged. Evidence:
  `tests/test_intake_cohort.py::RereviewTests`.

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
  `python3 -m tools.backups status` and `prune` cover the catalog, the review events and (when it
  exists) the claim corrections (`--store catalog|review-events|claim-corrections|all`); `prune` is a
  dry run unless `--apply`. The claim-correction store follows the same rules as the review events:
  `tools.review_events.corrections` has `backup`, `verify-backup` and `restore`, `backup_inventory` and
  `prune`, and `python -m tools.store_audit` replays it and checks its newest backup.
- **What counts as verified for retention (Kata 6jwh).** Only a snapshot whose whole history was
  replayed counts. The review-event and claim-correction daily snapshots are taken from a copy that is
  replayed on the spot, so the new one counts without another read. The catalog's daily snapshot is the
  serialized image of a transaction whose head was validated by a receipt (Kata 91h5), which is a head
  check and not a replay, so a corruption the identity check cannot see (an edit that restores the file
  identity, or silent media corruption) would be copied into every new day's snapshot. The catalog
  therefore does **not** count the new snapshot as verified. Retention replays catalog snapshots
  (newest first, `verify_backup` with the default `replay=True`) only when one is old enough to delete,
  and deletes nothing unless a replayed snapshot remains in the window, so a stretch of days with a
  silently corrupt head never ages out the last clean snapshot; the retention report says `refused`
  until a clean snapshot is inside the window, and `store_audit` reports the newest snapshot. The cost
  is one full catalog replay under the writer lock on the first write after the oldest snapshot ages
  out, which is once per UTC day in steady state (the write itself and taking the snapshot still replay nothing). The
  alternative, replaying every new snapshot, would restore the pre-91h5 cost on every day's first
  write. The fresh snapshot is still checked against its receipt (fields, size, SHA-256, state) before
  anything is deleted.
- **Loss window.** Between snapshots recovery relies on `synchronous=FULL` (`tests/test_store_durability.py`) and
  the hash-chained history: the worst case restores the day's first snapshot and
  replays that day's requests by their original request ids. This is a deliberate
  trade against per-write snapshots, which in one measured cohort produced 5,815
  backups (35 GB) for a 12.7 MB catalog.
- On the first write of a UTC day the snapshot is the serialized image of the
  transaction that was just fully inspected, written, fsynced, re-read and refused
  unless its hash equals the image (`tools/catalog/backups.py`, "Backup bytes mismatch").
  Fresh verification checks fields, size, SHA-256 and receipt state instead of a
  third replay, as the gate before any deletion; it does not make the snapshot count as
  verified for retention (above); every snapshot that is counted has been fully replayed.
- Evidence: `tests/test_backups.py` (daily gate reads no snapshot body, truncated or
  malformed/foreign-receipt snapshots do not count, a same-size corrupt snapshot passes the gate
  but fails `verify_backup`, window pruning, never deleting the only verified
  snapshot, dry-run default, protected snapshots, restore, review events follow
  the same rule; `test_a_head_only_checked_daily_snapshot_never_displaces_a_fully_verified_one` is
  the corruption-then-retention scenario for the catalog),
  `tests/test_corrections_backups.py` (the claim-correction store: daily snapshot, retention,
  corruption-then-retention, prune, `tools.backups` CLI, `synchronous=FULL` at init),
  `tests/test_catalog_daily_snapshot.py`.
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
   of the exact database bytes. A receipt is trusted for as long as it covers exactly
   the file (Kata 91h5): there is no daily expiry. `validated_on` only records the UTC
   date of the last full replay for the audit report. The byte-hash path is tested on every
   build by hiding the table (`without_dbpage`) and natively by the `test-no-dbpage` CI leg.
   What replaces the daily check is the scheduled audit ("Store validation cadence" below).
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
6. **First write of a UTC day** replays nothing when a receipt covers the file (Kata 91h5):
   the daily snapshot is the serialized image of the validated transaction, written, fsynced
   and re-hashed against that image, so the work under the writer lock is the snapshot copy
   itself. Without a receipt (a new catalog, a changed file or validator code) the writer
   replays once (`inspect` with `detail=False`, state only), as any writer does, and the
   snapshot is that replay's image. `inspect` decodes and encodes each payload once, falls
   back to the original step-by-step checks on any irregularity, and streams revisions and
   versions. `status --audit`, `verify_backup`, restore, `migrate-indexes` and
   `python -m tools.store_audit` always replay in full.

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

### Store validation cadence (Kata 91h5)

Reads and writes do not replay a store's history once per UTC day or once per process. The owner
is the only provider of documents and nobody edits the instance's files by hand (later automated
ingestion is the only other writer, and it is controlled), so a recorded hash is the reference
and an unexpected change is *detected and flagged*, not defended against in real time. This
supersedes the daily full check kept by Kata qwtp (D2).

- **Review-event receipt.** `<review events home>/.local/read-validation.json` (0700 directory,
  0600 file, canonical JSON with a checksum) holds the validated state and its binding: the
  file identity (device, inode, size, mtime_ns, ctime_ns, SQLite change counter), the contract
  hash, the SHA-256 of `event_history.py`, `review_events.py` and the item schema, and the
  Python, SQLite, `jsonschema` and `referencing` versions. It is rewritten after every full
  replay and every delta, best effort. A new process that finds a receipt for this code and
  contract trusts it exactly as the in-process memo is trusted: when the file's identity equals
  the receipt's, and the chain heads and correction count re-read from the file equal its state,
  it replays nothing; when only SQLite's own commits moved the identity (same file, larger, a
  new change counter) it verifies just the rows past the remembered head (`_extend`). The
  events themselves are not loaded: `StoredHistory` answers `event`, `superseded_by` and `at`
  from the store's unique keys on a read-only connection bounded by the snapshot's event count (an event
  appended later is never seen) and loads every row once, lazily, only for `events`,
  `successors` and `query`. A missing, oversized, malformed, group-readable, symlinked or
  wrongly checksummed receipt, one for other code, contract or runtime, or one whose store
  identity or contract differs, is a miss that costs one full replay and never a refusal. A
  full replay that refuses when asked for (an audit or publication, `full=True`) deletes the receipt,
  so no later process trusts that file; a refusing ordinary read leaves it, its identity already misses.
- **No daily expiry** in the catalog receipt, the review-event memo or the receipt above.
- **Where each tamper class is now detected**

  | Change to a store file | Detected |
  | --- | --- |
  | SQLite edit, raw edit or truncation that moves size, mtime, ctime or the change counter; a file swapped or restored; changed validator code, contract or runtime | at the next read or write (identity or binding differs), with the unchanged refusal |
  | Edit that restores size, mtime, ctime and change counter, or silent media corruption | by `status --audit` of the store, `python -m tools.store_audit`, backup verification or restore; not by reads or writes (before Kata 91h5: also by the first read or write of the next UTC day) |
  | Old review-event rows edited while the head, counts and change counter are made to look like a commit | the same audits (and publication, which reads with `full=True`) |
  | An actor who rewrites the file and its unkeyed receipt so they agree | the same audits; not a defense against that actor |

  Evidence: `tests/test_catalog_identity_binding.py` (`test_a_new_utc_day_replays_nothing`,
  `test_residual_identity_restoring_edit_is_served_until_an_audit_whatever_the_day`),
  `tests/test_catalog_delta_commit.py`
  (`test_first_write_of_each_day_no_longer_replays_so_a_forged_receipt_is_found_by_the_audit`),
  `tests/test_review_event_memo.py` (`test_residual_does_not_expire_with_the_utc_day_but_a_new_process_and_an_audit_find_it`),
  `tests/test_review_event_receipt.py` (a new process replays nothing and verifies only the delta,
  stored lookups equal replayed ones, every receipt defect is a miss, an audit re-baselines and a
  failing one removes the receipt), `tests/test_store_audit.py`.
- **First write of a day.** The writer's replay under the lock is gone when a receipt covers the
  file; what remains under the lock is the daily snapshot itself (serialize, write, fsync, hash,
  retention). The snapshot is the image of a validated transaction, so it is not replayed; whether
  its bytes are a valid store is checked by `verify-backup`, restore and the audit of the newest
  backup, as for every snapshot older than the one just written.
- **Scheduled audit.** `python -m tools.store_audit --root ROOT` replays the catalog
  (`status --audit`), the review events (`status --audit`, which also re-baselines the receipt),
  the claim corrections when that store exists (reported as `claim-corrections`; it replays on every
  read, so it has no receipt) and the intake ledger (`intake audit`), verifies the newest backup of
  the catalog, the review events and the claim corrections against its receipt (`--no-backups` skips that), and prints one body-free JSON object:
  `ok`, and per store `ok`, the state it verified or the refusal, and the seconds taken. A failing
  store is reported and the others still run; exit status 0 when everything verified, 1 when any
  store was flagged, 2 for a usage error. It takes no writer lock and changes nothing except the
  baseline receipts described above. It does not check the files under the instance's data roots
  (extractions, originals, sidecars): that is the integrity audit of Kata xv0n (`tools.context_index.integrity`). Run it from cron or
  a systemd timer, for example `17 3 * * * cd /path/to/instance && .venv/bin/python -m
  tools.store_audit --root . >> .local/store-audit.log 2>&1`, outside an intake run when the host is
  small (it holds a read transaction while it replays, as `status --audit` does). The daily verified
  backup stays.
- **Measured** (one production-sized instance, read only through a throwaway overlay, nothing
  written to it; 51.5k review events, 132k-revision 865 MB catalog; wall time, peak RSS of the
  process):

  | | before | after |
  | --- | --- | --- |
  | review-event `read_snapshot` in a new process | 5.8 s, 665 MB | 0.01 s (0.05 s with start-up), 28 MB |
  | catalog `status` in a new process, first read of a UTC day | 18.4 s, 595 MB | 0.02 s, 27 MB |
  | catalog writer ready (replay) before the first write of a UTC day, under the writer lock | 21.8 s, 2.6 GB | 0.02 s, 27 MB |
  | whole first write of a day under the lock before the commit (writer ready plus daily snapshot) | 23.8 s, 4.2 GB | 2.0 s, 1.7 GB |
  | cold index `status`, freshness memo removed (with Kata xv0n's pin-based freshness, which reads `read_snapshot(root)`) | 10.8 s, 727 MB | 3.6 s, 223 MB |

  The first read after installing this code pays the old cost once, because receipts are bound to
  the validator code. The last row is the whole cold index read on main (xv0n) against this change
  on top of it: what remains is the review-event and catalog work of the freshness proof, not a
  replay of either store.
- **Not done here.** The ledger `progress` summary keeps its UTC-date key (it is a view, not a
  store gate). The index freshness memo has no daily expiry since Kata xv0n.
  Document-update ingestion does not exist; a re-pinned hash is the app writing a new catalog
  revision, which every receipt already follows as a delta.

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
  past `audit`, like any other edit.
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

- The catalog receipt is **unkeyed**. For the **catalog**, an actor who rewrites the
  database *and* the receipt so that they
  agree, or who rewrites the file while restoring size, mtime, ctime and the change
  counter, can get a write or read accepted without a replay; `status --audit`, the scheduled
  audit and backup verification replay all history and refuse (no longer the next UTC day's
  read or write: Kata 91h5). For the **intake ledger** nothing is trusted on the strength of a stored
  marker any more (Kata qwtp): a writer checks structure only, so an edit that keeps SQLite
  sound (a changed digest or status) is served to writers until `intake.cli audit` or a
  `progress` read verifies it, with no daily bound. Catalog audit and
  backup verification do not validate the ledger. Neither is a defense against that
  actor. Pinned by `tests/test_intake_ledger_scaling.py::CadenceTests`
  (`test_the_documented_residual_a_digest_edit_is_served_to_writers_until_audit`).
- What the byte-hash binding saw and the identity binding does not: silent media
  corruption with no write through the OS, and an identity-restoring edit. Both
  are caught by the next audit (`status --audit`, `tools.store_audit`); there is no daily full
  check any more (Kata 91h5), so the window is the audit interval the owner schedules. Everything else refuses at the next read or write: SQLite edits, raw
  same-size `pwrite`s (even with mtime restored, because `utime` moves ctime), a
  replaced file, a restored older file.
- Evidence: `tests/test_catalog_identity_binding.py` (`test_raw_same_size_edit_is_refused`,
  `test_raw_edit_with_restored_mtime_is_refused_because_utime_moves_ctime`,
  `test_residual_identity_restoring_edit_is_served_until_an_audit_whatever_the_day`,
  `test_audit_always_replays`), `tests/test_catalog_delta_commit.py`
  (`test_forged_receipt_for_other_bytes_does_not_vouch_for_tampered_bytes`,
  `test_first_write_of_each_day_no_longer_replays_so_a_forged_receipt_is_found_by_the_audit`,
  `test_documented_residual_forged_receipt_is_trusted_until_an_audit`).
  The ledger has no receipt to forge: writers check structure only (see "Intake ledger"), and that
  residual is pinned by `CadenceTests` as above (Kata yy1k, qwtp).
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
  Without `sqlite_dbpage` (Ubuntu 24.04's SQLite) every read therefore replays in full and the
  identity-bound residuals above do not exist; `WithoutDbpage` in `tests/test_review_event_memo.py` and
  the `test-no-dbpage` CI leg (Kata 8rzp) hold that, and tests of the identity path skip there.
- **Read locks and busy waits (Kata xeb8).** Review-event connections used SQLite's busy
  timeout 0, and a full replay (a cold `progress` render, or a publication's `read_snapshot(full=True)`) held its read transaction, and so its
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
- **Catalog read locks and the identity probe (Kata 8as8).** Two multi-hour `cohort qualify`
  runs died with "Invalid store identity at .../catalog.sqlite3: database is locked". In
  rollback-journal mode a catalog writer's commit (the run's own `standing-select`) holds
  PENDING while readers finish, and every new reader, the 5 s instance-identity probe
  (`policy._probe_store_id`) among them, fails meanwhile. The long readers were the index
  freshness check (`renditions.inputs` → `read_records_by_receipt`, about 6 s at 458,000
  current records, on every `progress-serve` render and twice per MCP call; since Kata t5df
  only on a miss of the [freshness memo](#index-freshness-from-ingest-pins-and-the-integrity-audit-kata-xv0n-earlier-t5df-17b0)) and the
  `progress` catalog panel's full inspection (about 21 s) when no receipt vouched.
  `read_records_by_receipt` now copies the validated snapshot's current records into a
  temporary table and commits before its joins and lookups, which read only that copy. The
  `progress` panel never inspects or hashes a catalog larger than `progress.INSPECT_LIMIT`
  (32 MiB; `_binding(hash_bytes=False)` gives no byte binding): without an identity-bound
  receipt (or this process's memo) it reports `unvalidated`, and its render
  runs the index check, which leaves a receipt, first. The cohort side waits a busy store out: `tools/intake/store_busy.py`
  retries a call that failed only on a SQLite busy/locked error (in its cause chain) with
  backoff for up to 300 s, then raises the retryable `store_busy`, never an identity error.
  The model-review findings probe and each entry's review-and-select settle use it; a settle
  retry restores the entry and token usage first and passes a review that already succeeded
  (prefetched or made by the failed attempt) as its prefetched review, so it is charged once and
  never paid for again. WAL was not adopted: the stores require `journal_mode=delete`, refuse `-wal`
  sidecars, and bind receipts to the header change counter and file bytes that WAL does not
  maintain. Tests: `tests/test_store_busy.py` (a writer can take EXCLUSIVE while batch reads
  page their results; bounded retry; settle retry), `tests/test_progress_scale.py`
  (`CatalogTests`: a large catalog is never inspected or hashed).
- **Invalidation.** Any commit (by this or another process) is verified as a delta; a restore,
  swapped file, same-size edit (even with a restored mtime, because `utime` moves ctime) or
  changed contract, validator code or runtime misses and revalidates everything with the
  unchanged refusals. A new UTC day no longer does (Kata 91h5). `event_store.py audit` (and `status(audit=True)`) always replays
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
  `standing_publication.authorize_publication` and by index build, so publication still re-validates
  everything it serves), a different contract, a swapped file,
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
  and the correction count unchanged is served, by this process's memo and by the persisted
  receipt in any new process, until an audit (`status --audit`, `tools.store_audit`), which
  replays everything and refuses; a refusing audit also deletes the receipt, so no later reader
  trusts the file. It is not a defense against an actor who can write the file.
  Evidence: `tests/test_review_event_memo.py` (reuse, same-size edit with and without
  restored mtime, swapped and swapped-tampered file, a connection opened before a swap,
  a concurrent writer process's commit, an overlapping commit, the stated residual and its
  expiry, contract change, audit).

### Index freshness from ingest pins, and the integrity audit (Kata xv0n; earlier t5df, 17b0)

Every index read (`status`, `search`, `get`, `get-many`, claim corrections and the `progress` index
panel) checks freshness before and after it reads. The check compares the index's stored
`rendition_identity` with the current one. Hashes recorded at ingest are the reference: the owner is
the only provider of documents, nobody edits files under the instance's data and source roots by
hand, and the only other writer will be controlled automated ingestion. So the system detects and
flags an unexpected change; it does not defend against one in real time, on every read.

- **Identity from pins.** `renditions.pinned_identity` computes the identity from the stores alone:
  for each selection, the stored text of its receipt, original, rendition and `rendition-of` records
  and of the original's `preserved-metadata` submission sidecar (each record carries the SHA-256,
  size and path taken at ingest), the selection's own fields, and, when any selection is
  `docling` or lists `review_event_ids`, the review-event `state_token`. It opens no selected file.
  `tools/catalog/pins.py` reads those records with keyed lookups in one validated snapshot, with no
  copy of the current-record table. The checks that need only the catalog's records still refuse
  (rendition registered under the original, same path and hash, `indexed` coverage, policy allows the
  original, one sidecar). Freshness reads the review events with `read_snapshot(root)`, not
  `full=True`: the review-event store's own validation decides what that costs.
- **Build verifies in full.** `renditions.inputs` (publication: build, the `adapters` guards and
  build's post-check) still opens and re-validates every selected extraction, original and submission
  sidecar and the review qualification, and hashes the same pin texts into the same identity
  (`tests/test_workspace.py::test_identity_read_from_the_pins_equals_the_fully_verified_one`). An index
  built from files that verified at publication is fresh while the catalog's pins and the review state
  are unchanged. A catalog commit that re-pins a file (the form a future document-update ingestion will
  take) changes the identity and so makes the index stale, as it should.
- **Changed guarantee.** A selected extraction, original or sidecar modified, replaced or removed after
  ingest is **no longer a freshness failure**; readers no longer reopen those files. It is detected by
  the audit (below), by a verified read of that file, and by the next build (which refuses it).
  The old tamper tests that expected a reader to refuse are replaced, not removed:
  `tests/test_workspace.py::test_a_changed_retained_file_is_no_freshness_failure_but_the_audit_and_verified_reads_report_it`
  (same-size original, extraction bytes, a removed extraction and a symlink-swapped one: freshness
  passes; the audit reports path, kind and problem; `read_verified` refuses; build refuses),
  `::test_identity_read_from_the_pins_equals_the_fully_verified_one` and
  `::test_freshness_identity_opens_no_selected_file_and_the_memo_signs_the_stores` (an audit hook shows
  no selected file is opened). Unchanged: path-traversal, symlink and permission checks on every file a
  reader or the build opens, the catalog and review-event hash chains, treating document text as
  untrusted, and backups.
- **Integrity audit** (`python -m tools.context_index.integrity audit --root ROOT`; `tools/context_index/integrity.py`).
  It reads the catalog's present artifacts that carry a path and a hash (originals, retained
  extractions, submission sidecars, anything else registered), re-hashes each in up to four forked
  workers (the pool shared with the identity check, `tools/context_index/forked.py`, from a
  single-threaded process only; any doubt falls back to the sequential loop with the same result) and
  reports each mismatch with its path, kind (`original`, `extraction`, `sidecar`, or the artifact
  role), problem (`hash-differs`, `size-differs`, `missing`, `symlink`, `not-a-file`, `unreadable`,
  `unsafe-path`), expected and found hash and record. It lists at most `--limit` (default 1,000) but
  counts all. It is read-only (the catalog's read transaction ends before any file is hashed; the catalog is
  read through `observed_state`, so a missing or expired read-validation receipt is not rewritten; the
  audit writes nothing under the instance), never fails a read, and exits 0 clean, 1 with mismatches, 2 when it
  could not run (for a scheduler to alert on). `check PATH...` verifies named files the same way.
  README.md ("Integrity audit") shows a cron and a systemd timer. Evidence: `tests/test_integrity.py`.
- **Flagging a changed file per document** (Kata ejm8; `tools/context_index/flags.py`). A changed file
  does not fail the index; the document it belongs to is served **marked**, not withheld. Decision: the
  served text, locators and labels come from the index database, which was built from files verified
  at ingest, so a changed extraction, original or sidecar does not make that text wrong; what it
  undermines is the citation target (`source_location` into the original or extraction) and an
  `agent-generated` origin read from a sidecar. The document therefore gets the low-confidence reason
  `integrity-changed` (`confidence.REASONS`; details name the kind, path, problem and when the audit found
  it), its correction is "restore from backup or re-ingest through intake, rebuild, re-audit", and every
  other document is untouched. Withholding was rejected: it would hide verified ingest-time text and make
  one bad file an availability failure, against the owner's detect-and-flag direction.
  - **Where the flag comes from.** Reads still hash nothing. `integrity audit --record` (optionally
    `--db`) writes the audit result to a private (0600), checksummed report beside the index,
    `<index>.sqlite3.integrity.json` (ignored like the database), recording the index database's file
    identity, the audit start time, files checked, the mismatch count and every flagged document with its
    first three changed files (kind, path, problem, expected, found) and its changed-file count. Documents
    are resolved at record time from the index, so no cap on mismatches can leave a document unflagged. It is the only thing `--record` writes, never inside the data
    roots; plain `audit` still writes nothing. A reader loads it once per call (one small file) and
    ignores it unless it is private, owned by the user, within 64 MiB, checksum-valid and recorded for the
    very database being read; a rebuild replaces the database, so its flags end with it, and an audit
    that started before the index was replaced records nothing. A missing, corrupt, forged or foreign
    report means no flags: this is detection, not access control, and a forger who can write the index's
    `.local` directory has the same trust as the index database itself. A later clean audit clears a flag.
  - **Which documents.** A mismatch flags the indexed document at the same path (an extraction), and every
    selected rendition whose `source.path` is a changed original or whose receipt's sidecar changed (one
    `rendition_documents` pass over the index, no file read).
  - **Where it shows.** `search` hits and `get`/`get-many` pieces and documents carry the reason and the
    top-level legend explains it; `--low-confidence last|exclude` treat flagged documents like other low
    ones (counted in `low_confidence_excluded`). `status` adds `integrity: {audit, audited_at, checked,
    mismatches, flagged_documents, documents (at most 20, with the changed files), documents_not_listed,
    resolve}`; with no matching report it says `audit: none-for-this-index` and how to record one rather
    than implying a clean index. The progress text and page show the same count, list and command, and the
    paths and hashes in them are untrusted text (escaped, truncated to 200 characters). The stored label is
    unchanged, so an index is not stale for the reason and needs no rebuild beyond the one this code
    change causes (engine fingerprint).
  - **Publication.** Build and the publication guards still verify every selected file in full. When that
    fails, `build` now names every selected file that differs from its pin (kind, path, problem, expected,
    found; the first 20 and a count), refuses to publish, and leaves the prior index in place, still
    readable and flagged. Partial publication that excludes mismatched selections was not built: it would
    change the manifest/index relationship and the intake publication evidence; re-ingest and select the
    repaired document instead. A failure that is not a changed file keeps its own message.
  - **Changed guarantee.** Until now a changed retained file, once detected, took no part in reads; xv0n
    made it a non-failure and left detection to a report nobody read at read time. Now a read surfaces the
    last recorded audit's finding on the affected document only. The guarantee that a mismatched file is
    never *published* is unchanged (build refuses). Freshness still does not read files, so a change
    made after the last recorded audit is not flagged until the next one (the audit schedule bounds it;
    `read_verified` still refuses one file at a time).
  - Evidence (`tests/test_integrity_flags.py`):
    `::test_a_changed_file_flags_its_document_and_every_other_document_still_serves`,
    `::test_a_changed_original_flags_the_documents_extracted_from_it`,
    `::test_a_changed_original_flags_only_its_own_renditions`,
    `::test_every_flagged_document_is_recorded_however_many_files_changed`,
    `::test_low_confidence_exclude_and_last_treat_a_flagged_document_like_any_low_one`,
    `::test_a_clean_re_audit_clears_the_flag`, `::test_a_rebuilt_index_does_not_inherit_the_old_report`,
    `::test_a_forged_foreign_or_unsafe_report_means_no_flags`,
    `::test_the_audit_does_not_record_for_an_index_replaced_while_it_ran`,
    `::test_without_a_recorded_audit_status_says_so_instead_of_implying_clean`,
    `::test_flagged_documents_are_listed_up_to_a_cap_with_the_rest_counted`,
    `::test_get_many_labels_each_document_and_the_legend_explains_the_reason`,
    `::test_the_progress_panel_counts_and_lists_the_flagged_documents_with_the_resolution`,
    and `PublicationTests::test_a_build_refuses_every_changed_selected_file_at_once_and_keeps_the_prior_index` and
    `::test_a_missing_extraction_and_a_changed_original_are_both_named`.
- **Verified reads.** `integrity.read_verified(root, path)` opens one retained file, checks its SHA-256
  against the catalog's current pin (one hash per file actually opened) and returns the bytes or raises
  `IntegrityMismatch` with the same report entry. Any retrieval that opens an original or retained
  extraction beyond the index (for example a deeper search) must read through it. The retrieval paths
  today (`search`, `get`, `get-many`) answer from the index and open no original, so none calls it
  yet. Evidence: `tests/test_integrity.py::test_a_verified_read_returns_the_pinned_bytes_and_refuses_unpinned_paths`
  and `::test_a_changed_file_does_not_stop_reads_but_a_verified_read_refuses_it`.
- **Document updates.** Not built. The reference is the catalog's *current* record for a path, so an
  ingestion that re-pins a path through the catalog changes what freshness, the audit and verified reads
  expect without a code change (`tests/test_integrity.py::test_the_audit_follows_the_catalog_when_a_path_is_re_pinned`).
- **Memo** (`tools/context_index/proof_cache.py`, simplified). It remembers only the identity, in the
  ignored file `<index>.sqlite3.freshness.json`, so the pin reads are skipped while the catalog (and the
  review-event store, when selections need it) keep their exact file identity. The key is a SHA-256 of
  the memo format, instance root, `renditions` configuration, `policy.fingerprint`, validated roots, the
  hash of every toolkit `.py`, `.json` and `.sql` file and the runtime (Python, SQLite, Unicode data,
  `jsonschema`, `referencing`). It signs an `lstat` identity (device, inode, mode, size, mtime_ns,
  ctime_ns) of the stores only, no selected file or directory, and **has no UTC-date key**. A hit also
  needs a private (0600), non-symlink, owned memo of at most 1 MiB with a valid checksum, and no
  `-journal`, `-wal` or `-shm` beside a signed store. It is written only when the stores did not change
  during the computation and are older than 2 s (git's "racily clean" rule), through an
  `O_EXCL|O_NOFOLLOW` temporary file. Any other state is a miss. Publication never reuses it; `build`
  leaves its identity behind. Evidence (`tests/test_workspace.py`):
  `::test_build_leaves_a_private_freshness_proof_that_readers_reuse_without_the_full_proof`,
  `::test_freshness_proof_memo_misses_on_store_and_configuration_changes`,
  `::test_freshness_proof_memo_binds_the_review_event_store_when_selections_are_reviewed`,
  `::test_corrupt_forged_or_unsafe_freshness_proof_memo_is_ignored`,
  `::test_freshness_proof_with_racily_new_inputs_is_not_remembered`.
- **Cost** (read-only on an instance with 50,000 selected renditions, 40,000 review events and an
  865 MB catalog; immutable SQLite URIs, nothing written to the instance, intake running; the tables of
  the sibling Kata on store validation apply on top): cold `status` (the catalog and review-event stores
  already attested by their receipts) took 25.9 s wall and 1.85 GB peak RSS (the two checks of one
  `status`: 24.2 s cold, 1.2 s warm), and with this change 9.4 s wall and 0.71 GB (8.5 s cold:
  5.7 s the review-event replay and 2.4 s the pin reads and digest), a warm memo hit 0.35 s and 0.19 GB.
  Selection identity alone: 2.4-2.6 s here against 15.4 s for the full verification, with the same
  digest. The review-event replay is the sibling Kata's (store validation); the audit's cost is the
  retained files' bytes (about 1.7 GB read; the first run is cold-cache bound).
- **Residual.** The memo is unkeyed: an actor who can write the index's `.local` directory can forge it,
  the same trust as the index database itself. A file changed out of band is reported by the next audit,
  not by the read that opens it unless that read uses `read_verified`; an out-of-band edit of the catalog
  or review-event store is caught by those stores' own validation, unchanged here.

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
  chunk's documents share, so a chunk's stages settle together. A chunk's ledger commits do not grow with its
  size (Kata gfpd): `upload.stage_submissions` stages the receipt requests of all its new submissions with one
  `store.stage_many` commit (one catalog read, IDs numbered across the chunk in order, so the records equal
  one-at-a-time staging; each retained copy is durable before the request that names it), the prepare requests
  of text-family documents are staged with one commit and settle through `run_batch` (`extract-prepare` is a
  batch operation that runs each stage alone), and the registration requests are staged with one commit.
  `stage_many` reports a refused request on that request alone, and inserts the accepted ones in one
  transaction. PDFs, images and converted office documents still prepare one at a time, because their
  preparation can take seconds and a chunk settles under one ledger lock hold. `run` (the one-at-a-time path)
  also saves a new intent and records the running attempt in one commit; the intent is written first in the
  transaction, so it is durable whenever the attempt is, and a retry is never charged for an unfinished attempt.
- **Crash model.** A kill before the catalog COMMIT leaves SQLite's rollback journal and the prior revision
  (recovered like any killed catalog writer); a kill after it leaves the batch committed with stages still
  `running`, and the re-run finds each marker present and records its evidence; a kill between ledger members leaves
  an unfinished attempt that is reused. Retained copies, `registration.json` and `completion.json` are written
  without replacement and checked by content, so they too are completed, never duplicated.
- **Directory syncs per chunk (Kata 5n0q).** Each of those files is still written to a private partial file,
  fsynced and linked into place without replacement, but the directory entry is no longer synced after every
  link. `durable.batched_directory_syncs` defers the syncs of a stage to one `flush` that fsyncs each touched
  directory once (deepest first), and a stage flushes before anything that names the files: the chunk's retained
  copies before `store.stage_many` stages their requests, the chunk's renditions before `run_batch` records the
  prepare evidence, every `registration.json` before `catalog.submit_batch` commits the registrations, and the
  `completion.json` files after the commit before the ledger records the register evidence. A file found
  already present (an earlier run died after linking it) queues its directory again, so a rerun makes durable
  what the killed run may not have, and a directory created by a killed run has its parent synced too. Nothing
  else changes: the same files, names and bytes, no check weakened, and a failure inside a stage flushes nothing
  because nothing has committed yet. Outside a batch scope (`run` for one document, PDF, image and converted
  renditions, `upload.submit`) every sync happens at once as before. A text-family document goes from about
  15 fsyncs to about 8 in a 50-document chunk (10 to 5 directory syncs); the three files per document are kept.
  Evidence: `tests/test_intake_batch.py` (`DirectorySyncOrder`: no ledger or catalog commit and no recorded
  evidence while a directory entry made by a link, unlink or mkdir is unsynced, shown to fail when the flush is
  dropped; syncs per chunk; a kill before and after each flush completed by a re-run) and `tests/test_durable.py`.
- **Snapshots and receipts.** A chunk is one commit: one first-write-of-the-UTC-day replay and snapshot, one
  validation receipt, one lock hold. The lock is held for the chunk's validation and write (about the cost of one
  `submit` plus per-record encoding), not for the model-free extraction.
- **Compatibility.** The schema, `VERSION`, state tokens and revision hash chain are unchanged; `inspect` replays
  batch and single revisions identically, and history written before batching stays valid.

Evidence: `tests/test_catalog_batch.py` (one revision, idempotent markers, per-member rebase, refusal isolation
and equivalence with committing one by one on random inputs, kills before COMMIT/after COMMIT/after the receipt,
a failure between members, two-process contention) and `tests/test_intake_batch.py` (batched equals one-at-a-time
end state, revisions per chunk, ledger commits per chunk that do not grow with its size, `store.stage_many`,
failed documents isolated, a kill at 20 points of the chunk completed by a re-run
with no duplicate or lost document, two processes ingesting one instance). Measure with
`tools/diagnostics/batch_ingest_benchmark.py`.

### Intake ledger

- **Cadence (Kata qwtp).** A writer takes the lock and checks the ledger's *structure*, not its
  contents: the application ID and schema version on every lock, and SQLite `integrity_check` and
  `foreign_key_check` once per process for each ledger file (path, device and inode, so a replaced or
  restored file is checked again; a failed check is never remembered). There is no attestation or
  hand-off in `writer.lock`, which stays empty. A 36,000-request ledger opens in about 0.1 s in a fresh
  process instead of about 1.1 s, and every later lock costs about 10 ms. The O(ledger) pass (every
  request, stage, artifact digest, attempt and replay rule) is `python -m tools.intake.cli audit`
  (`store.audit`), also run by `relocate`. It is read-only, takes no writer lock, reads in keyset pages
  of 5,000 requests in short read transactions (about 0.9 s and 25 MB at 36,000 requests), and raises
  the first `corrupt_*` refusal. Who finds which tamper class: SQLite damage, orphaned rows and a wrong
  application ID or schema version are refused by the first writer or reader of a process; a changed
  request or artifact digest, a stage or attempt that breaks the shape rules, or a rewritten request is
  refused by `audit` and by `progress` (which verifies every page), and is **not** seen by a writer
  that opened the ledger earlier or whose structure is still sound. A writer reads and writes only the
  rows it touches; the ledger cannot reconstruct authority (the catalog and review events are
  authority), so a hand-edited ledger row misdirects work at worst, and `audit` is the check to run
  before an upgrade, a backup or after any unexplained failure.
- The writer lock is polled non-blocking with jitter for at most
  `LIBRARIAN_INTAKE_LOCK_WAIT_SECONDS` (default 30, max 3600; invalid values refuse)
  and the same value is the SQLite busy timeout. Exceeding it, or a transient SQLite
  BUSY/LOCKED during a stage, raises retryable `ledger_busy`; nothing was written.
  `settle` and cohort ingest retry up to three times. `progress` (`progress.read_ledger`) reads the ledger in
  keyset pages of 5,000 requests, each in its own short read transaction, and
  verifies each page afterwards (`store.verify_page`, the same rules `audit` runs), so it cannot hold a read lock across O(ledger) work and its
  memory is a page plus counters. It reduces the ledger to counts per distinct
  request signature and expands only the newest `--limit` items; each page is
  consistent, the pass as a whole is not one global snapshot.
- `progress` keeps its finished summary in memory keyed by the ledger's file identity
  (`file_identity`: device, inode, size, times and SQLite's change counter), `--limit`, the run set and the UTC date,
  stored only when the identity is unchanged from before the first to after the last
  page. A commit, a tampered row or a new UTC day re-verifies in full; the residual
  is that of the catalog receipt (an edit that restores size, times and change
  counter is seen at the next UTC-day check). Digests of requests and artifacts
  already proven are kept by SHA-256 (no raw bytes, no failures) and a hit still
  hashes the bytes in hand and compares the stored digest. Its catalog read never
  writes a receipt (`catalog_store.observed_state`): it trusts a receipt covering this exact file or
  this process's own full inspection today, and an unreadable receipt is ignored, never reported,
  so the full inspection decides; a catalog above `progress.INSPECT_LIMIT` is never inspected and
  is reported `unvalidated` instead (Kata 8as8). `progress-serve` (`progress_serve.Latest`) renders one page at a time, shares the
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
- `progress` also leaves out standing reviews that a later review replaced, and counts its
  headline funnel by document (Kata nbta). Before the page scan, `progress.ledger_index` reads
  names only: the first attempt start of each standing policy (`standing-<submission>-<rendition
  12>-<policy 8>`), the `upload-` receipt IDs and the derived `docs` rows. A standing review head is
  superseded for `policy` when the same rendition has a head under a policy first attempted later
  (policy arguments record its hash, not its version, so first use orders them; a never-attempted
  policy is newest), for `rendition` when the `docs` row's current rendition differs, and for
  `submission` when its submission's receipt was corrected; with `correction` these are
  `superseded_by` in `overall` and each run, verified with their page but not counted. Each
  receipt head is one document folded into exactly one state: its receipt until received, then
  its extraction and registration heads (the `docs` heads, matched by exact ID whatever their
  name, when the table exists; otherwise the newest by first attempt), then its review under the
  newest policy, and `mismatch`/`ambiguous` `docs` rows stay so. A document's furthest state is
  `selected`: publication requests do not name the documents they publish, so the publications
  and index sections report publication. The funnel sums to `documents.documents`; document attention groups are formed from the
  deciding request's code. Without the `docs` table the source is `requests` and rendition
  staleness is not detected. Publication requests are reported in `publications`, not as
  documents; request-level counts, rates, attention and `recent` are kept. At 77,000 documents
  and 324,000 requests (60% with an older-policy head) a cold render took 10 s at about 250 MB
  peak RSS against 9.3 s and 150 MB before; a cached render is unchanged. Tests: `DocumentTests`
  in `tests/test_intake_progress.py` (older-policy and old-rendition heads superseded, one count
  per document across extract and register requests, the cached pass agrees, the docs-absent
  fallback, a corrected submission, and a differently named correction the `docs` row names).
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
  the next writer to open it, inside that writer's lock and after its structure check, as one small committed
  transaction. The schema version stays 1, no row is written, a ledger that already has the index is
  not touched, and a reader or writer that holds the file past the lock wait makes the upgrade a
  retryable `ledger_busy` that a later writer completes. Evidence:
  `tests/test_intake_ledger_scaling.py::CorrectionIndexTests` (query plans, new and old ledger,
  idempotence, no full validation, busy reader).
- Evidence: `tests/test_intake_ledger_scaling.py` (`CadenceTests`: what a writer checks and when,
  alternating writer processes, replaced file, residual; `AuditTests`: every rule, page boundaries, no
  writes, beside a held lock, command line; bounded waits, progress), `tests/test_progress_scale.py` (reference-equal counts across page sizes,
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
- **Retrieval-quality evaluation** (`python -m tools.evals`, contract in
  `context/retrieval-evals.md`): a separate, explicit measurement, not derived from
  the diagnostics or audit logs. Cases (`retrieval-eval-cases-v1`) live in the
  ignored `config/evals.local.json` and are validated strictly before anything runs
  (unknown or duplicate fields, duplicate ids and contradictory expectations are
  refused). `generate` samples documents stratified by role, confidence level and
  rendition format with a seed, and builds each known-item query from distinctive
  terms of one indexed section (the FTS row's own title and text, since renditions
  index a search text that differs from their stored record), passing over queries
  already used or matching more than 200 sections so near-duplicates cannot share one;
  the document is always findable while rank is left to the run; it replaces only that seed's
  generated cases and is byte-identical when rerun on an unchanged index. `run`
  calls the CLI's own `load_manifest`, `readonly`, `freshness` and `search` in
  process, checking freshness once before and once after the whole batch (two
  checks, not one per case) and refusing a stale index, or a claim-correction change
  during the batch, without writing anything.
  The scorecard (`retrieval-eval-scorecard-v1`, 0600 under the ignored
  `tools/evals/.local/runs/`) is body-free: ids, closed failure codes, ranks,
  counts, confidence levels, aggregates (hit@1, hit@k, recall@k, MRR, must-not
  violations, absence accuracy, confidence-match rate, returned-confidence
  distribution), timings and provenance hashes (index metadata, standing policy
  identity without its scope, correction-store state, cases file, toolkit commit).
  `compare` exits 1 on a metric drop beyond tolerance or on per-case regressions
  above `--max-regressions`, and 2 on a scorecard missing a gated metric. Nothing reads a scorecard to change selection,
  ranking or qualification. Evidence: `tests/test_evals.py`
  (`test_refuses_malformed_cases`,
  `test_generation_is_deterministic_and_keeps_authored_cases`,
  `test_near_duplicate_documents_do_not_share_a_known_item_query`,
  `test_scores_hits_ranks_misses_must_not_absence_and_confidence`,
  `test_scorecard_is_body_free_and_private`,
  `test_stale_index_is_refused_without_a_scorecard`,
  `test_freshness_is_checked_once_before_and_once_after_the_batch`,
  `test_a_correction_recorded_during_the_run_is_refused`,
  `test_malformed_scorecards_are_refused`,
  `test_regressions_and_metric_drops_exit_nonzero`).
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
- The 75,000-entry manifest cap is an implementation limit, a runaway guard, not a design
  goal. It replaced 50,000 (Kata qzya, when the live instance held 49,864 of 50,000
  selections), which had replaced 20,000 (Kata q4ks, when about 11,600 reviewed selections
  sat staged behind a full manifest), which had replaced 5,000 (Kata jysk) once publishing
  was linear in selections (see "Publishing a cohort-sized manifest" in section 3). Measured
  at 50,000 and 75,000: see that section. Not measured above 75,000.
  Before Kata m0tg each select rewrote and revalidated the whole manifest (20 ms
  at 1,000 entries, 93 ms at 5,000, 274 ms at 20,000, so filling a manifest was
  quadratic); the append is flat.
- A single linear catalog hash chain (revisit triggers were: commit utilization
  above about a third of the ceiling with writers losing waits, multi-host
  writers, or a first-of-day hold above the 10 s wait even after the speedups).
  At 10.6k registrations on one production catalog the first of those had not been
  met.
