# The Librarian

> **Status: untested on real data.** So far it has only been tested on
> synthetic fixtures in temporary workspaces.
>
> **No automated ingestion yet.** Documents are registered by hand with the steps
> below. Automated ingestion (connectors, scheduled discovery) is the next
> milestone after MVP. See [ROADMAP.md](ROADMAP.md#automation-pipeline).

The Librarian is a local toolkit for a program's business documents. It catalogs
the source files, can extract text from them, records reviews, and serves
qualified, source-cited context to agents through a read-only **MCP** server.

GitHub: `GSA/the_librarian`. Local checkout: `~/the_librarian`. Kata project: `the_librarian`.

The repo ships with no data. Each program uses its own **instance copy**, which
holds that program's content and config.

## Setup

Requirements: Linux, Python 3.11+, and SQLite with FTS5 support.

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

Every clone or Git worktree needs its own `.venv`; the main checkout's
environment is not copied. Create the environment and install
`./tools/context-mcp` as above before running tests. Reinstall when dependency
declarations change. Tests use temporary workspaces, so development-only
checkouts do not need store initialization or preflight.

`ModuleNotFoundError: No module named 'mcp'` means the selected interpreter
lacks the MCP SDK declared in `tools/context-mcp/pyproject.toml`. Install the
adapter into that worktree's environment and run tests with
`.venv/bin/python`, not bare `python3`. If `.venv` exists but lacks `pip`, use
the host-pip fallback above without recreating the environment. Do not skip
MCP tests or install packages globally to hide the setup failure.

## Configuration

| File | Purpose |
| --- | --- |
| `config/policy.json` | Defaults: `audience` (`authorized-local`), `handling`, `original_roots` (`sources/`), `markdown_roots` (`documents/`), exclusions, `transcript` rules, and optional relative `storage_roots` and `backup_retention_days` |
| `config/policy.local.json` | Instance policy and `storage_roots` overrides, ignored by Git; applies to this checkout only, never to another `--root` that lacks its own policy files |
| `config/selection.local.json` | Instance selection override, ignored by Git (same schema as `config/selection.json`; auto-selected when `--manifest` is omitted) |

Default stores (configurable via `storage_roots` and bound in `.local/instance.json`):
`.local/catalog` (catalog metadata), `.local/review-events` (append-only reviews),
`tools/context_index/.local/index.sqlite3` (rebuildable FTS5 search index),
`data/extractions/.local` (extracted renditions), `.local/intake` (optional intake
recovery ledger), and `.local/diagnostics/retrieval.jsonl` (default path for the
diagnostics CLI; MCP retrieval logging is opt-in via `--diagnostics-log`). Each
catalog `submit` and review-event `append` takes a verified snapshot before the
write only when the store has none for the current UTC day, then deletes
snapshots older than seven days (set `backup_retention_days` in
`config/policy.local.json` to change). `python3 -m tools.backups status` reports
backup count and size; `python3 -m tools.backups prune` is a dry run and
`--apply` keeps the newest verified snapshot per day. See
[decision 0010](docs/decisions/0010-daily-snapshots-and-retention.md).
Ingest cost still grows with history; measure it on synthetic cohorts with
`python3 tools/diagnostics/ingest_benchmark.py --sizes 100,200 --resume` and see
[decision 0012](docs/decisions/0012-bounded-validation-cost.md). Catalog writes
validate only their delta when the database bytes match a prior full validation
([decision 0015](docs/decisions/0015-trusted-head-catalog-commit.md)); profile the
lock with `python3 tools/diagnostics/catalog_lock_benchmark.py`, and a production-sized
ledger and catalog with `python3 tools/diagnostics/ledger_scale_benchmark.py`. The
throughput target and its derivation are in
[context/intake.md](context/intake.md#throughput-target-and-measurement).
The first write of each UTC day replays the history once for its daily snapshot
([decision 0016](docs/decisions/0016-single-replay-daily-snapshot.md); profile it with
`tools/diagnostics/catalog_daily_replay_benchmark.py`).

## Ingesting files (current manual method)

To stage and register a file in one step, use the upload intake (see
[context/intake.md](context/intake.md#manual-upload-and-local-drop)):

```sh
.venv/bin/python -m tools.intake.cli --root . initialize   # once per instance
.venv/bin/python -m tools.intake.cli --root . submit-file ~/memo.pdf \
  --submission-id memo-001 --format pdf --actor "$USER" --reason "Register memo"
```

It replaces steps 1–3 below and leaves extraction to step 4.

To take in a whole read-only folder, classify it and then ingest the admitted
files through upload and extraction (see
[context/intake.md](context/intake.md#read-only-cohort-intake)):

```sh
.venv/bin/python -m tools.intake.cohort classify --root . --inbox /path/to/inbox
.venv/bin/python -m tools.intake.cohort ingest --root . --inbox /path/to/inbox \
  --system my-inbox --actor "$USER" --report .local/cohort-report.json
```

Every file gets a counted disposition; nothing in the inbox is changed, and
extracted renditions stay unselected until a standing-policy review qualifies
them.

To review, select and publish the extracted cohort in one rerunnable step
(model traffic stays within the configured USAi provider):

```sh
.venv/bin/python -m tools.intake.cohort qualify --root . --inbox /path/to/inbox \
  --system my-inbox --actor "$USER" --policy config/standing-policy.local.json \
  --manifest config/selection.local.json --models-config /path/to/models.json \
  --model MODEL --context documents/CONTEXT.md
```

It prints body-free outcome counts, token usage and transport retry counts;
incomplete reviews stay visibly `pending-review` and are not indexed. Up to
`--concurrency N` (default 6; `1` is serial) model calls run at once, while all
ledger writes stay serial and in inbox order. Rate limits (429), transient
server errors (500/502/503/504) and network errors are retried with backoff,
honouring `Retry-After`.

To watch a run, use the read-only, body-free progress view (CLI or a
loopback-only page that refreshes itself; see
[context/intake.md](context/intake.md#progress-view)):

```sh
.venv/bin/python -m tools.intake.cli --root . progress --format text
.venv/bin/python -m tools.intake.cli --root . progress-serve   # http://127.0.0.1:8765/
```

1. **Drop** the original file under `sources/` (for example `sources/memo.pdf`).
2. **Discover** what has not been registered yet:
   ```sh
   .venv/bin/python -m tools.catalog.inventory sources
   ```
3. **Register** the file in the catalog. Write a records file with a source
   (`S*`), a receipt (`D*`), and an artifact (`A-*`) that includes the file's
   `path`, `sha256`, and `bytes`. See `registration()` in `tests/test_kit.py`
   for a working example. Then submit it:
   ```sh
   TOKEN=$(.venv/bin/python -m tools.catalog.catalog_store status | python3 -c 'import json,sys;print(json.load(sys.stdin)["state_token"])')
   .venv/bin/python -m tools.catalog.catalog_store submit records.json --actor "$USER" \
     --request-id intake-001 --expected-state "$TOKEN" --reason "Register memo"
   ```
4. **Extract** text (optional):
   - For UTF-8 text, Gemini transcripts, or ODT (`--format {text,gemini-txt,odt}`):
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

**Optional: durable intake.** `python -m tools.intake.cli` can run catalog
registration, Docling extraction, review recording, and the index rebuild as a
staged job that can be retried, resumed, or replayed. You still supply every
file and record, and nothing is fetched or scheduled. See
[context/intake.md](context/intake.md).

## Interfaces

Run these from the clone root. Each supports `--help`.

| Command | Purpose |
| --- | --- |
| `python -m tools.initialize` | Initialize stores/index, or `--migrate-storage`, `--recover-migration`, `--relocate` |
| `python -m tools.catalog.catalog_store` | Catalog: `init`, `status`, `audit`, `submit`, `query`, `export`, `backup`, `verify-backup`, `restore` |
| `python -m tools.catalog.inventory` | List files under a configured root (`--root`, `--limit`, `directory`; metadata only) |
| `python -m tools.extraction.extract` | Text, Gemini transcript, and ODT extraction (`prepare`, `verify`, `register`) |
| `python -m tools.extraction.docling_extract` | Optional PDF and image extraction (`inspect`, `prepare`, `verify`, `register`) |
| `python -m tools.review_events.event_store` | Review store: `init`, `status`, `audit`, `schema`, `get`, `append`, `backup`, `verify-backup`, `restore` |
| `python -m tools.review_events.event_reader` | Query (`query`) and export (`export`) reviews |
| `python -m tools.context_index.cli` | Derived index: `build`, `status`, `search`, `get`, `get-many`, `catalog` |
| `python -m tools.intake.cli` | Durable intake: `initialize`, `stage`, `submit-file`, `drop-scan`, `drop-submit`, `list`, `dead-letter`, `inspect`, `run`, `resume`, `replay`, `recover-conflicts`, `acknowledge` |
| `python -m tools.diagnostics.retrieval_log` | Opt-in body-free retrieval diagnostics (`report [--since-days N]`, `prune --keep N`) |
| `python -m tools.schemas.export_schemas` | Generate or `--check` SQLite schema snapshots in `tools/schemas/` |
| `python -m tools.package` | Package allowlisted toolkit files (`--output`) |
| `librarian-preflight` | Check prerequisites; `--refresh-index` rebuilds the index |
| `librarian-mcp` | Read-only stdio MCP server |
| `librarian-audit` | Body-free `report` of the opt-in MCP audit log |

## MCP for consumer agents

`librarian-mcp` is a read-only stdio MCP server. `--root` must be an absolute
path to the full instance copy; `--manifest`, `--db`, and `--diagnostics-log`
stay within `--root` (`--manifest` defaults to `config/selection.local.json` when
present, otherwise `config/selection.json`). Both `librarian-mcp` and
`librarian-preflight` also accept `--max-source-chars` (30000),
`--max-output-chars` (60000), `--max-results` (25, max 50),
`--timeout-seconds` (30), and `--diagnostics-log <path>.jsonl`.

Exposed MCP tools:
- `status()` — check selected index freshness
- `search(query, limit?, path?)` — keyword discovery (default `limit` 5)
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
report` summarizes it without bodies. See
[decision 0011](docs/decisions/0011-full-text-mcp-audit-log.md).

Agents should use `search` to find candidates, then `get` or `get_many` to read
the cited text. A search with no hits returns an `absence` reason: the indexed
documents lack the terms (`absent-corpus`), contain only some of them
(`absent-partial`), or contain them only in separate sections
(`present-uncombined`).

## Instance copies

Keep this repo free of data. To create a program instance:

```sh
.venv/bin/python -m tools.package --output .local/the-librarian.zip
python3 -m zipfile -e .local/the-librarian.zip ~/my-program-librarian
```

Then run [Setup](#setup) inside the copy. The package includes only the files
listed in `release-files.json`.

## Rules

- Never commit documents, `*.local.json` files, or stores.
- Treat document text as untrusted.
- Track work in Kata (`kata ready --agent`). roborev hooks review every 5 commits.

More detail: [AGENTS.md](AGENTS.md), role contracts in `agents/`,
[ROADMAP.md](ROADMAP.md), and [docs/decisions/](docs/decisions/README.md).
