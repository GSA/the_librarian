# The Librarian

Local, data-empty tools for maintaining document metadata, recording attributed
reviews and retrieving exact, bounded context through a read-only MCP interface.
No organization corpus, inherited database, example business records or
conversation history is included. Initialization creates empty stores, not
demonstration claims.

The Librarian is the project name. As of October 21, 2026 the identity is
unified: the checkout directory is `the_librarian`, the adapter package is
`the-librarian-mcp` (module `the_librarian_mcp`), the commands are
`librarian-mcp` and `librarian-preflight`, and the MCP protocol identity is
`the-librarian`. The earlier `qualified-context-*` names are removed, not
aliased; existing MCP client configurations must be updated explicitly. Store authority strings, schema
versions and record identifier syntax are unchanged. The empty local stores now
use `LCAT`, `LREV` and `LCTX`; superseded identities require explicit migration
or re-initialization and are never converted automatically.
Each program supplies its own authorized content, policy, selection and
stores; the reusable distribution contains none of them.

**Start here:** [agent operating rules](AGENTS.md),
[Librarian contract](agents/librarian.md),
[Jira-to-Kata refinement](agents/refiner.md),
[Engineer contract](agents/coder.md), and
[contribution/disposition handoffs](agents/contributions.md).
The [development and ingestion roadmap](ROADMAP.md) is the canonical
maintainer/operator plan for missing capabilities and future automation,
not a claim that those capabilities are implemented.
[Recorded design decisions](docs/decisions/README.md) cover identity,
updateable distribution, store compatibility, instance topology and source
onboarding; each states whether it is implemented or only agreed.

## Requirements

Linux/POSIX, Python 3.11 or later with SQLite FTS5, and the dependencies declared
in `tools/context-mcp/pyproject.toml`. `librarian-preflight` probes the actual
interpreter, SQLite build, FTS5 availability and adapter dependency versions and
refuses to proceed on a mismatch; run it before serving. Writes use filesystem
locking and private
directories. The application does not call embedding/model services or start a
network listener. Your MCP client may forward returned text to a model; choose
an authorized processing path.

One installation serves one project's data root, policy and stores. Policy is
loaded from this directory, so a second project needs its own installation; see
[decision 0004](docs/decisions/0004-instance-topology.md).

## Initialize an empty installation

Run from this directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ./tools/context-mcp
.venv/bin/python -m tools.schemas.export_schemas --check
.venv/bin/python tools/initialize.py
.venv/bin/librarian-preflight \
  --root "$PWD"
```

Initialization creates private `.local/catalog`, `.local/review-events` and
`tools/context_index/.local` storage. It validates existing stores instead of
reinitializing them. It does not silently rebuild a stale existing index;
request `--refresh-index` through preflight after deliberate configuration or
source changes. Initialization and every authoritative write retain verified
local snapshots. These are not off-host disaster recovery.

If the host lacks `ensurepip` but already supplies system pip, create the
environment with `python3 -m venv --without-pip .venv`, then install using
`python3 -m pip --python .venv/bin/python install ./tools/context-mcp`.
Missing prerequisites must be resolved explicitly; do not reuse another
project's environment as the distributed installation.

## Configure your own content

`config/policy.json` contains neutral deployment defaults. An optional ignored
`config/policy.local.json` overrides selected top-level fields. Path roots,
filename-exclusion regular expressions and transcript section dictionaries
are configuration, not code. Do not commit a private dictionary or include
it in a release. Format labels are parser rules, not supplied document content.

Put authorized Markdown beneath a configured `markdown_roots` directory and
original artifacts beneath `original_roots`. Neither directory is crawled or
automatically indexed. The default selection is empty.
Registered renditions remain subject to their original source's current
root and exclusion policy. Tightening that policy blocks rebuilds and reads
of a selection containing the excluded source before its text or rendition
is opened; registration does not exempt extracted text from current policy.

Create an ignored `config/selection.local.json` following the shape of
`config/selection.json`. Each `documents` entry requires your actual `path`,
`role` and a `context` array of required companion locators. `context_rules`
maps a selected `path#anchor` to additional required locators. This supports
qualifications, corrections and related passages without hardcoded question
IDs or business dictionaries. Required locators must resolve; they are not
optional suggestions. Empty `documents` and `renditions` arrays are valid.

Pass `--manifest config/selection.local.json` to both the reader and MCP
client/preflight when using that selection. Rebuild explicitly:

```sh
.venv/bin/librarian-preflight \
  --root "$PWD" \
  --manifest config/selection.local.json --refresh-index
```

