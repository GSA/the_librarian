# The Librarian

> **Status: untested on real data.** So far it has only been tested on
> synthetic fixtures in temporary workspaces.
>
> **Ingestion is automated but operator-run.** The intake pipeline
> (`tools.intake`) stages and registers documents you supply (one file, a marked
> drop folder, or a whole read-only inbox folder) through a durable ledger that
> resumes after interruption. For an inbox folder, one command also extracts,
> reviews with a configured model against a standing policy, and selects and
> publishes what qualifies. Nothing fetches documents or runs on a schedule:
> remote connectors, scheduled discovery and return transport are still
> planned. See [Ingesting files](#ingesting-files) and
> [ROADMAP.md](ROADMAP.md#automation-pipeline).

The Librarian is a local toolkit for a program's business documents. It catalogs
the source files, can extract text from them, records reviews, and serves
qualified, source-cited context to agents through a read-only **MCP** server.

GitHub: `GSA/the_librarian`. Local checkout: `~/the_librarian`. Kata project: `the_librarian`.

The repo ships with no data. Each program uses its own **instance copy**, which
holds that program's content and config.

## Setup

Requirements: Linux, Python 3.14+, and SQLite with FTS5 support.

```sh
cd ~/the_librarian            # or an instance copy (see "Instance copies")
python3 -m venv .venv
.venv/bin/python -m pip install ./tools/context-mcp
.venv/bin/python -m tools.initialize      # creates empty stores under .local/
.venv/bin/librarian-preflight --root "$PWD"
.venv/bin/python -m unittest discover -s tests -v
```

If the host has no `ensurepip`, use `python3 -m venv --without-pip .venv` and
`python3 -m pip --python .venv/bin/python install ./tools/context-mcp`.

### Development and agent worktrees

This section is the canonical setup and troubleshooting guide for development
checkouts; [AGENTS.md](AGENTS.md) repeats only the essential commands.

Every clone or Git worktree needs its own `.venv`; the main checkout's
environment is not copied. Create the environment and install the adapter
editable (`.venv/bin/python -m pip install -e ./tools/context-mcp`) before running
tests, so they import this checkout's adapter source. Reinstall when dependency
declarations change. Tests use temporary workspaces, so development-only
checkouts do not need store initialization or preflight.

`ModuleNotFoundError: No module named 'mcp'` means the selected interpreter
lacks the MCP SDK declared in `tools/context-mcp/pyproject.toml`. Install the
adapter into that worktree's environment and run tests with
`.venv/bin/python`, not bare `python3`. If `.venv` exists but lacks `pip`, use
the host-pip fallback above without recreating the environment. Do not skip
MCP tests or install packages globally to hide the setup failure.

Every test module also runs on its own (`.venv/bin/python -m unittest tests.test_convert`).
CI is `.github/workflows/ci.yml` (`lint`, `test`, `package-smoke`, the checks `main` should require)
plus `docling.yml` (the PDF/image runtime tests) and `nightly.yml`. To reproduce the `test` job locally:

```sh
PYTHONMALLOC=pymalloc .venv/bin/python -W error::ResourceWarning -X dev \
  .github/scripts/run_tests.py --expected-skips .github/expected-skips.txt
```

A run that skips a test not listed (on a SQLite build without `sqlite_dbpage`, three file-identity tests also skip; CI's build has it, so the list stays exact there) in `.github/expected-skips.txt` fails; a provisioned Docling runtime
(`.github/scripts/provision-docling.sh`) makes the listed ones run, so use `.venv/bin/python -m unittest discover -s tests`
if you have it.

### Excluding low-value inbox files and deselecting selections

Copy [config/intake-exclusions.example.json](config/intake-exclusions.example.json) to the ignored
`config/intake-exclusions.local.json` (`classify`, `ingest` and `qualify` read it automatically;
`--exclusions FILE` names another). Each rule has an `id`, an optional `reason`, and `globs`
(matched against the whole inbox-relative path; `*` stays in one segment, `**` crosses
segments) and/or `extensions` (case-insensitive file-name endings). A matching file is reported
`excluded` with its `rule` and what it would otherwise have been; one that would have been
admitted still gets a receipt but is never extracted, sent to the model or selected. `classify`
is the dry run (`counts.excluded`). The rules do not touch the standing policy and do not
apply to an `--agent-origin` cohort (use its `exclude`).

To remove selections already in the manifest (after adding a rule, or to drop byte-identical
duplicates), dry run first, then `--apply`, then publish:

```sh
.venv/bin/python -m tools.intake.cohort deselect --root . --manifest config/selection.local.json \
  --all-rules --dedupe --report /private/plan.json            # dry run: counts, groups, entries
.venv/bin/python -m tools.intake.cohort deselect --root . --manifest config/selection.local.json \
  --all-rules --dedupe --actor "$USER" --reason "low value" --apply
.venv/bin/python -m tools.intake.cohort publish --root . --policy config/standing-policy.local.json \
  --manifest config/selection.local.json
```

Selectors are `--receipt D123`, `--receipts-file` (IDs or a dry-run `--report`), `--rule ID` /
`--all-rules` and `--dedupe` (of byte-identical selections across receipts it keeps the lowest
receipt and removes the rest; every group is reported even without `--dedupe`). Receipts and files
stay; only the selection goes, with a ledger record and a superseding review event
([context/intake.md](context/intake.md#excluding-files-and-deselecting-selections)).

## Configuration

| File | Purpose |
| --- | --- |
| `config/policy.json` | Defaults: `audience` (`authorized-local`), `handling`, `original_roots` (`sources/`), `markdown_roots` (`documents/`), exclusions, `transcript` rules, and optional relative `storage_roots` and `backup_retention_days` |
| `config/policy.local.json` | Instance policy and `storage_roots` overrides, ignored by Git; applies to this checkout only, never to another `--root` that lacks its own policy files |
| `config/selection.local.json` | Instance selection override, ignored by Git (same schema as `config/selection.json`; auto-selected when `--manifest` is omitted) |
| `config/intake-exclusions.example.json` | Template for the ignored `config/intake-exclusions.local.json`: path-glob and extension rules that keep low-value inbox files (dependency trees, VCS internals, build caches) out of model review (`cohort ... --exclusions`, [below](#excluding-low-value-inbox-files-and-deselecting-selections)) |
| `config/agent-origin.example.json` | Template for an ignored `config/*.local.json` origin file declaring an agent-written cohort (`cohort ... --agent-origin`) |

Default stores (configurable via `storage_roots` and bound in `.local/instance.json`):
`.local/catalog` (catalog metadata), `.local/review-events` (append-only reviews),
`tools/context_index/.local/index.sqlite3` (rebuildable FTS5 search index),
`data/extractions/.local` (extracted renditions), `.local/intake` (optional intake
recovery ledger), and `.local/diagnostics/retrieval.jsonl` (default path for the
diagnostics CLI; MCP retrieval logging is opt-in via `--diagnostics-log`). Each
catalog `submit`, review-event `append` and claim-correction `record`/`resolve`/`intake` takes a verified snapshot before the
write only when the store has none for the current UTC day, then deletes
snapshots older than seven days (set `backup_retention_days` in
`config/policy.local.json` to change). `python3 -m tools.backups status` reports
backup count and size of each store (catalog, review events, claim corrections once initialized);
`python3 -m tools.backups prune` is a dry run (`--store catalog|review-events|claim-corrections|all`) and
`--apply` keeps the newest verified snapshot per day. Retention only counts snapshots whose history
was replayed, so a corruption copied into new daily snapshots never ages out the last clean one. See the
[current-state design](docs/design/current-state.md) (backups).
Ingest cost still grows with history; measure it on synthetic cohorts with
`python3 tools/diagnostics/ingest_benchmark.py --sizes 100,200 --resume` and see the
[current-state design](docs/design/current-state.md) (catalog validation, writers and cost). Catalog writes
validate only their delta when the database file is unchanged since a prior full
validation, judged by file identity with a full check each UTC day
(trust model and residual in the design doc); profile the
lock with `python3 tools/diagnostics/catalog_lock_benchmark.py`, and a production-sized
ledger and catalog with `python3 tools/diagnostics/ledger_scale_benchmark.py` (per-file planning cost with `upload_planning_benchmark.py`). The
throughput target and its derivation are in
[context/intake.md](context/intake.md#throughput-target-and-measurement).
The first write of each UTC day replays the history once for its daily snapshot
(profile it with
`tools/diagnostics/catalog_daily_replay_benchmark.py`).

Retrieval quality is measured per instance against ignored local cases
(`config/evals.local.json`). Capture a baseline before a policy change, rereview
or publish, and compare after it:

```sh
python -m tools.evals --root ROOT generate --seed 1 --count 200 --absences 10
python -m tools.evals --root ROOT run   # baseline; scorecard under ROOT/tools/evals/.local/runs/
# ...change policy, rereview, publish...
python -m tools.evals --root ROOT run   # after
python -m tools.evals compare BASELINE.json NEW.json
```

Case format, metrics and procedure are in
[context/retrieval-evals.md](context/retrieval-evals.md).

## Ingesting files

Every path below is started by an operator. The intake commands record each
step in a durable ledger so a run can be retried, resumed or replayed; nothing
watches folders or runs on a schedule.

| Input | Command | Then |
| --- | --- | --- |
| One file | `tools.intake.cli submit-file` | Steps 4–7 of [registering by hand](#registering-a-file-by-hand) (extract, write, select, rebuild) |
| A marked drop folder | `tools.intake.cli drop-scan` / `drop-submit` | As for one file |
| A read-only inbox folder (text, Gemini TXT, MHTML, ODT; PDF and images once the Docling runtime is provisioned) | `tools.intake.cohort qualify` | Nothing: it ingests, extracts, reviews, selects and publishes what qualifies; the rest stay visibly pending |
| A tree of agent-written context | `tools.intake.cohort qualify --agent-origin FILE` | As for an inbox; everything it publishes is low confidence, labelled `agent-generated` ([below](#agent-written-context-an-old-context-tree)) |
| One PDF or image | `submit-file`, then Docling stages | See [tools/extraction/README.md](tools/extraction/README.md) |

### One file or a drop folder

To stage and register a file in one step, use the upload intake (see
[context/intake.md](context/intake.md#manual-upload-and-local-drop)):

```sh
.venv/bin/python -m tools.intake.cli --root . initialize   # once per instance
.venv/bin/python -m tools.intake.cli --root . submit-file ~/memo.pdf \
  --submission-id memo-001 --format pdf --actor "$USER" --reason "Register memo"
```

It replaces steps 1–3 of [registering by hand](#registering-a-file-by-hand)
and leaves extraction to step 4. `drop-submit --drop DIR` does the same for each
file in a folder marked with `.librarian-drop` that has a
`NAME.submission.json` descriptor beside it (`drop-scan` only lists them).

### A read-only inbox folder

To take in a whole read-only folder, classify it and then ingest the admitted
files through upload and extraction (see
[context/intake.md](context/intake.md#read-only-cohort-intake)):

```sh
.venv/bin/python -m tools.intake.cohort classify --root . --inbox /path/to/inbox
.venv/bin/python -m tools.intake.cohort ingest --root . --inbox /path/to/inbox \
  --system my-inbox --actor "$USER" --report .local/cohort-report.json
```

Every file gets a counted disposition; nothing in the inbox is changed. Archives are
`needs-reader` until `cohort expand-archives --apply` expands their members into instance
storage; `delete-archive-original` removes an original only after every member is verified
and registered ([context/intake.md](context/intake.md#archives)). Ingest commits
catalog registrations in chunks of `--batch-size` documents (default 50) as one revision each;
`--batch-size 1` keeps one revision per registration, and a re-run after any interruption completes the rest.
Reading and parsing text files runs in `--parse-workers` processes (default: CPUs, at most 4); `1` keeps one process.
Extracted renditions stay unselected until a standing-policy review qualifies
them. If a registration reports `register/changed-extraction-identity` (the
extractor changed after it was first prepared), `python -m tools.intake.cohort
recover-identity --root .` lists the recoverable ones and `--apply` repairs
them through validated correction requests
([context/intake.md](context/intake.md#read-only-cohort-intake)). Extractions blocked by a
toolkit defect that has since been fixed (for example `extract-mutation`) are restaged for the next
`ingest` with `recover-blocked-extractions --root . --code CODE` (`--apply`). An accepted review
that reports contradictions or missing evidence now qualifies with those issues attached;
`recover-unresolved-evidence --root .` (`--apply`) qualifies earlier reviews blocked as
`unresolved-evidence` from their saved findings without a model call.
`rereview --root . --policy ... --manifest ... --model MODEL` lists standing reviews that need a
new model review (older policy, insufficient inspected scope) with an estimated cost; add
`--apply --models-config FILE` (and `--limit N`) to re-review them as corrections, then `publish`.

To review, select and publish the extracted cohort in one rerunnable step
(model traffic stays within the configured USAi provider). `qualify` runs `ingest` itself,
so do not run `ingest` first: an unchanged, already received inbox is replayed (each file is
re-read, re-hashed and checked against the ledger) before the first review either way:

```sh
.venv/bin/python -m tools.intake.cohort qualify --root . --inbox /path/to/inbox \
  --system my-inbox --actor "$USER" --policy config/standing-policy.local.json \
  --manifest config/selection.local.json --models-config /path/to/models.json \
  --model MODEL --context documents/CONTEXT.md
```

A replay of an unchanged inbox costs about 5 milliseconds per file, so rerunning or resuming
`qualify` on a large cohort starts reviewing after minutes, not hours; measure it with
`tools/diagnostics/ingest_benchmark.py --replay` ([current-state design](docs/design/current-state.md#3-intake-and-review)).

A rendition outside the standing policy's scope costs no model call (`counts.out_of_scope`); with no policy file, the generated scope is every upload receipt (`"D*"`), and an existing policy is never rewritten (see `context/intake.md`).

It prints body-free outcome counts, token usage and transport retry counts;
incomplete reviews stay visibly `pending-review` and are not indexed. Up to
`--concurrency N` (default 6; `1` is serial) is the most model calls run at once,
while all ledger writes stay serial and in inbox order. Concurrency adapts: it halves on a 429
or 503 and recovers one step per clean window, never above N, and the output reports the
effective rate (`throughput`); `--no-adaptive` holds it fixed. `--inspected-form ranges` asks
for `inspected_records` as ranges, which saves output tokens
(`tools.diagnostics.inspection_form_ab` measures it on a sample; see `context/intake.md`). Rate limits (429), transient
server errors (500/502/503/504) and network errors are retried with backoff,
honouring `Retry-After`.

A `qualify` that stops early (interrupted, host restart) keeps every saved review and
selection, but publishes only at the end of a run, so the served index goes stale. Publish what
the manifest holds now, without ingesting or reviewing anything and without a model call:

```sh
.venv/bin/python -m tools.intake.cohort publish --root . \
  --policy config/standing-policy.local.json --manifest config/selection.local.json
```

It runs the same `standing-publish` stage as the end of `qualify` (same checks, same ledger
evidence), is idempotent (an unchanged manifest with a fresh index is reused), prints body-free
`counts` (`published`, `selections`) and a `publication` status, and exits 1 when nothing was
published (for example `standing-selection-unauthorized` after a standing-policy version bump
that has not been requalified). Run `qualify` again afterwards to resume reviewing.

### Agent-written context (an old context tree)

Context an AI agent wrote (for example the Markdown tree an earlier version of the Librarian
maintained) is ingested as its own cohort with a declared origin, so it is reviewed like any
other cohort but always indexed as low confidence (`agent-generated`) and labelled with its
generator, date and source commit. Put the origin in an ignored `config/*.local.json` (see
[config/agent-origin.example.json](config/agent-origin.example.json)): `generator`, optional
`source_repository` label, `provenance` (`git` reads each file's last commit and author date
from the inbox's git work tree, read-only; `none`), `include` paths (default the whole inbox) and
`exclude` paths (reported as `excluded`). Pass it to `classify` (a dry run that also reports
formats, bytes and how many files are committed or uncommitted), `ingest` or `qualify`:

```sh
.venv/bin/python -m tools.intake.cohort classify --root . --inbox /path/to/old-context \
  --agent-origin config/old-context.local.json
.venv/bin/python -m tools.intake.cohort qualify --root . --inbox /path/to/old-context \
  --agent-origin config/old-context.local.json --system old-context --actor "$USER" \
  --policy config/standing-policy.local.json --manifest config/selection.local.json \
  --models-config /path/to/models.json --model MODEL --context documents/CONTEXT.md
```

Search hits from it carry `origin: "agent-generated (Generated by ... on DATE from
REPO@COMMIT:PATH)"`, and `get` pieces carry the same detail in their confidence label; MCP
`search(low_confidence="last"|"exclude")` ranks them last or leaves them out. Use a `--system`
label of its own, and keep the origin file unchanged once ingested (see
[context/intake.md](context/intake.md#agent-written-context)).

### Watching a run

To watch a run, use the read-only, body-free progress view (CLI or a
loopback-only page that updates when reloaded; see
[context/intake.md](context/intake.md#progress-view)):

```sh
.venv/bin/python -m tools.intake.cli --root . progress --format text
.venv/bin/python -m tools.intake.cli --root . progress-serve   # http://127.0.0.1:8765/
```

### Registering a file by hand

Hand registration is still supported, for example for a format or record shape
the intake commands do not cover.

1. **Drop** the original file under `sources/` (for example `sources/memo.pdf`).
2. **Discover** what has not been registered yet:
   ```sh
   .venv/bin/python -m tools.catalog.inventory sources
   ```
3. **Register** the file in the catalog. Write a records file with a source
   (`S*`), a receipt (`D*`), and an artifact (`A-*`) that includes the file's
   `path`, `sha256`, and `bytes`. See `WorkspaceCase.registration()` in `tests/support.py`
   for a working example. Then submit it:
   ```sh
   TOKEN=$(.venv/bin/python -m tools.catalog.catalog_store status | python3 -c 'import json,sys;print(json.load(sys.stdin)["state_token"])')
   .venv/bin/python -m tools.catalog.catalog_store submit records.json --actor "$USER" \
     --request-id intake-001 --expected-state "$TOKEN" --reason "Register memo"
   ```
4. **Extract** text (optional):
   - For UTF-8 text, Gemini transcripts, MHTML, or ODT (`--format {text,gemini-txt,mhtml,odt}`):
     `.venv/bin/python -m tools.extraction.extract {prepare,verify,register} D1 --format odt ...`
   - For PDF or images (`--format {pdf,image}`): use Docling (`{inspect,prepare,verify,register}`).
     See [tools/extraction/README.md](tools/extraction/README.md). Docling needs its
     own environment and pinned models. A Docling result can be selected for
     retrieval only after a recorded review of that exact output.
5. **Write** the reviewed knowledge as Markdown under `documents/`, citing the
   receipt. Record reviews with `python -m tools.review_events.event_store append` if needed.
6. **Select** the document (and up to 10 registered `renditions`) in `config/selection.local.json`:
   ```json
   {"schema_version": 1, "audience": "authorized-local",
    "documents": [{"path": "documents/memo.md", "role": "business-rule", "context": []}],
    "context_rules": {}, "renditions": []}
   ```
7. **Rebuild** the index. Do this after every change to content or selection
   (`config/selection.local.json` is used automatically when present):
   ```sh
   .venv/bin/librarian-preflight --root "$PWD" \
     --manifest config/selection.local.json --refresh-index
   ```

Registering a file does not make it retrievable. Only documents and renditions
listed in the selection file are indexed.

**Staged jobs.** `python -m tools.intake.cli stage` and `run` can perform any of
these steps (catalog registration, extraction including Docling, review
recording, and the index rebuild) as a staged job that can be retried, resumed,
or replayed. You still supply every file and record. See
[context/intake.md](context/intake.md).

## Interfaces

Run these from the clone root. Each supports `--help`.

| Command | Purpose |
| --- | --- |
| `python -m tools.initialize` | Initialize stores/index, or `--migrate-storage`, `--recover-migration`, `--relocate`, `--migrate-catalog-indexes` |
| `python -m tools.catalog.catalog_store` | Catalog: `init`, `status`, `audit`, `submit`, `query`, `export`, `backup`, `verify-backup`, `restore`, `migrate-indexes` |
| `python -m tools.catalog.inventory` | List files under a configured root (`--root`, `--limit`, `directory`; metadata only) |
| `python -m tools.extraction.extract` | Text, Gemini transcript, and ODT extraction (`prepare`, `verify`, `register`) |
| `python -m tools.extraction.docling_extract` | Optional PDF and image extraction (`inspect`, `prepare`, `verify`, `register`) |
| `python -m tools.retirement` | Read-only retirement manifest of a legacy tree joined to catalog receipts (`manifest`) and byte check of a restored copy (`verify`); see [context/retirement.md](context/retirement.md) |
| `python -m tools.store_audit` | Scheduled full verification of the catalog, review events, claim corrections (when initialized), intake ledger and newest backups; JSON report, exit 1 if any store is flagged (cron/systemd timer; see the design doc) |
| `python -m tools.review_events.event_store` | Review store: `init`, `status`, `audit`, `schema`, `get`, `append`, `backup`, `verify-backup`, `restore` |
| `python -m tools.review_events.event_reader` | Query (`query`) and export (`export`) reviews |
| `python -m tools.review_events.corrections` | Claim corrections: `init`, `status`, `list`, `record` (correct/dispute a quoted claim), `resolve` (confirm/retract), `intake` (a `claim-corrections-v1` file), `backup`, `verify-backup`, `restore`; `tools.backups status`/`prune` cover its snapshots |
| `python -m tools.context_index.cli` | Derived index: `build`, `status`, `search`, `get`, `get-many`, `catalog` |
| `python -m tools.context_index.integrity` | Read-only integrity audit of registered files against their ingest hashes: `audit`, `check PATH...` (exit 1 on a mismatch) |
| `python -m tools.intake.cli` | Durable intake: `initialize`, `stage`, `submit-file`, `drop-scan`, `drop-submit`, `list`, `dead-letter`, `inspect`, `run`, `resume`, `replay`, `recover-conflicts`, `acknowledge`, `audit`, `progress`, `progress-serve` |
| `python -m tools.intake.cohort` | Read-only inbox intake: `classify`, `ingest`, `qualify`, `publish`, `recover-identity`, `recover-blocked-extractions`, `recover-unresolved-evidence`, `recover-deferred`, `rereview`, `backfill-docs`, `audit-docs`, `docs-status`, `expand-archives`, `delete-archive-original` |
| `python -m tools.diagnostics.retrieval_log` | Opt-in body-free retrieval diagnostics (`report [--since-days N]`, `prune --keep N`) |
| `python -m tools.evals` | Retrieval-quality evaluation: `generate` seeded cases, `run` them against a fresh index into a body-free scorecard, `compare BASE NEW` (exit 1 on a regression) |
| `python -m tools.schemas.export_schemas` | Generate or `--check` SQLite schema snapshots in `tools/schemas/` |
| `python -m tools.package` | Package allowlisted toolkit files (`--output`) |
| `librarian-preflight` | Check prerequisites; `--refresh-index` rebuilds the index; `--verify-start` starts a fresh MCP server and calls `status` and `search` |
| `librarian-mcp` | Read-only stdio MCP server |
| `librarian-audit` | Body-free `report` of the opt-in MCP audit log |

## Integrity audit and the no-hand-edit rule

Files in an instance's data and source roots (`sources/`, the extractions home and everything else the
catalog registers) are written only by intake. **Never edit, replace, move or delete them by hand**; to
change a document, submit it through intake. Readers prove the index fresh from the hashes the catalog
recorded at ingest and do not reopen those files, so a hand edit is not noticed by a read. It is found by
the read-only audit, which re-hashes every registered file against its recorded hash and reports each
difference (path, kind, expected and found hash) without changing anything or failing any read:

```sh
.venv/bin/python -m tools.context_index.integrity --root "$PWD" audit            # all files (JSON report)
.venv/bin/python -m tools.context_index.integrity --root "$PWD" check sources/memo.pdf   # named files
.venv/bin/python -m tools.context_index.integrity --root "$PWD" audit --record   # also flag affected documents
```

With `--record` the audit also writes its result beside the index, so reads keep serving but mark each
document whose retained file changed (`integrity-changed`, low confidence), and `status` and the progress
page list them with how to resolve. Schedule `audit --record` rather than plain `audit`; a rebuild clears
the flags until the next audit. A build refuses to publish while a selected file differs from its recorded
hash, and names every such file.

Exit status: 0 clean, 1 mismatches found, 2 the audit could not run. **Run the audit on a schedule**, for
example nightly, and alert on a non-zero status. With cron (`crontab -e`):

```cron
17 3 * * *  cd /path/to/instance && .venv/bin/python -m tools.context_index.integrity --root . audit --record > .local/integrity-audit.json || mail -s "Librarian integrity audit: $?" owner@example.org < .local/integrity-audit.json
```

With a systemd user timer (`~/.config/systemd/user/librarian-audit.{service,timer}`, then
`systemctl --user enable --now librarian-audit.timer`):

```ini
# librarian-audit.service
[Service]
Type=oneshot
WorkingDirectory=/path/to/instance
ExecStart=/path/to/instance/.venv/bin/python -m tools.context_index.integrity --root . audit --record
# librarian-audit.timer
[Timer]
OnCalendar=*-*-* 03:17:00
Persistent=true
[Install]
WantedBy=timers.target
```

A reported file is restored from backup or re-ingested; the audit repairs nothing. See
[context/integrity.md](context/integrity.md).

### Store audit

Reads and writes trust the stores' read-validation receipts and never replay a store's whole history
(there is no daily full check), so a store edited outside the application is found by the store audit,
which replays the catalog, the review events, the claim corrections (when initialized) and the intake
ledger in full, verifies the newest backup of each of the first three, and prints a body-free JSON report (exit 0 all verified, 1 a store
was flagged, 2 it could not run):

```sh
.venv/bin/python -m tools.store_audit --root "$PWD"
```

Schedule it with the file audit above, for example `23 3 * * *  cd /path/to/instance && .venv/bin/python
-m tools.store_audit --root . > .local/store-audit.json || mail -s "Librarian store audit" owner@example.org
< .local/store-audit.json`. Details and the tamper classes each check catches:
[design](docs/design/current-state.md#store-validation-cadence-kata-91h5).

## MCP for consumer agents

`librarian-mcp` is a read-only stdio MCP server. `--root` must be an absolute
path to the full instance copy; `--manifest`, `--db`, and `--diagnostics-log`
stay within `--root` (`--manifest` defaults to `config/selection.local.json` when
present, otherwise `config/selection.json`). Both `librarian-mcp` and
`librarian-preflight` also accept `--max-source-chars` (30000),
`--max-output-chars` (60000), `--max-results` (25, max 50),
`--timeout-seconds` (30), and `--diagnostics-log <path>.jsonl`.

Exposed MCP tools:
- `status()` — check selected index freshness and document counts by confidence level
- `search(query, limit?, path?, low_confidence?)` — keyword discovery (default `limit` 5); `low_confidence` is `"include"` (default, rank order), `"last"` or `"exclude"` (counted in `low_confidence_excluded`); agent-written hits also carry `origin: "agent-generated (...)"`
- `get(locator, format?, max_chars?, max_output_chars?)` — read exact qualified text (`format`: `"full"` or `"compact"`)
- `get_many(locators, format?, max_chars?, max_output_chars?)` — read 1–10 qualified packets (`format` default `"full"`)
- `catalog(receipt?, source?, path?, kind?, coverage?, audience?, after?, expected_state?, limit?, format?, max_output_chars?)` — query live catalog metadata
- `review_events(receipt?, source?, scope?, outcome?, content?, unit?, origin?, include_history?, after?, expected_state?, limit?, max_output_chars?)` — query stored review events (requires `receipt`, `source`, or `scope`)

```sh
.venv/bin/librarian-mcp --root "$PWD" --manifest "$PWD/config/selection.local.json"
```

Pi (`.local/environment/pi/mcp.json` in The Engineer) or any MCP client:

```json
{"mcpServers": {"the-librarian": {
  "command": "/abs/instance/.venv/bin/librarian-mcp",
  "args": ["--root", "/abs/instance",
           "--manifest", "/abs/instance/config/selection.local.json"],
  "timeout": 60}}}
```

Add `--audit-log --agent-label <agent>` to a binding to keep a private,
full-text record of every call: the arguments and the exact text returned, in
`.local/audit/<agent>-mcp-access.jsonl` (rotated at 64 MiB, never deleted by the
toolkit). It contains corpus text and agent queries, so treat it at the
instance's audience. If a record cannot be written the call fails instead of
returning unrecorded text. `.venv/bin/librarian-audit --root "$PWD"
report` summarizes it without bodies. See the
[current-state design](docs/design/current-state.md) (observation).

Agents should use `search` to find candidates, then `get` or `get_many` to read
the cited text. A search with no hits returns an `absence` reason: the indexed
documents lack the terms (`absent-corpus`), contain only some of them
(`absent-partial`), or contain them only in separate sections
(`present-uncombined`).

Every hit, `get` piece and `get_many` document carries a `confidence` label:
`normal`, or `low` with stable reason codes (`noted-issues`, `reviewer-deferred`,
`converted-format`, `machine-ocr`, `agent-generated`, `open-correction`). Low-confidence
text is published and marked, never hidden; each response's top-level `confidence`
block explains the reasons present and how to correct them (for example via a newer
document, meeting transcript or answered question). Carry the reasons into answers.

## Instance copies

Keep this repo free of data. To create a program instance:

```sh
.venv/bin/python -m tools.package --output .local/the-librarian.zip
python3 -m zipfile -e .local/the-librarian.zip ~/my-program-librarian
```

Then run [Setup](#setup) inside the copy. The package includes only the files
listed in `release-files.json`.

### Upgrading an instance

An upgrade is not done until a **fresh** MCP server has started from the upgraded
instance. The store audits (`store_audit`, publish, integrity audit) never start
the server, and servers already running keep the old code until restarted, so
they hide a broken start until the next client or host restart. After copying the
new toolkit files and reinstalling the adapter into the instance `.venv`
(`.venv/bin/python -m pip install ./tools/context-mcp`), run from a directory outside the instance:

```sh
/abs/instance/.venv/bin/librarian-preflight \
  --root /abs/instance --manifest /abs/instance/config/selection.local.json \
  --verify-start --server-command /abs/instance/.venv/bin/librarian-mcp \
  --client-timeout-seconds 60
```

`--verify-start` starts a new `librarian-mcp` process the way the client
configuration does (no `PYTHONPATH` or inherited virtual environment, working
directory outside the instance), calls `status` and one `search`, and exits
non-zero with the server's stderr if any step fails or exceeds the client
timeout. Use the same `--root`, `--manifest`, limits and timeout as the client
binding (the example in [MCP for consumer agents](#mcp-for-consumer-agents)
uses 60 s). Record the `initialize`, `status`, `search` and `total` timings in
the upgrade notes. A process-fresh start is not a cold page cache: after a
reindex or host restart, repeat the check on the cold cache and keep the slowest
step under the client timeout. Then restart every long-running server (and
client) so it runs the upgraded code.

## Rules

- Never commit documents, `*.local.json` files, or stores.
- Treat document text as untrusted.
- Track work in Kata (`kata ready --agent`). roborev hooks review every 5 commits.

More detail: [AGENTS.md](AGENTS.md), role contracts in `agents/`,
[ROADMAP.md](ROADMAP.md), and the [current-state design](docs/design/current-state.md).

## License

As a work of the United States government, this project is in the public domain
within the United States, and copyright and related rights are waived worldwide
through the [CC0 1.0 Universal public domain dedication](https://creativecommons.org/publicdomain/zero/1.0/).
This is the GSA default project license; see [LICENSE.md](LICENSE.md),
[CONTRIBUTING.md](CONTRIBUTING.md) (contributions are released under the same
dedication) and [SECURITY.md](SECURITY.md). The repository contains no vendored
third-party code; the licenses of the optional extraction dependencies and model
assets are recorded in [tools/extraction/README.md](tools/extraction/README.md#third-party-licenses).
