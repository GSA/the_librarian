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
- Review-event reads (`qualify`, standing review, progress, `event_store status`) reuse a
  full validation held in process memory while the events database file identity (device,
  inode, size, mtime, ctime, SQLite change counter), the contract hash and the UTC date are
  unchanged and the chain heads re-read from the file still match. Any commit, swap or edit,
  a restart or a new UTC day validates everything again; `event_store.py audit` always does.
  An edit that restores identity and leaves the chain heads alone is seen only then (the
  catalog receipt's residual). Usage and evidence:
  [design](../docs/design/current-state.md#review-event-validation).
- Concurrent intake agents may share one instance ([design](../docs/design/current-state.md#concurrent-writers)).
  The catalog writer lock waits up to 10s across processes. A catalog write
  validates only its delta when the database file identity (device, inode, size,
  mtime, ctime, SQLite change counter) matches a prior full validation made the
  same UTC day ([design](../docs/design/current-state.md#what-a-catalog-write-validates)), and replays all history otherwise,
  on the first read or write of each UTC day and under `status --audit`; that
  first write replays the history once, holding the writer lock about as long as one `inspect`
  plus writing the snapshot (about 3 s at 25k registrations, 11 s at 100k;
  [design](../docs/design/current-state.md#what-a-catalog-write-validates)). A lost race is a
  retryable `catalog-stale-state` (another writer committed after the pin) or
  `catalog-busy` (lock wait exceeded) attempt, never `native-input`. Such an attempt wrote
  nothing, so it does not spend `max_attempts` ([design](../docs/design/current-state.md#concurrent-writers)):
  it counts against a separate bound of 30 lost races per stage cycle, and one `settle`
  call retries at most 6 in a row (about 60 s at the 10 s catalog wait) before it returns
  the stage `retryable` for the next run to resume. Faults still spend `max_attempts`.
  `tools/diagnostics/catalog_contention_budget.py` reproduces it.
  `catalog-submit`, `extract-register` and `docling-register` rebase an outdated
  pin when no later revision wrote any record they write; overlap blocks as
  `catalog-conflict` and needs a correction. Upload and cohort ingest retry
  retryable stages (see above for the lost-race bound). `recover-conflicts`
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
  ([design](../docs/design/current-state.md#intake-ledger)).
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

Planning a submission (`upload.records_for`) no longer scans history ([design](../docs/design/current-state.md#intake-ledger)):
the next `S`/`D` numbers come from the highest current ID per prefix in the catalog's primary-key index plus
the highest promised by any `upload-*` request in the ledger, read incrementally (only requests added since the
previous call, by row id, each decoded once per process; a different or smaller ledger resets it);
staged, retrying, blocked and dead-letter requests keep their IDs reserved. Profile it with
`python3 tools/diagnostics/upload_planning_benchmark.py --catalog 1600,6400,25600 --ledger 36000`, and use
`ledger_scale_benchmark.py --matched --warm` (a ledger with one committed upload request per registration, and
the one-off daily replay excluded) for per-file figures.

**Replaying an unchanged inbox** (`ingest`, and the `ingest` inside every `qualify`) re-checks every file:
`classify` reads and hashes each admitted file once and parses MIME HTML and Gemini transcripts for the
disposition, `submit` re-reads it against `expected_sha256` and the retained copy, and the ledger decides
what is already received and extracted. Hashing is about 1% of the cost. The rest was repeated ledger opens
(about nine per file, each re-validating the storage roots and walking the path for symlinks) and the same
receipt request read several times. A replay now opens the ledger three times per entry (the receipt request
with its summary, then the correction-chain head of the extraction and of the registration) and
reuses only a receipt stage that has `succeeded`, and the storage-root check is reused for an identical policy,
with the symlink walk done once per path component. A modified file is still detected (a new content-salted
submission superseding the old, or `changed-since-intake`). Measure it with
`python3 tools/diagnostics/ingest_benchmark.py --sizes 2000 --replay --fixture mixed --padding 1800 --reuse DIR`
(wall and CPU seconds, bytes read from `/proc/self/io`, and a digest of the whole report so two `--tree`
checkouts can be compared): about 11 to 5 ms per file at 2,000 and 10,000 synthetic files of about 133 KiB.
A MIME HTML file costs about 8 ms more in its parser during `classify`; that is the remaining floor.

**Extraction work per document.** Each extract stage reads the catalog snapshot, the original and the extractor
identity afresh (`extract.bundle`) and parses the original; a first ingest used to do that six times per document
and now does it four times and parses once (the newest parse is kept, keyed by format, source SHA-256 and extractor
SHA-256). A source or extractor change between `prepare` and `register` still blocks: each stage re-reads and
compares the pinned identity before publishing or submitting (`changed-extraction-identity`), and `extract.run`
re-reads and re-hashes the original and recomputes the extractor identity under the catalog writer lock before and
after publication and before and after registration (`Source changed during extraction/registration`). See the
design document for why each check remains. Measure it with `tools/diagnostics/ingest_benchmark.py` (the
`tools.extraction.extract.run` and `upload.reserved_ids` timers) on a seeded instance.

**Catalog lookup indexes.** Every write probes the committed catalog for records that reference a record it
rewrites and for artifacts that already hold a path it moves. Catalogs created now have lookup indexes and a
`refs` table for that, so those probes no longer scan the catalog; first ingest into a large catalog was about
4x faster at 40,000 synthetic documents. An older catalog keeps working without them (slower, same decisions)
until the operator runs, once and with no other writer in the way:

```sh
python -m tools.initialize --migrate-catalog-indexes      # idempotent; verifies and takes a snapshot first
python -m tools.catalog.catalog_store migrate-indexes --check   # report "legacy" or "indexed"; writes nothing
```

If `audit` reports "Catalog lookup structures are incomplete or differ", run
`python -m tools.catalog.catalog_store migrate-indexes --rebuild`: it recreates them only when the rest of the catalog
verifies. Restoring a snapshot taken before the migration yields a working catalog without them; run the migration again.
If a migration process was killed it can leave SQLite's rollback journal, which every command refuses as an unsupported
sidecar: confirm no catalog process is running, then run `migrate-indexes --recover`, which (under the writer lock) lets
SQLite roll the journal back, verifies the catalog in full and migrates.

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

- The ledger is opened `mode=ro` without the writer lock and walked in keyset pages of 5,000
  requests, each in its own short read transaction (SQLite's identity, integrity and foreign-key
  checks run first). Digest and shape verification of a page runs after its transaction ends, so a
  large ledger cannot starve a writer's commit, and it applies the same rules as `store.verify`
  (one shared `store.check_request`). Memory is a page plus counters, not the ledger: the
  report is reduced to counts per distinct request signature (stage layout, statuses, last
  attempt code) and only the newest `--limit` items per list are expanded. Each page is
  consistent; the pass as a whole is not one global snapshot, so a request staged mid-pass is
  counted or not, never torn. The page still reports `ledger_unavailable` if a writer holds the
  database past its read timeout. The catalog uses its validated read path without writing a
  receipt: a read-validation receipt that covers this exact file (0017) or this process's own
  full inspection of the identical binding today vouches for it, otherwise the whole history is
  inspected. Review events are validated in full on a miss of the in-process memo ([design](../docs/design/current-state.md#review-event-validation)), so a render after any review-event commit, restart or new UTC day replays them; the index reports freshness, stale
  reasons, indexed documents/sections and selected documents/renditions.
- Nothing is re-verified while the ledger is unchanged. The finished summary is kept in
  memory keyed by the ledger's file identity (device, inode, size, mtime, ctime, SQLite change
  counter, as in 0014/0017), `--limit`, `--run` set and UTC date, and is stored only when the
  identity is the same before the first and after the last page. Any commit, rewritten row or
  new UTC day misses and verifies in full, so a tampered ledger is reported `unavailable` on the
  next render; the same residual as 0017 applies (an edit that restores size, times and counter
  is seen at the next UTC-day check). Digests of requests and artifacts already proven are kept
  (32-byte keys, never raw bytes, never failures) and a hit still hashes the bytes in hand and
  compares the stored digest. Nothing is written to disk.
- The report says whether the run is moving: `rates` counts attempts finished in the last
  1, 5 and 15 minutes by outcome, `attention_groups` counts blocked, dead-letter and retryable
  requests by state and error code, and the attention list is filled round-robin across those
  groups so one repeated code cannot use the whole limit. `render.ledger_seconds` and
  `render.ledger_cached` show what the last read cost.
- `progress-serve` runs at most one render at a time: requests that arrive while one is
  running share its result, and a result younger than one second is served again, so any number
  of open tabs cost one render per second at most. Measure with
  `python3 tools/diagnostics/progress_benchmark.py` (synthetic fixtures only).
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
entry carries body-free `stage`, `status`, `category`, `code` and `attempts`. The inbox is never written.
Extraction and registration are read through the head of their `correction_of` chain, resolved by
the store (`store.Heads`), never by request name: a registration repaired by a correction is
`extracted` with the head's rendition and hash, a blocked, dead-lettered or still staged head is
settled and reported as itself, and an entry with no correction produces the report and ledger
effects it always did. A head must be a single stage with the entry's operation, receipt and
format, else the entry fails as `correction_mismatch` (`ambiguous_correction` for a chain with
two children); neither aborts the run. The
optional `--report` (0600) lists per-file paths, hashes and outcomes and is
private instance data. Extraction never reviews, selects or qualifies.

`qualify ... --system LABEL --actor A --policy PATH --manifest PATH
--models-config FILE --model M --context LOCATOR [--context LOCATOR ...]
[--index DB] [--concurrency N]` runs `ingest` (so there is no need to run `ingest` first, and doing
so only pays the replay twice), ensures the private standing policy and
selection manifest exist (`--context` names maintained documents under the
policy's `markdown_roots` and is required; nothing is written without it), and
runs `model_review.review` then stages `standing-review` + `standing-select`
(`standing-<submission-id>-<rendition-hash>-<policy-hash>`) for each extracted rendition that
has no saved review. A changed rendition gets a new review; an unchanged one
reuses its ledger stages with no model call. The
cohort's manifest entries are first pruned to its current renditions, then reconciled
to exactly the qualified ones, adding any that are missing. The cohort is the `--system` label: an
entry is the cohort's when its rendition was extracted by this run or its receipt's pinned
`local-path` submission sidecar names that system. Every other entry (another cohort's, an
operator's, or one whose submission cannot be proven) is left byte-identical and in place, so
several inboxes can share one catalog, manifest and index ([design](../docs/design/current-state.md#3-intake-and-review)).
Only an orphan (receipt missing from the catalog, or retained rendition file gone) is dropped. The
read-modify-write of the manifest holds the ledger writer lock, so cohorts qualified in parallel
processes keep each other's writes, and replacement is atomic. The manifest holds at most 5,000
selections across all cohorts. When the manifest has no room for an in-scope rendition that has no
saved review, `qualify` makes no model call for it: it stays `pending-review` (`selection-capacity`)
with nothing saved, and a later run with room reviews it once. A rendition whose review is saved is
selected without a call once room appears. `counts.selection_capacity` (`entries`,
`model_calls_skipped`) reports what was held back and the calls avoided; free room is read just
before each call, and `standing-select` remains the check at write time. With `--concurrency`,
reviews run ahead only while there is room for every entry in flight, so near the cap the run
proceeds more serially. To qualify a cohort larger than the free room, free space (retire entries
of other cohorts) or expect the rest to wait; they cost nothing until room appears. `counts.other_cohorts` (`selections`, `unattributed`, `orphaned`) is
present when other entries exist. Policy, manifest and context paths are validated (instance-relative, no
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
`publish --root R --policy PATH --manifest PATH [--index DB]` runs only the `standing-publish`
stage of `qualify` (same correlation scheme `publish-<policy>-<manifest-hash>-<destination>`,
guards, checks, availability and ledger evidence) for the manifest as it is now, with no ingest,
no review and no model call. `qualify` publishes once, after reconciling the manifest, so a run
that stops early leaves its saved reviews and selections unpublished and the index stale (the
index pins the manifest bytes and the review-event store state, so it stays stale from the first
review a run appends until the next publish); `publish` recovers that at the cost of one
publication instead of a re-run's whole ingest and entry walk. It is idempotent: a saved success
is reused while the index is present and fresh, otherwise a new revision is staged. Output is
body-free `counts` (`published`, `selections`) and `publication` (`status`, `available`, and the
blocking `code` when refused); exit status 0 only when published. A run's entries not yet
reviewed are not selected, so what is served is exactly the selections made so far. Qualification
is not published during a run on purpose: while reviews are still being appended an interim index
is stale again at the next review, so it could not serve progress.
**Restoring cohorts an earlier toolkit pruned.** Before the cohort-scoped reconciliation change (Kata xc2j), `qualify` for one cohort
removed every other cohort's selections from a shared manifest. The reviews are still in the
ledger and review store, so re-run `qualify` once per cohort with its own `--system`, `--inbox`
and the same policy, manifest and index; saved reviews are reused (0 model calls, 0 tokens) and
each cohort's selections return, while the others stay. After a standing-policy version bump,
requalify every cohort before expecting a publication: a selection citing an older version's
review is refused until its cohort is requalified.
**Standing scope.** The policy's `scope.receipts` and `scope.audiences` decide which renditions
`qualify` may review. A rendition outside the scope costs no model call: `qualify` reads its
`receipt_id` and `audience` from the pinned rendition (the check the adapter makes) before
scheduling a call, stages the request, and the `standing-review` adapter ends it as the blocked
`exception` `prohibited-scope` by its own unchanged scope rule. `counts.out_of_scope` reports
`entries` refused and `model_calls_skipped`. With no policy file, `qualify` generates
`scope.receipts: ["D*"]` (any upload receipt `D<number>`, audience `source`), so it covers the
cohort without a list of IDs; a policy that exists is never rewritten. A hand-written policy lists
receipt IDs (at most 5,000) or may include `"D*"`.
*Operator note: widening an existing instance's scope.* Edit the scope **and raise `version`**
in the private policy file, then re-run `qualify` once per cohort with the same `--system`,
`--inbox`, policy, manifest and index. Because the policy hash is part of every standing request, the
re-run reviews every in-scope entry again under the new version: one model call (and its tokens)
per entry, including entries already qualified under the old version, and the first review of
entries previously refused. Entries still refused cost nothing. A later re-run with the policy
unchanged costs 0 calls. Do not edit the scope without raising `version`: the new-hash reviews
then block as `conflicting-review`. Requalify every cohort afterwards, since publication refuses a
selection citing an older version. Estimate the cost first from `counts.out_of_scope.entries`
plus the already-qualified count times the sample's tokens per document.
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
sample token usage to the admitted count and bytes. Record the chosen model,
rationale and estimate privately before any bulk review (and a budget ceiling, if
the owner sets one), and actual usage after it.

**Recovering a registration blocked as `changed-extraction-identity`.** The extractor's identity
is a hash of its code (`tools/extraction/extract.py`, plus `mhtml.py` for MIME HTML, and the
instance policy) and is pinned in the stage's saved intent when the stage first prepares. A
toolkit upgrade that edits those files, between a stage's first attempt and its replay, makes
`prepare != intent`: the stage blocks as `changed-extraction-identity`, and `replay` and
`recover-conflicts` cannot clear it because replay keeps the saved pin (an example is a
registration that lost a writer race recorded as `native-input` before concurrent writers were retried, replayed by
`recover-conflicts` after the upgrade). Do not edit the ledger or stage corrections by hand. Run

    python -m tools.intake.cohort recover-identity --root ROOT           # dry run
    python -m tools.intake.cohort recover-identity --root ROOT --apply

The dry run prints body-free counts: `candidates` (chains whose head is a blocked `extract-register`
whose last attempt was `changed-extraction-identity`) and `skipped`: `prepare-mismatch` (the paired
`extract-` request is missing or does not match the receipt and format), `prepare-blocked`, or
`source-unavailable` (the source no longer prepares, for example a changed or removed original).
`--apply` stages a correction request `<root>-c<n>` of the paired `extract-prepare` (when its head
prepared under a different extractor) and of the registration (with a fresh catalog pin), and settles
both. A correction goes through the store's validation like any request, so the identity check, the
source-hash check, catalog pinning and `rebase` apply to it unchanged, and a source or extractor that
moves again blocks it again. The next `ingest` or `qualify` follows the chain to the corrected
registration and reports the entry `extracted` with no new extraction work; it is then reviewed and
selected like any other rendition. Repaired chains are not candidates again. Run it while no ingest is
writing, as with `recover-conflicts`.

Model calls made by `qualify` and `model_review evaluate` are recorded body-free (model and token
counts only, never prompts, documents or replies) as one session file per run in
`$AGENTSVIEW_DIRECT_API_DIR`, default `~/.agentsview/direct-api`, for AgentsView's Direct API agent.
A failure to write them never affects the run.
