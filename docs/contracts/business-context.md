# Business entity, claim and relationship contract

Version: 1. Pure validation and resolution implemented; persistence and traversal remain gated.

## Intent and delivery boundary

Give Librarian maintainers and Engineers one precise, reusable business-context
contract to implement against. Cover capabilities, rules/requirements, workflows,
domain terms/entities, roles, systems/integrations and decisions without copying
Knowitall's code graph or shipping a demonstration corpus.

The implementation delivers durable contract documentation and pure,
executable structural/semantic validation with synthetic component tests.
It does not enable catalog business-record persistence, ingestion,
extraction, claim confirmation, a projection, entailment or MCP traversal.
Validation means conformance, not source truth, authorization or acceptance.
Existing catalog, review-event and index contracts remain unchanged.

Alternative approaches considered: documentation alone lacks executable failure
cases; a complete persistence/projection implementation overlaps separately
scoped successor work and requires authority compatibility decisions.

## Components and public interface

- `docs/contracts/business-context.md`: normative fields, relation semantics,
  qualification/answer behavior, identity history, writer mapping and gates.
- `docs/decisions/0010-business-context-contract.md`: accepted direction versus
  implemented validation; reconciliation with ADR0003/0006/0009.
- `tools/business_context.py`: pure validators and deterministic resolution,
  using standard-library types and local canonical-path checks.
  No filesystem reads, SQLite, configuration mutation, clock reads or writers.
- `tests/test_business_context.py`: generated, non-identifying in-memory cases;
  temporary existing-writer compatibility checks where necessary.

Public functions: `validate_bundle(bundle)`, `resolve_entity(bundle, query,
namespace, category=None)`, and `validate_answer(answer, bundle)`.
They raise `BusinessContextError` on invalid input. Resolution returns a stable
object containing status and sorted candidate IDs, not a guessed match.
Return behavior and exact fields are documented; validators do not mutate input.

## Identity and bounded vocabulary

A version-1 bundle contains exactly `contract_version`, `entities`, `claims`
and `identity_decisions`. Arrays may be empty, with explicit size bounds.
All record objects have exact field sets; unknown or missing fields fail.
Each top-level array is capped at 256 entries; nested lists at 30 except alias
lists at 100. Strings are bounded at 500 characters (paths 4096, pointers 1000),
nonempty without controls unless explicitly nullable. Tokens after ID prefixes
are 1–100 ASCII letters/digits/hyphens, starting with a letter/digit. Claims'
review IDs follow native rev-ID syntax. List duplicates fail. These are bounded
contract checks, not runtime authorization or memory-budget guarantees.
IDs are instance-local opaque protocol IDs, never names or source hashes:
`E-<token>` entity, `C-<token>` assertion revision, `M-<token>` identity decision.
Tokens use bounded ASCII letters/digits/hyphens. IDs are unique within a bundle;
a later instance contract must bind them to its stable instance identity.
IDs are never recycled. Each corrected assertion gets a new C-ID.

Entity fields: `id`, `namespace`, `category`, `name`, `aliases`, `evidence`.
Categories: `capability`, `rule`, `workflow`, `domain-term`, `domain-entity`,
`role`, `system`, `integration`, `decision`. A requirement is a rule; a domain
entity is a business concept, not an invented instance or a code symbol.
Aliases are bounded strings in the entity's namespace. Names/aliases are search
keys, not identity. No fuzzy match, stemming, transliteration or model choice.
Use Unicode NFC, casefold and whitespace collapse consistently, bound the
normalization version, and preserve the original display spelling.

Resolution: validate first; filter by exact namespace and optional category;
check a syntactically valid exact E-ID before normalized name/alias matching.
An exact ID outside the requested scope is unknown, never a cross-scope match.
Return resolved only for a single surviving target, ambiguous with all sorted
targets for multiple, and unknown for none. Alias collisions remain representable.

Identity decision fields: `id`, `operation` (merge/split), `inputs`, `outputs`,
`actor`, `decided_at`, `reason`, `evidence`, `supersedes` (null or M-ID).
Merge takes at least two distinct input IDs to one NEW output ID; split takes
one input to at least two NEW output IDs. All entities remain present. Decisions
must reference existing entities, be acyclic, and prevent simultaneous competing
active decisions consuming the same entity. Supersession is explicit, backward
linked and acyclic; old decision bytes remain. Corrected decisions consume the
same inputs; their predecessor output entities remain historical rather than
silently becoming current. Active redirects are followed for merges. Splits
return all reachable outputs and therefore ambiguity; no arbitrary successor.
Claim evidence and references are not rewritten or transferred by identity moves.

## Evidence and external references

Entity, claim and identity-decision evidence uses exact records with:
`source_id`, `artifact_id`, `source_version`, `sha256`, `locator`, `scope`.
Source ID follows existing S/I/O syntax and artifact ID existing A- syntax.
Version is an explicit nonempty identifier, not an assumed provider revision.
Hash is mandatory lowercase SHA-256. Locator fields: `path`, `anchor`,
`line_start`, `line_end`, `json_pointer`. Paths are canonical root-relative.
Either a positive inclusive line range (pointer null), or an exact JSON pointer
(line fields null); anchor is nonempty or null. Scope states inspected support,
not merely source co-occurrence. No parser or validator authenticates that support.
Text and rendition/source-location details remain with canonical exact reads.

