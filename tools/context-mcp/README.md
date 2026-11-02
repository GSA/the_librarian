# The Librarian MCP adapter

Read-only stdio tools over the local reader and native review-event store.
Install from the toolkit root with `python -m pip install ./tools/context-mcp`.
This package is the adapter, not the complete reader distribution; retain the
toolkit source files and provide their explicit paths.

The distribution name is `the-librarian-mcp`, the module is
`the_librarian_mcp`, the commands are `librarian-mcp` and
`librarian-preflight`, and the MCP protocol identity is `the-librarian`.
The earlier `qualified-context-*` names were removed on October 21, 2026;
update each MCP client configuration explicitly, since no alias remains.

The toolkit root README describes empty initialization, external policy and
content selection, preflight, source packaging and permission boundaries.
No organization data or demonstration corpus is bundled.

## Full-text audit log

`--audit-log [PATH] --agent-label LABEL` opts into a full-text audit of every
tool call, separate from the body-free diagnostics below. Each
`librarian-mcp-audit-v1` JSONL record holds the label, tool, complete
arguments, outcome, the complete text returned to the client, its SHA-256, and
the locators, source hashes and record ids it mentions. A bare `--audit-log`
writes `.local/audit/<label>-mcp-access.jsonl`; any path must be a `.jsonl`
file below `<root>/.local` without symlinks. Files are 0600 in a 0700
directory, appended with fsync and rotated into `<name>.<n>.jsonl` before
exceeding 64 MiB; nothing is truncated or deleted. A write failure returns an
error instead of the result. The log contains corpus text and agent queries;
it is never retrieval context and no tool reads it. Summarize it without bodies:

```
librarian-audit --root <root> report
```

See [decision 0011](../../docs/decisions/0011-full-text-mcp-audit-log.md).

## Retrieval diagnostics

`--diagnostics-log <path>.jsonl` opts into body-free retrieval diagnostics,
written by `tools/diagnostics/retrieval_log.py`. The path must stay within
`--root`. Omit the flag and nothing is recorded, which is the default.

`search` is observed in full: hit, empty with the absence classification the
reader computed, or an error exception class name. `get` and `get-many` are
observed only as a bare count; no locator is recorded, so the log cannot say
which records were read and is not a basis for pruning selection. No
search-to-read ratio is reported, because that number cannot distinguish an
unconvincing search excerpt from a question the search result already answered.
`status`, `catalog` and `review-events` write nothing. Query text, locators,
excerpts, prompts and model output are not representable in a record, so they
cannot be written. A logging failure refuses the result rather than returning
it silently.

Read and bound the log with the writer's own CLI:

```
python tools/diagnostics/retrieval_log.py report [--since-days N]
python tools/diagnostics/retrieval_log.py prune --keep N
```

`report` separates the collection-gap rate (`absent-corpus`: nothing on the
subject is indexed) from the findability-gap rate (`present-uncombined`: the
terms are indexed but never co-occur), and reports raw search and read counts.
Diagnostics are never retrieval context, never a review, never evidence about
any subject matter, and never change selection, ranking or qualification. An
absence classification reports what is indexed; it never shows that a fact does
not exist.
