# The Librarian

Local, data-empty tools for maintaining document metadata, recording attributed
reviews and retrieving exact, bounded context through a read-only MCP interface.
Initialization creates empty `LCAT`, `LREV` and `LCTX` stores (`the-librarian-mcp`
package, `librarian-mcp` and `librarian-preflight` commands, and `the-librarian`
MCP protocol identity); each program instance supplies its own content, policy,
selection and stores.

**Start here:** [agent operating rules](AGENTS.md),
[Librarian contract](agents/librarian.md),
[Jira-to-Kata refinement](agents/refiner.md),
[Engineer contract](agents/coder.md),
[contribution/disposition handoffs](agents/contributions.md),
[development and ingestion roadmap](ROADMAP.md), and
[recorded design decisions](docs/decisions/README.md).

## Requirements

Linux/POSIX, Python 3.11 or later with SQLite FTS5, and the dependencies declared
in `tools/context-mcp/pyproject.toml`. `librarian-preflight` verifies the Python
interpreter, SQLite build, FTS5 availability and adapter dependency versions before
serving. Writes use filesystem locking and private directories. One installation
serves one project's data root, policy and stores; see
[decision 0004](docs/decisions/0004-instance-topology.md).

## Initialize an empty installation

Run from this directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ./tools/context-mcp
.venv/bin/python tools/schemas/export_schemas.py --check
.venv/bin/python tools/initialize.py
.venv/bin/librarian-preflight \
  --root "$PWD" --reader "$PWD/tools/context-index/context_index.py"
```

Initialization creates private `.local/catalog`, `.local/review-events` and
`tools/context-index/.local` storage, validates existing stores, and retains
verified local snapshots on every authoritative write. Pass `--refresh-index`
through preflight to rebuild the search index after configuration or source changes.

If the host lacks `ensurepip` but supplies system `pip`, create the environment with
`python3 -m venv --without-pip .venv` and install via
`python3 -m pip --python .venv/bin/python install ./tools/context-mcp`.

## Configure your own content

`config/policy.json` contains neutral deployment defaults, and an optional ignored
`config/policy.local.json` overrides selected top-level fields (path roots,
filename-exclusion regular expressions, and transcript section dictionaries).
`config/ontology.json` (with optional ignored `config/ontology.local.json`)
declares the relation vocabulary.

Place authorized Markdown beneath `markdown_roots` and original artifacts beneath
`original_roots`, then define the active selection in an ignored
`config/selection.local.json` following the shape of `config/selection.json`:
- `documents`: entries with `path`, `role`, and required companion `context` locators
- `context_rules`: mappings from `path#anchor` to additional companion locators
- `renditions`: registered renditions selected for indexing, subject to their original source's current root and exclusion policy

Pass `--manifest config/selection.local.json` to both the reader and MCP
client/preflight, and rebuild explicitly:

```sh
.venv/bin/librarian-preflight \
  --root "$PWD" --reader "$PWD/tools/context-index/context_index.py" \
  --manifest config/selection.local.json --refresh-index
```

## Interfaces

Each command supports `--help`. CLI paths are relative to this toolkit.

| Tool | Purpose |
|---|---|
| `tools/catalog/catalog_store.py` | Initialize, submit/query metadata, inspect revisions, back up and restore |
| `tools/catalog/inventory.py` | Explicit-root metadata discovery with configured exclusions |
| `tools/review-events/event_store.py` | Schema, attributed native append, correction history, state-pinned writes, backup and restore |
| `tools/review-events/event_reader.py` | Exact filtered event queries and Markdown export with state-pinned pagination |
| `tools/extraction/extract.py` | Registered Gemini-format UTF-8 transcript/ODT extraction, verification and metadata registration |
| `tools/extraction/docling_extract.py` | Optional offline PDF/image Docling prepare, repeat verification and deferred-rendition registration |
| `tools/context-index/context_index.py` | Explicit build, freshness status, lexical search, qualified get/get-many and live catalog reads |
| `tools/diagnostics/retrieval_log.py` | Opt-in body-free retrieval diagnostics: write, report and prune |
| `librarian-mcp` | Read-only stdio adapter: status, search, get, get_many, catalog, review_events |

Catalog submissions take a `records` array of `id`, `kind` and `data` objects
(`D*` receipts, `S*/I*/O*` sources, `A-*` artifacts, `R-*` relationships, and
policy records). `config/ontology.json` defines relation terms, allowed endpoint
kinds, disjoint pairs, and the `reported` evidentiary basis enforced by the store
(see [decision 0009](docs/decisions/0009-declared-ontology-and-entailment.md)).

Native review bundles follow `event_store.py schema` with attribution, handling,
known/unknown metadata, and scope.

Retrieval returns original text, hashes, exact spans, and configured companion
qualifications. Zero-hit searches return an `absence` classification
(`absent-corpus`, `absent-partial`, or `present-uncombined`) describing coverage
within the indexed selection. Passing `--diagnostics-log` to `librarian-mcp`
enables body-free search/read count logging (see
[tools/context-mcp/README.md](tools/context-mcp/README.md) and
[decision 0007](docs/decisions/0007-execution-traces.md)).

MCP invocation:

```sh
.venv/bin/librarian-mcp \
  --root "$PWD" --reader "$PWD/tools/context-index/context_index.py"
```

## Reproduction and distribution

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/package.py --output .local/the-librarian.zip
```

Tests run in temporary workspaces using this toolkit's documentation as structural
input. `tools/package.py` packages the explicit `release-files.json` allowlist,
excluding local policy, content, databases, snapshots, environments, caches, and
Git history.

