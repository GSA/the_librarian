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
  (strict UTF-8 `text`, `gemini-txt`, `mhtml`, `odt`); review/review-append;
  publication/index-build; review/standing-review, publication/standing-select,
  publication/standing-publish; return/await-ack. See `tools/intake/contracts.py` for
  argument fields. Preserve native attribution and original concurrency tokens.
- Intake stores exact requests before effects. Prepared intent commits before
  an execution attempt. Restart reconciles unfinished attempts without charging
  another attempt. Catalog/review retries use original request IDs and daily
  verified native snapshots (a retry or same-day write reports `backup: null`); evidence records original committed revisions, not current
  unrelated revisions. Docling reruns the offline native lifecycle, binding
  source/runtime/rendition identity; no auto-review or selection. Intake passes
  the pinned identity to the native lifecycle as `expected_identity`, so a
  source or runtime change refuses before any rendition or registration is
  published and each attempt converts once.
- Concurrent intake agents may share one instance ([0013](../docs/decisions/0013-concurrent-intake-writers.md)).
  The catalog writer lock waits up to 10s across processes. A catalog write
  validates only its delta when the database file identity (device, inode, size,
  mtime, ctime, SQLite change counter) matches a prior full validation made the
  same UTC day ([0015](../docs/decisions/0015-trusted-head-catalog-commit.md),
  [0017](../docs/decisions/0017-attested-file-identity-for-catalog-receipts.md)), and replays all history otherwise,
  on the first read or write of each UTC day and under `status --audit`; that
  first write replays the history once, holding the writer lock about as long as one `inspect`
  plus writing the snapshot (about 5 s at 25k registrations, 20 s at 100k;
  [0016](../docs/decisions/0016-single-replay-daily-snapshot.md)). A lost race is a
  retryable `catalog-stale-state` (another writer committed after the pin) or
  `catalog-busy` (lock wait exceeded) attempt, never `native-input`.
  `catalog-submit`, `extract-register` and `docling-register` rebase an outdated
  pin when no later revision wrote any record they write; overlap blocks as
  `catalog-conflict` and needs a correction. Upload and cohort ingest retry
  retryable stages within the request's attempt budget. `recover-conflicts`
  reports registration stages blocked by writer races (including legacy
  `native-input`), skipping corrected parents and overlapping writes; add
  `--apply` to replay and settle them.
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
  context) and an attributed `standing-findings-v2` (or legacy `-v1`) record for one exact
  registered Docling or extraction rendition. Only findings that inspected every
  page, text record, table and individual warning (`#/warnings/N`) of a Docling
  rendition, or every payload record (`records: [index]`) and warning
  (`#/payload/warnings/N`) of a text, transcript or ODT rendition, with no
  contradictions or missing evidence, are
  `qualified`; parser success and confidence never qualify. The v2 advisory
  `unverifiable_references` list (claims depending on material the document
  cites but does not contain) never blocks; a qualified event states only its
  count. Other outcomes
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
- A writer fully validates the ledger (SQLite integrity, digests, stage and
  attempt shape) unless the previous clean writer attested the file
  ([0014](../docs/decisions/0014-intake-ledger-handoff-and-bounded-waits.md)).
  On clean unlock a writer records the file identity and SQLite change counter
  in `writer.lock` (any process, not just the same one); the next writer consumes
  and truncates it under the lock, and skips the O(ledger) pass only when the file
  still matches. A foreign commit, journal, replaced file, crash or failed unlock
  leaves no matching attestation and forces full validation, so alternating
  writers do not repeat it per hand-off. The attestation is a hint that a
  validated writer finished last, not proof against someone who edits the file
  and forges the counter.
- A locked block that raises a read-only refusal (for example `unknown_request` from a
  cohort existence probe) still hands off the attestation, because the file is
  byte-identical to what the writer validated; a raise after any write does not.
- The ledger writer lock wait is bounded (default 30s, set
  `LIBRARIAN_INTAKE_LOCK_WAIT_SECONDS` or pass `wait`; the same value is the SQLite
  busy timeout). Exceeding it, or a transient SQLite BUSY/LOCKED during a stage,
  raises retryable `ledger_busy` (CLI: `{"error":"ledger_busy","retryable":true}`),
  never `ledger_unavailable` or a hang. `runner.settle` and `cohort ingest` retry it
  up to three times; an entry still busy fails visibly as `ledger/ledger_busy` and
  the run continues, so the next run resumes it. A stage whose run reports busy
  while recording a failure is a retryable attempt, not blocked. `ledger_unavailable`
  remains for non-transient SQLite failures after validation.
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