External code reference fields: `provider` fixed to `knowitall`, `repository`,
`revision`, `snapshot`, `locator`. Repository is an explicit external identity;
revision and snapshot are nonempty immutable provider identifiers, never branch
names supplied as substitutes. Locator is provider-defined exact identity.
This contract does not validate provider availability, commit existence or runtime
behavior. No code-symbol nodes, dependency edges or copied code body enter it.

## Claims and directional semantics

Claim fields: `id`, `subject_id`, `relation`, `object_id`, `external_reference`,
`basis`, `qualification`, `origin`, `confidence`, `evidence`, `observed_at`,
`valid_from`, `valid_until`, `review_event_ids`, `contradicts`, `supersedes`,
`correction_reason`, `handling`.

Exactly one of internal object_id or external_reference is present/non-null.
Internal subject and object IDs must exist and match relation endpoint categories.
Initial finite relation set, explicitly not transitive or symmetric:

- rule `constrains` workflow/system/integration/capability;
- workflow `realizes` capability;
- role `participates-in` workflow;
- system/integration `supports` workflow/capability;
- integration `connects-to` system;
- decision `governs` rule/workflow/system/integration/capability;
- domain-term `denotes` domain-entity;
- rule/capability/workflow/decision `references-code` an external Knowitall ref.

`supports` expresses a reported operational relationship, not independent
corroboration or proof. `references-code` is navigation, not implements/satisfies.
No unknown relation is silently accepted or added to catalog ontology defaults.
Future relation extensions require a deliberate contract/version decision.

Basis is exactly `reported`: an attributed record of what was asserted.
Qualification is exactly `extracted`, `inferred-candidate`, `confirmed`,
`rejected` or `retracted`. The first three are the task's required positive axis;
the final two make negative dispositions explicit. `origin` contains exact
`actor`, `method` (manual/extraction/model), and nullable `tool_version`.
Model/extraction origins require tool_version; model actor names the producing
route, not an impersonated reviewer. Confidence is null or a finite real in
[0,1], excluding booleans; it is never a qualification trigger.

Claims require evidence, including inferred candidates: their evidence is the
candidate's inspected inputs, NOT proof of its inferred relation. Inferred
candidates may never be presented as confirmed. Confirmation/rejection/retraction
requires actual native review event IDs and an attributed decision preserved in
its canonical private home. A structurally valid review reference does not
verify an event, author, scope or authorization. Future runtime qualification
must resolve active events and that exact decision, check exact assertion/source
binding, actual scope, handling and independent-review requirements. Current
native review outcomes alone do not mean a business assertion is confirmed.

Times are timezone-aware ISO-8601; the documented comparison uses UTC instants.
Observation time is mandatory; valid_from/valid_until are independently nullable,
with a strict ordered half-open interval [from, until) when both are known.
Null bounds mean unknown, NOT indefinite validity or permission to claim current.

Contradicts is a unique list of other C-IDs and requires existing claims; self
links fail. A contradiction may be reported across unlike wording, so do not
invent a lexical or endpoint-equality test for it. Qualification cannot hide an
unresolved contradiction. Correction/supersession has one predecessor C-ID,
requires nonempty correction_reason, cannot cycle, and cannot branch into two
active successors. A replacement may change a mistaken endpoint; the linked
reason/evidence explains it. Store historical claims and evidence unchanged.
Semantic bundle checks resolve links independent of array ordering.

## Qualified answer contract

Answer fields: `contract_version`, `as_of`, `status`, `claim_ids`, `evidence`,
`review_event_ids`, `handling`, `limitations`, `freshness`.
Status is `qualified`, `partial`, `unknown`, `stale` or `denied`.
Freshness fields are exactly `catalog_state`, `review_state`, `vocabulary_hash`,
`selection_hash`, `policy_hash`, `source_state`, `code_state`, `checked_at`.
Tokens/hashes are nonempty exact bindings when available and null when unknown
or denied; hashes use lowercase SHA-256. Qualified answers require all bindings. Answer evidence uses the same exact
reference shape as claims; review IDs use native rev-ID syntax. checked_at and
as_of are mandatory timezone-aware times; checked_at must not exceed as_of.
States are current/stale/unknown/denied; code state may be not-applicable.
No pure validator measures freshness: all tokens and observations are supplied
by an authorized runtime and are checked against real authorities by that runtime.

