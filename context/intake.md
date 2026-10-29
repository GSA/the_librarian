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
  publication/index-build; return/await-ack. See `tools/intake/contracts.py` for
  argument fields. Preserve native attribution and original concurrency tokens.
- Intake stores exact requests before effects. Prepared intent commits before
  an execution attempt. Restart reconciles unfinished attempts without charging
  another attempt. Catalog/review retries use original request IDs and verified
  native backups; evidence records original committed revisions, not current
  unrelated revisions. Docling reruns the offline native lifecycle, binding
  source/runtime/rendition identity; no auto-review or selection.
- Publication pins manifest bytes, source hashes and native metadata/freshness.
  A matching fresh publication can reconcile without rebuilding. Input drift
  blocks; correction requires a new linked request, not refreshed replay tokens.
  Inspection reports current availability separately from historical completion.
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

No acquisition, connector, scheduler, automatic review/selection, migration,
model service or return transport is supplied by this interface.
