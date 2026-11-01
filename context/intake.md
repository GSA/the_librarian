# Durable intake operator contract

`python -m tools.intake.cli --root ROOT` operates one private installed instance.
Initialize the intake ledger explicitly; stage exact UTF-8 JSON, then run only a
named stage. The ledger is operational recovery state, never corpus authority.

- Request v1: `version`, `correlation_id`, nullable `correction_of`, `policy`
  (`version`, `sha256`), `retry` (`max_attempts`, `backoff_seconds`), and `stages`.
  Each stage has `id`, `kind`, `operation`, `arguments`. Closed fields, duplicate
  key refusal, 1 MiB request limit, 16 stages, 1–10 attempts, 0–86400s backoff.
- Fixed adapters: receipt/catalog-submit; extraction/docling-prepare,
  docling-verify, docling-register; extraction/extract-prepare, extract-register
  (strict UTF-8 `text`, `gemini-txt`, `odt`); review/review-append;
  publication/index-build; review/standing-review, publication/standing-select,
  publication/standing-publish; return/await-ack. See `tools/intake/contracts.py` for
  argument fields. Preserve native attribution and original concurrency tokens.
- Intake stores exact requests before effects. Prepared intent commits before
  an execution attempt. Restart reconciles unfinished attempts without charging
  another attempt. Catalog/review retries use original request IDs and verified
  native backups; evidence records original committed revisions, not current
  unrelated revisions. Docling reruns the offline native lifecycle, binding
  source/runtime/rendition identity; no auto-review or selection. Intake passes
  the pinned identity to the native lifecycle as `expected_identity`, so a
  source or runtime change refuses before any rendition or registration is
  published and each attempt converts once.
- Publication pins manifest bytes, source hashes and native metadata/freshness.
  A matching fresh publication can reconcile without rebuilding. Input drift
  blocks; correction requires a new linked request, not refreshed replay tokens.
  Intake passes `index.build` an optional `publication_guard` callback, which the
  native builder invokes inside the publication lock immediately before
  atomically replacing the destination; a raising guard preserves the prior
  index. Inspection reports current availability separately from historical
  completion, including when a published index later becomes unreadable.
- Standing-policy stages (`tools/intake/standing.py`) pin a private (0600)
  `standing-policy-v1` file by SHA-256 (id, version, active/revoked status,
  reviewer, receipt/audience scope, page/record/findings budgets, selection
  context) and an attributed `standing-findings-v1` record for one exact
  registered Docling or extraction rendition. Only findings that inspected every
  page, text record, table and individual warning (`#/warnings/N`) of a Docling
  rendition, or every payload record (`records: [index]`) and warning
  (`#/payload/warnings/N`) of a text, transcript or ODT rendition, with no
  contradictions or missing evidence, are
  `qualified`; parser success and confidence never qualify. Other outcomes
  (`pending-review`, `rejected`, `deferred`, `exception`) block visibly with a
  reason code and write nothing. Missing findings are a retryable
  `review-route-unavailable`. Corrections, new mappings or existing independent
  reviews of the rendition require independent review. A qualified review
  appends one deterministic event under its request ID (idempotent after a lost
  acknowledgment); selection re-evaluates, marks the catalog child indexed and
  atomically adds the manifest entry; publication pins the policy, and current
  availability is false once the policy is revoked/changed or sources drift.
  Qualification compares the rendition's full source identity (artifact,
  source IDs, path, hash, bytes, audience, handling refs) with the current
  catalog registration. Publication and availability refuse a manifest whose
  standing-reviewed selections were authorized by a different policy or fall
  outside the publishing policy's receipt/audience scope
  (`standing-selection-unauthorized`).
- Undecodable or invalid native inputs block durably with a recorded attempt
  outcome instead of surfacing a traceback, and ledger write failures after
  validation report an unavailable ledger rather than claiming corruption.
- Retryable failures record eligibility time; nothing sleeps or schedules itself.
  `replay` explicitly resets a blocked/dead-letter stage's bounded cycle while
  retaining exact request, intent and all attempt/replay history. Other requests
  remain runnable. Unfinished work is resumed with `run`/`resume`, not replay.
- Return requires a complete root-relative disposition artifact and its SHA-256.
  `run` records `return-pending`. `acknowledge` accepts only matching
  `delivery_correlation`, `disposition_sha256`, `transport_receipt`; exact repeats
  are idempotent and changed receipts conflict. `delivered` is a supplied
  transport acknowledgment claim, not independent remote or business acceptance.
- `.local/intake` is 0700; database/lock are 0600, symlink-free, LINT schema 1,
  FULL synchronous SQLite with process serialization. Missing/incompatible or
  corrupt ledgers refuse operation; never rebuild corpus authority from this
  ledger. Protect operational request bodies like other private instance data.

