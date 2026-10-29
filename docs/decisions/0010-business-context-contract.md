# 0010 - Business entity, claim and relationship contract

**Audience:** maintainers, Librarians, refiners and Engineers.
**Date:** October 29, 2026.
**Status:** Accepted; pure bundle validation, scoped identity resolution and answer
conformance implemented and exercised by synthetic tests. Business persistence,
qualification authority, ingestion, traversal and entailment remain unimplemented.

## Decision

Adopt the [version-1 business-context contract](../contracts/business-context.md)
for capabilities, rules/requirements, workflows, domain terms/entities, roles,
systems/integrations and decisions. Instance-local opaque entity IDs, scoped
aliases and explicit append-preserving merge/split decisions replace name-based
identity guesses. Typed directional claims carry exact source/version/hash/locator,
attribution, observation and half-open validity, contradiction and correction links.

`basis: reported` records attribution, independently of qualification. Extracted
and attributed inferred-candidate claims cannot become confirmed through confidence,
repetition or reviewer agreement. Confirmation requires genuine native review
references bound to an explicit business disposition in maintained private narrative.
The pure validator checks shapes and companions, not actual event existence,
source truth, independent review, authority, permissions or runtime freshness.

Knowitall owns code identity and dependencies. Librarian carries only external
repository/revision/snapshot/locator-bound `references-code` navigation. A link
is not support, absence of a record is not absence in the world, and reverse
navigation is not complete impact. All answers retain these qualifications.

## Existing authority route

Retain authorized narrative; register actual sources/artifacts through
`catalog_store.submit` using real provenance, handling, request ID and state
bindings; append genuinely inspected native reviews through `event_store.append`;
select narrative and correction/qualification companions; refresh and consume
exact qualified reads. Bundle validation does not perform any of these writes.
A JSON artifact is not canonical entity/claim registration.

[0003](0003-store-compatibility.md) remains binding. Existing catalog KINDS,
ID syntax, source/artifact endpoints, review-event schemas and index contracts
are unchanged. A future business writer needs deliberate versions, fields,
endpoint/history validation and release compatibility/migration design. Local
ontology overrides cannot admit business record kinds or entailed authority facts.

Immediately before any separately approved reset/migration, inspect actual
catalog AND review stores, bindings/counts and retained private narrative.
Unknown, inaccessible, corrupt or nonempty state refuses an empty-store path.
A data-empty distribution is not live emptiness evidence. Existing initialization
never overwrites authority. Nonempty state requires approved, backed-up, rehearsed
append-preserving migration or a separately approved new instance. No live reset,
migration or source processing is authorized by this decision.

## Reconciliation and later gates

[0006](0006-relationship-traversal.md) still requires a real bounded engineering
question, failed exact-read/search/selection attempt, inspected coverage and exact
canonical evidence route, recorded in authorized private state before traversal.
That real-question gate remains unsatisfied; synthetic tests are not field acceptance.
No graph engine is selected.

[0009](0009-declared-ontology-and-entailment.md) separates reported attribution
from any computed deduction. Upstream extraction/model candidates are attributed
pending input, never synthesized projection nodes or confirmed facts. Future
entailed results must be labelled, premise-carrying, rebuildable and freshness-bound;
no entailment or projection is implemented here. New relations require explicit
contract/version decisions, not implicit algebraic inference.

## Evidence and consequences

`tests/test_business_context.py` exercises scoped collisions, normalization,
merge/split and correction history, malformed inputs, typed evidence and external
references, contradicted/expired/candidate answers, companions and fail-closed
stale/denied states. Temporary compatibility cases prove existing writer refusal
and initialization preservation; package cases exercise the explicit release list.
No test initializes a installed corpus or grants publication permission.

Engineers can implement against a bounded contract without gaining an alternative
source/review authority. Future runtime qualification and storage remain explicit
work rather than advertised capabilities of a structural validator.
