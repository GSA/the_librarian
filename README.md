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
holds that program's content and config. Code questions go to **The Knowitall**.

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

## Configuration

| File | Purpose |
| --- | --- |
| `config/policy.json` | Defaults: `original_roots` (`sources/`), `markdown_roots` (`documents/`), exclusions, and handling text |
| `config/policy.local.json` | Instance overrides, ignored by Git |
| `config/ontology.json` | Allowed relationship types (`config/ontology.local.json` to override) |
| `config/selection.local.json` | List of documents agents can retrieve, ignored by Git. It uses the same format as `config/selection.json`. |

Stores: `.local/catalog` (metadata), `.local/review-events` (reviews), and
`tools/context_index/.local` (search index, which can be rebuilt). Every write
creates an automatic backup.

## Ingesting files (current manual method)

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
   - For Gemini transcripts or ODT:
     `.venv/bin/python -m tools.extraction.extract {prepare,verify,register} D1 --format odt ...`
   - For PDF or images: use Docling. See
     [tools/extraction/README.md](tools/extraction/README.md). Docling needs its
     own environment and pinned models. A Docling result can be selected for
     retrieval only after a recorded review of that exact output.
5. **Write** the reviewed knowledge as Markdown under `documents/`, citing the
   receipt. Record reviews with `python -m tools.review_events.event_store` if needed.
6. **Select** the document in `config/selection.local.json`:
   ```json
   {"schema_version": 1, "audience": "authorized-local",
    "documents": [{"path": "documents/memo.md", "role": "business-rule", "context": []}],
    "context_rules": {}, "renditions": []}
   ```
7. **Rebuild** the index. Do this after every change to content or selection:
   ```sh
   .venv/bin/librarian-preflight --root "$PWD" \
     --manifest config/selection.local.json --refresh-index
   ```

Registering a file does not make it retrievable. Only documents listed in the
selection file are indexed.

**Optional: durable intake.** `python -m tools.intake.cli` can run catalog
registration, Docling extraction, review recording, and the index rebuild as a
staged job that can be retried, resumed, or replayed. You still supply every
file and record, and nothing is fetched or scheduled. See
[context/intake.md](context/intake.md).

## Interfaces

Run these from the clone root. Each supports `--help`.

| Command | Purpose |
| --- | --- |
| `python -m tools.catalog.catalog_store` | Catalog: init, status, submit, query, backup, restore |
| `python -m tools.catalog.inventory` | List files under a directory (metadata only) |
| `python -m tools.extraction.extract` | Text, transcript, and ODT extraction |
| `python -m tools.extraction.docling_extract` | Optional PDF and image extraction |
| `python -m tools.review_events.event_store` | Record reviews |
| `python -m tools.review_events.event_reader` | Query and export reviews |
| `python -m tools.context_index.cli` | Build, search, and read the index from the shell |
| `python -m tools.intake.cli` | Optional durable intake |
| `librarian-preflight` | Check prerequisites; `--refresh-index` rebuilds the index |
| `librarian-mcp` | Read-only MCP server |

## MCP for consumer agents

`librarian-mcp` is a read-only stdio MCP server. Its tools are `status`,
`search`, `get`, `get_many`, `catalog`, and `review_events`. Paths must be
absolute. `--root` must be the full instance copy.

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
[ROADMAP.md](ROADMAP.md), [docs/decisions/](docs/decisions/README.md), and the
[business-context contract](docs/contracts/business-context.md).