## Manual upload and local drop

`submit-file FILE --submission-id ID --format {text,gemini-txt,odt,pdf,image}
--actor A --reason R` stages one explicitly supplied file. Optional
`--name`, `--correction-of`, `--expected-sha256` and declared
`--origin-system/--origin-locator/--origin-version`.

- The file is read once through a symlink-free, regular-file-only descriptor and
  refused if it changes during the read, exceeds the existing extractor limit, or
  fails the extension/signature check for an already supported format.
- Bytes and a hash-bound `submission.json` provenance sidecar are atomically
  published (0600 in 0700 directories) under
  `<first original root>/intake/<submission-id>/`, then registered as source,
  receipt, original and preserved-metadata records through a durable
  `catalog-submit` stage with correlation `upload-<submission-id>`.
- The acknowledgment reports `receipt` (committed, pending, retryable, blocked,
  dead-letter) separately from `processing`; upload never extracts, reviews,
  selects or qualifies anything.
- A filename never supplies a remote identity or version; only declared origin
  fields do (`identity_basis`). Equal bytes under distinct submissions remain
  distinct sources and receipts.
- An exact retry returns the existing acknowledgment and resumes unfinished
  work. Changed bytes or descriptor under one submission ID are a
  `submission_conflict`; send a new submission with `--correction-of`, which
  reuses the parent source and adds a `version-of` relationship. A parent whose
  receipt durably blocked or dead-lettered without committing (for example a
  stale catalog state after an interruption) may also be replaced this way: the
  replacement plans fresh records against the current catalog, links the
  ledger requests, and leaves the stranded request visible. A parent that can
  still commit is `correction_parent_pending`; retry it exactly instead.
- `--origin-kind local-path` records a copy of a local file: a system label and
  a relative locator (`identity_basis: local-path-copy`), never a remote
  identity or version.
- `drop-scan --drop DIR` is discovery only. `drop-submit --drop DIR` submits each
  `NAME` paired with `NAME.submission.json` (the v1 descriptor fields). The drop
  must exist and contain `.librarian-drop`; a missing or unmarked mount is
  `drop_unavailable`, never an empty success. Drop contents are never modified.

No acquisition, connector, scheduler, unauthorized review/selection, migration,
model service or return transport is supplied by this interface.

## Read-only cohort intake

`python -m tools.intake.cohort classify --root ROOT --inbox DIR [--report FILE]`
gives every file below a read-only inbox exactly one disposition and prints
body-free counts: `admit` (with format `text`, `gemini-txt` or `odt`; a `.txt`
that parses as a Gemini transcript is `gemini-txt`), `needs-runtime` (PDF and
images awaiting a verified Docling runtime), `needs-reader` (no existing
reader, for example Office formats), `archive-expanded` (a zip whose sibling
`_extracted/<name>/` is classified instead), `skipped-asset` (web site assets),
`skipped-artifact` (alternate-data-stream sidecars such as `:Zone.Identifier`),
`unreadable` (fails strict UTF-8 or signature checks), `empty`, `over-limit` and
`unsafe-path` (symlinks and non-regular files). Suffix is never format proof.

`ingest ... --system LABEL --actor A [--limit N]` submits each admitted file as
a `local-path` upload (stable `inbox-<hash>` submission ID, ASCII-safe name,
percent-encoded inbox-relative locator, pinned SHA-256) and runs
`extract-prepare` then `extract-register` through the ledger. Outcomes are
counted as `received`, `extracted`, `failed`, `changed-since-intake` and
`not-processed`; reruns are idempotent. The inbox is never written. The
optional `--report` (0600) lists per-file paths, hashes and outcomes and is
private instance data. Extraction never reviews, selects or qualifies.

## Model-assisted standing review

`tools/intake/model_review.py` turns one model's review of one exact extraction
rendition into a private `standing-findings-v1` record under
`<intake_home>/findings/`, exactly as the model reported it: partial inspection
is never filled in, and replies that do not parse, name an unknown record or
warning, or exceed the prompt or record budget write no findings. The standing
policy's `reviewer` must be `usai:<model>`. Prompts mark document text as
untrusted. The transport is restricted to models configured for the `usai`
provider in a Pi `models.json`.

`python -m tools.intake.model_review evaluate --root ROOT --models-config FILE
--model M ... --rendition PATH ... [--controls]` scores candidate models on a
sample (complete inspections, failed replies, decisions, token usage, time, and
whether a synthetic garbled/injection control was rejected) and prints a
body-free report with the selected model and rule. `estimate()` extrapolates
sample token usage to the admitted count and bytes for a budget ceiling. Record
the chosen model, rationale, estimate and approved ceiling privately before any
bulk review, and actual usage after it.
