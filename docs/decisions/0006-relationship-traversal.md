# 0006 - Relationship traversal and graph storage options

**Audience:** maintainers and operators.
**Date:** October 21, 2026. Embedded-candidate facts checked October 22, 2026.
**Status:** Open. No graph capability exists in this repository. This document
records the evaluation and what would have to be true before adding one.

## Context

A question arose whether a dedicated graph database — Neo4j was named — is a
good fit, versus the derived SQLite projection approach already prototyped
elsewhere.

The originating installation has a derived relationship projection built over
the catalog and index: a `nodes`/`aliases`/`edges` schema in SQLite, around
540 lines of implementation with a comparable volume of tests, bound to the
reader's freshness checks and refusing stale reads. Its CLI builds the
projection, reports status, and walks from an identifier with a depth, edge
kind and direction. It is derived and rebuildable, writes nothing canonical,
and infers no facts.

That prototype runs at **single-digit thousands of nodes and edges, in a
SQLite file of a few tens of megabytes**, over a few hundred registered
receipts and a few thousand review events. Edge kinds are dominated by
containment, cross-reference and review-linkage relationships. Exact instance
counts belong in that installation's private workspace, not here; the
characterization above is what the design argument depends on.

Its notice is the substantive part of the design: a reference is not support,
a parent is not a dependency, a review is not verification, missing edges do
not establish absence, and reverse traversal identifies recorded connections
rather than a complete impact assessment. The graph is a discovery aid that
points at canonical records; it is never the evidence.

This capability has not been ported here. The roadmap lists it as a candidate
to evaluate *after* core field use, and classifies graph work as "later, only
if justified".

## Evaluation

### Scale does not justify a graph server

Single-digit thousands of nodes and edges is small. A recursive CTE over an
indexed edge table answers bounded-depth traversal on this data without
measurable difficulty, and the projection already caps traversal (depth, edge
kinds, node count) because unbounded traversal produces unreadable output, not
because SQLite cannot cope. A dedicated graph engine earns its cost at scales
and query complexities that are orders of magnitude away, and nothing in the
per-project topology suggests a single instance will approach them — each
instance holds one project's corpus, not an aggregate.

### A server contradicts the current architecture

The system is deliberately file-based, single-writer and offline. Authority
stores are append-only SQLite with hash chains, verified backups and
triggers; the derived index is rebuildable and refuses stale reads; the
adapter is a read-only stdio process. Introducing a database server adds a
runtime dependency to every instance, a second backup and recovery story, a
network surface, an authentication surface, and a new way for an instance to
be partially available. Multiplied by one instance per project, as decided in
[0004](0004-instance-topology.md), that is `N` servers to run, pin, patch and
restore.

It also breaks the property that makes the current design trustworthy: the
derived store can be deleted and rebuilt at any time, so it can never become
an unintended authority. A graph server that is expensive to rebuild tends to
become the thing people query directly, and then the thing people trust.

### What a graph engine would actually buy

Genuine advantages exist and should be acknowledged rather than dismissed:
a declarative path query language is far more expressive than hand-written
recursive CTEs; variable-length and shortest-path queries are first-class;
and exploratory visual tooling is better. If the use case becomes open-ended
multi-hop path discovery across a large, densely connected corpus, that
expressiveness is worth real money.

None of that is the current use case. The current use case is "show me what
is recorded as connected to this record, a bounded distance away, so I can go
read the canonical evidence."

### Embedded alternatives

If query expressiveness later becomes the binding constraint without the
scale or operational appetite for a server, an embedded graph engine (a
file-based engine with a declarative query language) preserves the
rebuildable-file property while improving queries.

**Candidates checked October 22, 2026.** The earlier `Unverified` note is
resolved for the two obvious options. Both were checked against package and
repository metadata only; neither was installed, benchmarked or tried against
any corpus.

