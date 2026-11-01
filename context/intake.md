# Durable intake operator contract

`python -m tools.intake.cli --root ROOT` operates one private installed instance.
Initialize the intake ledger explicitly; stage exact UTF-8 JSON, then run only a
named stage. The ledger is operational recovery state, never corpus authority.

- Request v1: `version`, `correlation_id`, nullable `correction_of`, `policy`
  (`version`, `sha256`), `retry` (`max_attempts`, `backoff_seconds`), and `stages`.
  Each stage has `id`, `kind`, `operation`, `arguments`. Closed fields, duplicate
  key refusal, 1 MiB request limit, 16 stages, 1–10 attempts, 0–86400s backoff.
- Fixed adapters: receipt/catalog-submit; extraction/docling-prepare,
  docling-verify, docling-register; review/review-append;
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
  registered Docling rendition. Only findings that inspected every page, text
  record, table and individual warning (`#/warnings/N`), with no contradictions or missing evidence, are
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
- `drop-scan --drop DIR` is discovery only. `drop-submit --drop DIR` submits each
  `NAME` paired with `NAME.submission.json` (the v1 descriptor fields). The drop
  must exist and contain `.librarian-drop`; a missing or unmarked mount is
  `drop_unavailable`, never an empty success. Drop contents are never modified.

## Progress view

`progress` and `progress-serve` are read-only, body-free observation of a run;
they never stage, run, replay, repair or initialize anything, and a missing,
locked, public or corrupt ledger, catalog, review-event store or index is
reported as that source being unavailable with its error class, never a crash.

```sh
.venv/bin/python -m tools.intake.cli --root . progress [--format text] [--run bulk=upload-bulk-]
.venv/bin/python -m tools.intake.cli --root . progress-serve [--port 8765] [--refresh 5]
```

- The ledger is opened `mode=ro` without the writer lock and fully validated in
  one read transaction; the catalog and review events use their validated
  read paths, and the index reports freshness, stale reasons, indexed
  documents/sections and selected documents/renditions.
- Each request maps to one funnel state from its ordered stage statuses and
  last attempt: `staged`, `received` (receipt committed), `extracting`,
  `extracted`, `pending-review`, `reviewed`, `qualified`, `selected`,
  `published`, or the exceptions `rejected`, `deferred`, `exception`
  (blocked `standing-<outcome>-*` codes), `skipped-by-format`
  (`unsupported-type`, `type-mismatch`, `unsupported-format`,
  `skipped-by-format`), `needs-reader` (`needs-reader*`), `blocked` and
  `dead-letter`. Per kind, stage counts by ledger status are also shown.
- Recent and needs-attention items carry correlation ID, current stage,
  operation, status, funnel state and last attempt outcome/category/code and
  times only; request arguments, intents, evidence and document text are never
  read into the report. `--run NAME=PREFIX` groups correlation IDs by prefix.
- `progress-serve` serves `/` (HTML, `meta refresh`, no scripts or external
  assets) and `/progress.json`, GET/HEAD only, with `Cache-Control: no-store`
  and a deny-all CSP. It binds `127.0.0.1` by default, rejects non-loopback
  `Host` headers, and refuses any non-loopback `--host` with
  `non_loopback_bind` unless `--allow-non-loopback` records explicit approval.

No acquisition, connector, scheduler, unauthorized review/selection, migration,
model service or return transport is supplied by this interface.