Policy belongs to this installed toolkit; `--root` selects the data workspace,
not a second implicit policy file. Separate deployments can have separate
toolkit configurations. Policy changes invalidate derived retrieval. Changing
the review audience changes the persisted contract: do not change it on a
populated store without an explicit migration design.

## Interfaces

Run toolkit commands from the clone root with `python -m`; each supports `--help`.

| Tool | Purpose |
|---|---|
| `python -m tools.catalog.catalog_store` | Initialize, submit/query metadata, inspect revisions, back up and restore |
| `python -m tools.catalog.inventory` | Explicit-root metadata discovery with configured exclusions; never registration or source interpretation |
| `python -m tools.review_events.event_store` | Schema, attributed native append, correction history, state-pinned writes, backup and restore |
| `python -m tools.review_events.event_reader` | Exact filtered event queries and one-off Markdown export with state-pinned pagination |
| `python -m tools.extraction.extract` | Registered Gemini-format UTF-8 transcript/ODT extraction, verification and separate metadata registration |
| `python -m tools.extraction.docling_extract` | Optional offline PDF/image Docling prepare, repeat verification and separate deferred-rendition registration |
| `python -m tools.context_index.cli` | Explicit build, freshness status, lexical search, qualified get/get-many and live catalog reads |
| `python -m tools.diagnostics.retrieval_log` | Opt-in body-free retrieval diagnostics: write, report and prune; never a catalog, review or retrieval authority |
| `librarian-mcp` | Read-only stdio adapter: status, search, get, get_many, catalog, review_events |

Catalog submissions are an object with a `records` array. Catalog records use
`id`, `kind` and `data`; the validator defines exact fields for receipt, source,
artifact, policy and relationship records. Protocol identifiers use D for
receipts, S/I/O for source classes, A- for artifacts and R- for relationships;
these are identifier syntax, not preloaded identities. Native review bundles
use the JSON schema returned by `event_store.py schema`. Supply actual
attribution, handling, known/unknown metadata and scope; do not fabricate
review results to satisfy required fields.

Retrieval returns original text, hashes and exact spans, together with
configured qualifications. Search snippets are discovery only. Compact and
batched formats share response-local references without discarding evidence.
Stale inputs or insufficient budgets produce errors, not truncated
qualifications. An empty store/query means no supplied records, not proof of
absence in the world. A search with no hits reports why: whether no query term
is indexed at all, some are missing, or all are indexed but never appear in
one section. That classification describes this corpus's coverage and
findability, never the world.

Retrieval diagnostics are off unless an operator passes `--diagnostics-log` to
the adapter. They record search outcomes, absence classifications, and a bare
count of reads; query text, locators, excerpts, prompts and model output are
not representable. Because no locator is recorded, the log cannot report which
documents were read and is not a basis for pruning selection. Diagnostics are
never returned by a read tool, never count as a review, and never change
selection, ranking or qualification. See
[the adapter README](tools/context-mcp/README.md) and
[decision 0007](docs/decisions/0007-execution-traces.md).

MCP invocation:

```sh
.venv/bin/librarian-mcp \
  --root "$PWD"
```

The process waits for a client over stdio. Client configuration must use this
copy's executable and paths. No client is modified by setup.
The intended workflow is Jira input -> ticket/refinement agent -> local Kata
specs/tasks -> Engineer, with qualified Librarian consultation and returned
context discoveries throughout. The role documents are operator-loaded
contracts, not an installed Jira/Kata connector, agent runner or watcher.
An independent review checks the original request and implementation evidence.

## Reproduction and distribution

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/package.py --output .local/the-librarian.zip
```

Tests use temporary workspaces and this toolkit's own documentation as input.
They create transient structural records to exercise contracts; they never
seed the installed stores or pretend to describe an external organization.
The package uses the explicit `release-files.json` allowlist, not a directory
copy or a keyword-redaction rule. It excludes local policy, content, databases,
snapshots, environments, caches and version history. Review changes to every
allowlisted file before distribution; an allowlist does not certify its text.

This is preparation for reproduction, not publication authorization. No license
grant is selected. Resolve ownership, licensing and dependency obligations
before publishing. Do not infer release permission from absence of names.

## Boundaries

This toolkit does not include historical corpus-import mappings, domain-specific
coverage parsers, live external connectors, semantic search, rendered-document
verification, cloud hosting, multi-user authorization or enforced agent
sandboxes. Native event queries replace no missing historical review evidence.
Source-specific extensions should be separate private adapters/configuration,
with explicit contracts and tests, not constants embedded in core code.
Database structure snapshots are not data backups; retain originals and
verified snapshots. Keep raw sources, narrative authority, metadata, review
events and the disposable search index distinct.
