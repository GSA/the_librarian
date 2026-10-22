# 0005 - Starting empty and onboarding sources through an inbox

**Audience:** maintainers and operators.
**Date:** October 21, 2026.
**Status:** Accepted. The inbox-to-catalog path is manual today; the connectors
and scheduling described here are planned, not installed.

## Context

Every instance starts with no content. The distribution contains no corpus, no
inherited database and no example records, and initialization seeds zero
records by design — this is a requirement, not a temporary limitation.

An instance becomes useful by ingesting from the sources its project actually
uses, which today include a local drop directory and will include hosted
systems such as a wiki and a shared drive. The current operator workflow is to
place files in an inbox directory and register them deliberately. Automation
is wanted, but the manual path is not a stopgap to be discarded: it is the
reference semantics every connector must reproduce.

The risk in automating this is not throughput. It is that an automated
connector quietly becomes a second authority — deciding that a document is
registered, reviewed, qualified or consumed because a transfer succeeded.

## Decision

### The inbox is the ingestion contract

All ingestion, manual or automated, lands content in the instance's private
inbox and then registers it through the supported catalog writer. A connector
is a producer of inbox content and source metadata; it is never a writer of
catalog, review or selection state. This keeps one authoritative writer per
store and lets a connector be added, replaced or withdrawn without changing
anything downstream of the inbox.

Consequences that follow and must hold for every connector:

- A copied file is not a registered receipt until its catalog commit is
  confirmed.
- A registered receipt is not extracted text.
- Extracted text is not a review.
- A review is not qualified context; selection is explicit and versioned.
- Successful ingestion is not business acceptance, and does not mark a source
  consumed.

### Source identity is the connector's real problem

Bytes are easy; identity is not. Retained local content is identified by
SHA-256. A native hosted document — a wiki page, a live spreadsheet — has no
stable byte identity, only an upstream version. A connector must establish a
verified upstream version and export mapping, and when the provider cannot
supply one it must report that verification is needed rather than infer
"unchanged" from a title, a timestamp or a rendered export.

Renames and moves do not create new source content. Content changes create
linked versions without overwriting the earlier one. Remote deletion never
deletes a retained original or a backup; loss of access triggers a handling
and serving-policy evaluation rather than an assumption that the local copy
remains authorized to serve.

### Discovery is separate from commitment

Keep two durable cursors: what has been discovered, and what has been
committed. Finding an item must not mark it processed. Interrupted pagination,
expired credentials, denied access and a lost mount are all distinguishable
from "no changes", and an empty or unmounted directory is never a successful
reconciliation.

### Remote access stays read-only

A connector to a hosted system is granted read-only scope on an explicitly
approved set of locations. It does not write, move, label or delete upstream.
Credentials live in the instance's private configuration, never in the
distribution, and never become executable authority. Source text is data; a
document that contains instructions is still data.

### Order of work

Local inbox automation first, then one approved remote scope, then additional
scopes. The local path exercises restart, duplicate, mutation, partial failure
and committed-retry behavior without any authorization or pagination
complexity, and those cases are where ingestion actually breaks. A remote
connector built before that behavior is proven inherits the complexity of both
problems at once.

### Breadth over keyword relevance

Traverse the permitted scope broadly enough to discover material whose
relevance is not yet known. An instance that only ingests what matches a
current ticket cannot answer the question the operator has not asked yet, and
a skim is never evidence of absence.

## Consequences

- Connectors stay small and replaceable, because they only produce inbox
  content and source metadata.
- Each new source type needs its own identity rules, fidelity tests and
  recovery tests before it can be scheduled. This is slower per connector and
  is the intended cost.
- Per-instance ingestion means per-instance credentials and per-instance
  authorized scope. No shared connector identity spans instances; see
  [0004](0004-instance-topology.md).
- The manual inbox path remains supported indefinitely. It is the fallback
  whenever a connector is unavailable, unauthorized or untrusted for a
  particular source.

## Not decided

- Which hosted system is connected first, and under what authorization.
- The inbox layout and staging rules for an instance.
- Whether scheduling is a timer, a service or an operator-run command.
