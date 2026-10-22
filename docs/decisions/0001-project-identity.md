# 0001 - Project, package, command and protocol identity

**Audience:** maintainers and operators of any installation.
**Date:** October 21, 2026.
**Status:** Implemented in this repository; dependent installations and client
configurations must be updated by hand.

## Context

The project is named The Librarian. The extracted toolkit kept earlier
`qualified-context-*` identifiers for the directory, Python distribution,
module, console commands and MCP protocol name. Two names for one system
made every instruction ambiguous: operators could not tell whether
`qualified-context` referred to the product, the adapter package, the reader
or the protocol identity a client registers.

The owner chose a complete rename with no compatibility aliases, accepting
that existing client registrations break until they are corrected explicitly.

## Decision

Rename the externally visible software identity and leave persisted data
identity untouched.

Renamed:

| Surface | Before | After |
|---|---|---|
| Checkout directory | `qualified-context-kit` | `the_librarian` |
| Python distribution | `qualified-context-mcp` | `the-librarian-mcp` |
| Python module | `qualified_context_mcp` | `the_librarian_mcp` |
| Console commands | `qualified-context-mcp`, `qualified-context-preflight` | `librarian-mcp`, `librarian-preflight` |
| MCP protocol identity | `qualified-context` | `the-librarian` |
| Review-event schema `$id` | `urn:qualified-context:review-events:1` | `urn:the-librarian:review-events:1` |
| Adapter version | `0.2.0` | `0.3.0` |

The schema `$id` was renamed on a second pass, after confirming every store in
this repository was empty (0 catalog revisions, 0 review batches, 0 events, 0
corrections). The `$id` is hashed into the review store's `contract_hash`, so
renaming it invalidated the existing empty store. The store correctly refused
to open - `Stored contract differs from installed contract; explicit migration
required` - and was re-initialized rather than migrated, which is the right
handling for an empty store and not a precedent for a populated one.

Deliberately unchanged, because they are persisted identity shared with a
populated installation:

- The store authority strings `catalog-metadata-only` and
  `review-events-only`, the SQLite `application_id` values and
  `user_version`/`schema_version` numbers. The `application_id` values decode
  as ASCII `FCAT`, `FREV` and `FCTX` - the `F` is a fossil of the originating
  program name. Renaming them is cosmetic and would cost a migration against
  80,309 catalog versions and 1,670 review events in the originating
  installation, so the fossil stays.
- Record locator syntax, receipt identifiers and selection-manifest fields.

The originating installation is unaffected by the `$id` rename: it already
uses a different schema identity (`urn:falcon:review-event-trial:1`, authority
`trial-non-authoritative`, audience `authorized-local-post-eod`) and therefore
a different `contract_hash`. The two review contracts had already diverged
before this rename.

No alias, shim, deprecation period or forwarding entry point is provided.

## Consequences

- Operators must edit each MCP client registration to the new command and
  server name. A stale registration fails loudly rather than resolving to a
  silently different server.
- Any review store created before this rename is unreadable by this version
  and must be re-initialized. That is only acceptable because every store here
  was empty; it would otherwise require the explicit migration demanded by
  [0003](0003-store-compatibility.md).
- The originating corpus installation still runs the old names. It is a
  separate installation with its own upgrade path; see
  [0004](0004-instance-topology.md). Neither installation imports code from
  the other's checkout, so the two can differ until it is upgraded.
- Existing catalog, review-event and index databases are readable by the
  renamed adapter without migration, because only software identity changed.
- A future rename of persisted identity, if ever justified, requires the
  explicit migration and recovery design demanded by the roadmap's refresh
  procedure. Schema snapshots are not backups.

## Verification performed

In this repository, after the software rename: schema snapshot check reported
no drift, the full `tests` suite passed, empty initialization reported zero
seeded records, and `librarian-preflight` reported all checks passing after
refreshing the disposable index. The index refresh was required because the
index binds the absolute corpus root and the checkout directory moved; that
is the designed stale-read refusal, not a defect.

After the schema `$id` rename: store emptiness was confirmed by direct row
counts first; the renamed contract produced a new `contract_hash`
(`aaa2eaec...` to `d2a0d3f8...`); the old store refused to open with the
migration-required error; a fresh `init` produced a new store id and recorded
the new contract hash with a verified backup; the schema snapshot check and
the full test suite passed again.