`submit-file FILE --submission-id ID --format {text,gemini-txt,mhtml,odt,pdf,image}
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

## Throughput target and measurement

Reference target for a large cohort on an instance of production size (about 100 MB
catalog, 25,000 revisions, about 130 MB ledger, 35,000 requests), on a host that is not
otherwise saturated: **at least 10 files/min sustained on the instance (a 5,000-file
cohort in under 8.5 hours), at least 20 files/min on the synthetic production-size harness
(`ledger_scale_benchmark.py`, which measured about 28), p95 per-file latency under 10 s,
and the last decile of a run no more than 2x the first decile** (measured 1.7x at 1,600
synthetic files). Each file is two catalog commits. The model behind it is, per file, at this size:
about 14 whole-file catalog bindings (`serialize` + SHA-256, about 0.11 s each at 100 MB),
at most one full ledger validation per process, two delta commits (about 0.1 s each), and
extraction. Measure with the synthetic harnesses; never point them at a real instance:

```sh
python3 tools/diagnostics/ingest_benchmark.py --sizes 100,200,400 --resume
python3 tools/diagnostics/catalog_lock_benchmark.py --sizes 400,1600,6400,16000 --samples 3
python3 tools/diagnostics/catalog_commit_ceiling.py --size 1600 --workers 1,2,4
python3 tools/diagnostics/ledger_scale_benchmark.py --ledger 36000 --catalog 25600 --files 40
```

`ledger_scale_benchmark.py` reports `full_ledger_validations`; it should be about 1 per
process, not about 2 per file. Timings come from a shared host: compare shapes, CPU and
counts rather than third digits.

## Progress view

`progress` and `progress-serve` are read-only, body-free observation of a run;
they never stage, run, replay, repair or initialize anything, and a missing,
locked, public or corrupt ledger, catalog, review-event store or index is
reported as that source being unavailable with its error class, never a crash.

```sh
.venv/bin/python -m tools.intake.cli --root . progress [--format text] [--run bulk=upload-bulk-]
.venv/bin/python -m tools.intake.cli --root . progress-serve [--port 8765] [--refresh 5]
```

- The ledger is opened `mode=ro` without the writer lock. The read transaction
  only runs SQLite's integrity checks and copies rows; digest and shape
  verification runs after it ends, so a large ledger cannot starve a writer's
  commit. Both paths verify the same rules. The page still reports
  `ledger_unavailable` if a writer holds the database past its read timeout. The catalog and review events use their validated
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

## Read-only cohort intake

`python -m tools.intake.cohort classify --root ROOT --inbox DIR [--report FILE]`
gives every file below a read-only inbox exactly one disposition and prints
body-free counts: `admit` (with format `text`, `gemini-txt`, `mhtml` or `odt`; a `.txt`
that parses as a Gemini transcript is `gemini-txt`; a text-suffix file whose
content starts with MIME headers, such as a Confluence "Exported From Confluence"
`.doc`, is `mhtml`), `needs-runtime` (PDF and
images awaiting a verified Docling runtime), `needs-reader` (no existing
reader, for example Office formats, or MIME that cannot be read strictly; the
report carries a `code` such as `mime-malformed`, `mime-no-html`, `mime-encoding`,
`mime-charset` or `mime-empty`, and raw encoded text is never admitted instead), `archive-expanded` (a zip whose sibling
`_extracted/<name>/` is classified instead), `skipped-asset` (web site assets),
`skipped-artifact` (alternate-data-stream sidecars such as `:Zone.Identifier`),
`unreadable` (fails strict UTF-8 or signature checks), `empty`, `over-limit` and
`unsafe-path` (symlinks and non-regular files). Suffix is never format proof.

`mhtml` extraction (`tools/extraction/mhtml.py`, stdlib `email` + `html.parser`)
recognises MIME by content, reads only non-attachment `text/html` parts with
strict transfer-encoding and charset decoding, converts them to plain-text lines
and never follows links, scripts or images. The payload records
`method {name: "mhtml-html-text", version}` and the parts read; the extractor
identity covers the reader code, and a stored rendition must equal a fresh
conversion of the original bytes. A file earlier ingested as plain `text` gets a
new `mhtml` submission that is recorded as a correction of the old one
(`correction_of`), so it is extracted again rather than kept. Bump
`mhtml.METHOD_VERSION` when conversion output changes.

`ingest ... --system LABEL --actor A [--limit N]` submits each admitted file as
a `local-path` upload (stable `inbox-<hash>` submission ID, ASCII-safe name,
percent-encoded inbox-relative locator, pinned SHA-256) and runs
`extract-prepare` then `extract-register` through the ledger. When an inbox
file's bytes change after intake, a content-salted submission ID is derived and
submitted as a correction of the prior submission (`correction_of`). Outcomes are
counted as `received`, `extracted`, `failed`, `changed-since-intake` and
`not-processed`; reruns are idempotent. `retried_after_conflict` counts entries
(and attempts) that lost a writer race and were retried, separately from
failures; `failed_by_code` groups failures by `stage/code`, and each failed
entry carries body-free `stage`, `status`, `category`, `code` and `attempts`. The inbox is never written. The
optional `--report` (0600) lists per-file paths, hashes and outcomes and is
private instance data. Extraction never reviews, selects or qualifies.

`qualify ... --system LABEL --actor A --policy PATH --manifest PATH
--models-config FILE --model M --context LOCATOR [--context LOCATOR ...]
[--index DB] [--concurrency N]` runs `ingest`, ensures the private standing policy and
selection manifest exist (`--context` names maintained documents under the
policy's `markdown_roots` and is required; nothing is written without it), and
runs `model_review.review` then stages `standing-review` + `standing-select`
(`standing-<submission-id>-<rendition-hash>-<policy-hash>`) for each extracted rendition that
has no saved review. A changed rendition gets a new review; an unchanged one
reuses its ledger stages with no model call. The
manifest is first pruned to the cohort's current renditions, then reconciled
to exactly the qualified ones, adding any that are missing. Policy, manifest and context paths are validated (instance-relative, no
symlinks, existing maintained sections) before anything is ingested or written;
manifest and policy files are replaced atomically.  `qualify` always processes the whole inbox
(`--limit` exists only on `ingest`). Changing an active policy's content does not
requalify old evidence: renditions already reviewed under another policy stay
`pending-review` until an explicit policy-change review correction is made. `standing-publish` builds the context index;
a saved publication is reused only while the index is present and fresh,
otherwise a new revision is staged. A review an operator later superseded is reported `pending-review` (`review-superseded`) and its saved selection is not restored; the operator's corrected selection is kept.
Bumping the standing policy version re-reviews every rendition; a new-version review that qualifies supersedes the same policy's earlier-version standing review of that rendition (same source and scope) and replaces its manifest selection, (recorded at least a second after it); operator corrections, reviews from any other policy and independent reviews still block as `conflicting-review`.
A standing review blocked as `changed-standing-inputs` (its saved intent no longer matches current inputs, e.g. after a replay) is restaged as a correction request `<correlation>-c<n>` with the same review arguments and event and fresh request IDs; later runs follow the `correction_of` chain to its newest request.
The policy (permissions, reviewer match), manifest and index destination are preflighted before ingest. Incomplete or failing reviews remain visibly
`pending-review`, `rejected`, `deferred` or `exception` without indexing.
`--concurrency N` (default 6) keeps up to N model calls in flight, each for a
rendition with no saved `standing-<...>` request; staging, runs, selection,
manifest writes and usage accounting stay on one thread in inbox order, so
results match `--concurrency 1`, which is fully serial.
Stdout prints body-free `counts`, token `usage` and `retries`: per-code counts
(`http-429`, `http-503`, `network`, ...) of transport retries during the run.

## Model-assisted standing review

`tools/intake/model_review.py` turns one model's review of one exact extraction
rendition into a private `standing-findings-v2` record under
`<intake_home>/findings/`, exactly as the model reported it: partial inspection
is never filled in, and replies that do not parse, name an unknown record or
warning, or exceed the prompt or record budget write no findings. The standing
policy's `reviewer` must be `usai:<model>`. Prompts mark document text as
untrusted. The reply lists internal `contradictions`, `missing_evidence` the
rendition itself needs (truncated sections, absent tables or figures), and
advisory `unverifiable_references` to cited outside material; replies without
the advisory list still parse. The transport is restricted to models configured
for the `usai` provider in a Pi `models.json` and sends only to that provider's
`baseUrl` (redirects are refused). HTTP 429, 500, 502, 503, 504 and network or
timeout errors are retried up to 5 calls in total, waiting a numeric
`Retry-After` or exponential backoff with jitter (2 s, 4 s, ...), each wait
capped at 60 s and all waits within 600 s; other HTTP errors and invalid replies
fail at once and the rendition stays `pending-review`.

`python -m tools.intake.model_review evaluate --root ROOT --models-config FILE
--model M ... --rendition PATH ... [--controls]` scores candidate models on a
sample (complete inspections, failed replies, decisions, token usage, time, and
whether a synthetic garbled/injection control was rejected) and prints a
body-free report with the selected model and rule. `estimate()` extrapolates
sample token usage to the admitted count and bytes for a budget ceiling. Record
the chosen model, rationale, estimate and approved ceiling privately before any
bulk review, and actual usage after it.