| Candidate | License | Latest | Platform / Python | Finding |
|---|---|---|---|---|
| Kuzu (embedded, Cypher) | MIT | 0.11.3, uploaded 2025-10-10 | manylinux/macOS/Windows wheels for cp37–cp314, including 3.13t/3.14t free-threaded | **Upstream repository is archived.** Its README says "Kuzu is working on something new" and that prior releases "will continue to be usable", with docs and blog moved to GitHub Pages and extensions requiring 0.11.3 or a self-hosted extension server |
| Apache AGE (Cypher in PostgreSQL) | Apache-2.0 | per-PostgreSQL tags, `PG15/v1.8.0-rc0` dated 2026-09-19; PG18 and PG19 tags exist | Server extension; Python driver `apache-age-python` 0.0.7 last uploaded 2023-09-22 | Actively developed, but **every release is tagged `-rc0`** and support is cut per PostgreSQL major, so the pin is a build-and-extension decision, not a wheel |

Neither is adoptable as written. Kuzu matches the rebuildable-file property
and has the stronger packaging story, but an archived upstream fails the
pinning intent of [0002](0002-updateable-distribution.md) — a pinned
dependency with no maintainer is a frozen dependency, and its MIT license
permits a fork without making one free to maintain. A community fork exists
under a different name with a fraction of the activity; forks are not a
maintenance guarantee. AGE is maintained under a foundation but is a
PostgreSQL server extension, which is the server dependency this decision
declines, and its Python driver has not been released in three years.

**Still unverified:** runtime behavior, query correctness, rebuild cost,
concurrency, crash recovery, and whether either reads the schema shape this
projection would produce. Nothing here was executed. Any future proposal must
re-check these facts at that time — archived and release-candidate states
change — and must satisfy the same license, release-cadence, platform,
Python-version and pinnability rules as [0002](0002-updateable-distribution.md).

## Current position

Do not add a graph database server. If and when relationship traversal is
wanted here, port the derived SQLite projection approach: rebuildable,
freshness-bound, stale-read-refusing, and explicitly not evidence.

Before any graph work starts, these must be true:

1. A real question was asked that the existing exact reads, lexical search and
   selection could not answer, and it was recorded.
2. The canonical evidence route for answering it is identified, so the graph
   is a navigation aid rather than an answer source.
3. The preserved qualifiers survive the port: no inferred facts, no implied
   support, no absence claims, stale-read refusal intact.
4. The projection is rebuildable from the authority stores alone.

### Precondition 1 remains unsatisfied — October 21

The originating installation measured its prototype's traversal surface and
reported the result to its owner. Summarized generically, with no instance
data: the traversal interface there cannot express per-hop edge kinds,
per-hop direction, or a path between two named endpoints; and a set of other
shapes it also cannot express — absence sets, aggregation and ranking, and a
pattern-matched rather than single-identifier start — turned out to be
ordinary SQL over the same schema, answered in well under a second.

Three genuinely path-shaped queries were identified. All three were
**hypothetical shapes the interface cannot express, not questions a real task
had asked and failed to answer.** Measured read cost there was dominated by
input validation rather than traversal, so a different engine would not have
improved it. That measurement therefore does **not** satisfy precondition 1,
and the owner was told so explicitly.

This is useful evidence for the opposite reason: it demonstrates that
*interface expressiveness*, not storage engine, was the limiting factor in the
one place a projection actually exists. A future precondition-1 record must
name a real task that failed, not a query shape a CLI happens not to accept.

A graph engine is reconsidered only if a recorded use case needs multi-hop
path queries that a bounded recursive CTE cannot express, at a scale where
SQLite measurably fails — and then with an explicit answer for backup,
recovery, authorization, concurrency and per-instance operational cost.

## Consequences of leaving this open

- Relationship discovery stays unavailable in this distribution. Operators
  navigate through exact reads, search and the selection manifest.
- The prototype projection elsewhere remains an unported reference, not a
  shipped feature, and must not be described as one.
- Deciding this later costs little, because a derived projection can be added
  without migrating any authority data.