Validator rejects qualified answers referencing candidate/extracted/rejected/
retracted/superseded claims, active unresolved contradictions, unknown validity
or intervals not containing as_of, missing evidence/reviews/handling, or any
noncurrent required freshness state. Required evidence and review references
must include those of every cited claim; no silent companion omission.
Partial/unknown must carry limitations and cannot imply absence or confirmation.
Stale refuses evidence-bearing answers: claim_ids/evidence/review_event_ids are
empty. Denied additionally requires all freshness bindings null, source_state
and code_state denied, and empty handling; it includes no source identifiers. Denial
reports only a generic limitation and status; runtime policy checks occur before
loading evidence, including historical correction and identity links.
Every answer's limitations includes the fixed generic caveat: `Missing records
do not prove absence; reverse navigation is not complete impact; a link is not
support.` Validation enforces this exact caveat without storing a query or body.

## Mapping to existing authorities and implementation gates

Catalog owns metadata and identity history; review_events owns attributed review
history; maintained private narrative owns source-backed interpretation and
explicit business disposition. Context index and any future projection are
rebuildable navigation, never another writer or truth authority.

Existing supported path: retain explicitly authorized maintained narrative;
register its source/artifact through catalog_store.submit with actual provenance,
handling, request ID and state token; record actual inspected source review via
event_store.append; deliberately select narrative with required qualification/
correction companions; refresh and consume exact qualified reads. Business bundle
validation does NOT register, index or confirm its contents, and storing a JSON
artifact does not create canonical entity/claim records.

A future business-record writer must deliberately extend catalog KINDS, ID
syntax, exact fields, endpoint resolution, referential/history checks and release
compatibility binding. Catalog's current source/artifact relation model remains
unchanged; declaring business categories in ontology.local.json still fails.
Review-event schema must not be overloaded with arbitrary business status fields;
future qualification binds genuine native event evidence to an explicit decision.
New fields/versions require a migration design, not an implicit adoption.

Immediately before any separately approved initialization/reset/migration, inspect
and validate actual targeted catalog AND review authority stores, bindings and
counts; also check retained/private narrative state. A data-empty package proves
nothing about an installed corpus. Unknown/inaccessible/corrupt/nonempty state
refuses an empty-store path. Existing initialization never overwrites stores.
Nonempty stores require approved, backed-up, rehearsed append-preserving migration
or a separately approved new instance; originals and history survive.
This task performs no reset, migration, new initialization or business ingestion.

## ADR0006 and ADR0009 reconciliation

Upstream model/extraction output can be an attributed pending candidate, never
canonical truth or a synthesized node in projection. Entity categories describe
source-backed business concepts; no model may mint an abstract node from silent
sources. Reported candidate provenance is not an entailed authority record.
Computed entailment, if later justified, remains labelled with all premises in a
rebuildable freshness-bound projection; never store/promote it as reported fact.
No entailment is implemented by this contract.

Before traversal implementation: record a REAL engineering task question, the
failed exact-read/search/selection attempt and inspected coverage, and the exact
canonical source/review/qualification route. Record it only in authorized private
state. A hypothetical question shape or passing synthetic test does not satisfy
that gate. No real question is invented here; its selection/access belongs to the
separately authorized first implementation slice. No graph engine selection.


## Python usage

Run from an installation root; these are Python APIs, not a CLI or MCP tool.

```python
from tools.business_context import validate_bundle, resolve_entity, validate_answer

validate_bundle(caller_bundle)  # None on success; BusinessContextError on refusal
resolution = resolve_entity(caller_bundle, caller_query, caller_namespace, category=None)
validate_answer(caller_answer, caller_bundle)  # None on success
```

Resolution returns exactly `{"status": "resolved" | "ambiguous" | "unknown",
"candidates": [sorted_entity_ids]}`. Normalized duplicate alias spellings within
one entity produce one candidate; literal duplicate list values fail. Superseded
identity outputs are historical, not current matches, unless explicitly consumed
by an active redirect. Final outputs must also match namespace/category scope.
All identity redirects, including historical ones, must be acyclic; each output
has a unique creating decision. Within the supplied bundle, an output cannot
be created after any decision has consumed that ID, including historical
decisions. Compare `decided_at` as UTC instants independently of array order;
creation before or at consumption remains valid, including explicit later
consumption of historical outputs. Claim references remain bound to their original
entity IDs. Validation cannot establish that IDs were never recycled outside
the supplied bundle or that historical bytes match retained authority.

Qualified answers reject incoming as well as outgoing unresolved contradictions
with active claims; rejected/retracted or superseded contradictory records do not
block qualification. Partial may cite candidates with full evidence/review/handling
companions, never promote them. Unknown omits claims/evidence/review references.
Nonqualified states require an explicit limitation in addition to the caveat.
Any denied source/code state requires a denied envelope; any stale source/code
state refuses evidence-bearing fields, even on partial answers. A code reference
precludes `not-applicable`. Denied limitations must be generic: the validator
enforces representable-field removal but cannot certify arbitrary free-text
privacy. Authorized runtime checks must happen before loading sensitive records.

The empty JSON pointer denotes the whole exact JSON document; nonempty pointers
start with `/` and use only `~0`/`~1` escapes. Review ID syntax is
`rev-[a-z0-9-]{1,100}`; source IDs follow bounded existing S/I/O-number syntax.
Malformed JSON-shaped values fail with BusinessContextError; no input mutation,
filesystem/database access or clock measurement occurs in the three APIs.
