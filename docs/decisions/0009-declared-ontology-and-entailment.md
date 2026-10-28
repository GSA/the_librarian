# 0009 - A declared ontology, and which half of a reasoner is adopted

**Audience:** maintainers and operators.
**Date:** October 28, 2026.
**Status:** Accepted. The declared vocabulary, its contract binding and
constraint checking are implemented here. Entailment is **not** implemented and
is gated on [0006](0006-relationship-traversal.md); no projection exists to
carry it.

## Context

A question arose whether the optimal form of this project is an ontology
system, and, following from that, whether the rules refusing inference should
be removed — on the argument that refusing inference makes sense for code
analysis but not for an ontology.

Both halves of that question are worth taking seriously, and both are
answered below. The first half is largely already settled by the code: an
ontology exists. The second half is where the existing rules were genuinely
too coarse, and this decision corrects them.

## An ontology already exists; it is simply not declared

`tools/catalog/catalog_store.py` enforces, at write time, a closed domain
model:

- Five record kinds: receipt, source, artifact, policy, relationship.
- Exact required field sets per kind — unknown or missing fields are refused,
  not ignored.
- Typed identifier syntax per kind: `D` for receipts, `S`/`I`/`O` for source
  classes, `A-` for artifacts, `R-` for relationships, `corpus-policy` for
  policy.
- A controlled relation vocabulary of exactly four terms: `version-of`,
  `duplicate-of`, `rendition-of`, `same-event`.
- Endpoint typing: a relationship links artifacts or sources, and the source
  category must agree with its identifier prefix.
- Structural constraints: no self-links, no cyclic artifact parents,
  referential integrity on endpoints.

That is a T-Box. It is expressed in Python control flow rather than in a
vocabulary document, which has three costs. The model cannot be read without
reading the validator; a deployment cannot extend it without editing core
code, which [AGENTS.md](../../AGENTS.md) already forbids for domain constants;
and the constraints are enforced as scattered `require()` calls rather than as
a stated, reviewable whole.

So "should this be an ontology system" is not a question about adopting
something new. It is a question about **declaring what is already enforced**.
That part is adopted.

## The refusal rules were three different rules wearing one label

"No inference" names three distinct prohibitions in this repository, which
were never separated and do not share a justification.

1. **`agents/librarian.md` — no answering from memory or inference when the
   corpus is silent.** This governs the *agent*, not the data model. It is an
   anti-fabrication rule attached to the absence classifications
   (`absent-corpus`, `absent-partial`, `present-uncombined`). Ontology work does
   not touch it. **Unchanged.**

2. **[0006](0006-relationship-traversal.md) — model-synthesized node classes
   (concept, rationale, theme) are barred.** This is the code-analysis case.
   It bars *abduction*: a model guessing an abstraction that no artifact
   states. Such output is unauditable, irreproducible and carries no premise
   anyone can check. **Unchanged, and correctly so.**

3. **[0006](0006-relationship-traversal.md) precondition 3 — "no inferred
   facts" in the projection.** This was generalized from (2) without noticing
   that deduction is not abduction. **This is the rule that was too broad.**

The objection to (3) is correct on the merits. Deduction over declared axioms
is analytic: if `rendition-of` is declared transitive, then from `A rendition-of
B` and `B rendition-of C`, the conclusion `A rendition-of C` adds no
information about the world. It unpacks what was already asserted, is
truth-preserving by construction, and is fully reproducible from its premises.
Treating that as equivalent to a model inventing a "theme" node is a category
error.

## Evaluation: split the reasoner

A reasoner does two separable jobs, and they get opposite answers here.

### Constraint checking is adopted

Disjointness, cardinality, and domain/range checking add no facts. They detect
that asserted records contradict each other or violate the declared model, and
refuse the write. This is precisely what the validator already does by hand —
acyclicity, endpoint typing, self-link refusal, exact field sets — only
generalized and stated once.

This half is strictly aligned with the project's existing behavior of refusing
rather than repairing. It is adopted and implemented.

### Entailment is computed, never stored

Entailment is not rejected because it is unsound. It is rejected *as authority
content* for two reasons that have nothing to do with logic.

**Entailment is truth-preserving but not attribution-preserving.** The unit of
record here is an attributed assertion: a relationship record carries
`provenance` spans, a `scope`, and a `basis` of `reported`, and traces to a
receipt. A conclusion composed from two premises recorded on different dates,
from different sources, under different receipts has no honest author, no date
and no single source. Its `scope` is undefined, because the scope of a composed
relation is not the composition of its premises' scopes. The system's purpose
is to point at canonical evidence; a record that cannot name its evidence
defeats that purpose even when it is true.

**Axioms apply retroactively to an append-only store.** The catalog is
immutable hash-chained revisions. Entailments are not: they change when the
axiom set changes, and adding one premise today mints conclusions over records
chained months ago and reviewed under a different closure. Materializing them
would mean either rewriting history or accumulating conclusions that silently
contradict the axioms in force when they were written. Neither is compatible
with the store's integrity model.

There is a third, subtler reason to keep axioms visible rather than ambient.
Declaring `same-event` transitive or `duplicate-of` symmetric is itself an
empirical modeling claim, often wrong at the edges — transitive `duplicate-of`
is how a cluster of mutual non-duplicates appears. Inference risk does not
disappear when axioms are declared; it **relocates** from per-edge claims that
are visible and attributable to a global axiom set applied invisibly at read
time. That is an argument for declaring axioms in a reviewable, versioned
document, which this decision does, and against letting them silently
manufacture authority records, which it does not.

The correct home for entailment therefore already exists in the architecture:
the derived, rebuildable, stale-refusing projection described in 0006. A
conclusion computed there is explicitly not evidence, is deletable, and
rebuilds from authority. It must carry its premise chain, so that reading an
entailed edge hands back the attributed records it came from. That is not a
weakening of "a reference is not support" — it is that rule implemented,
because the premise chain *is* the route to canonical evidence.

### Open-world semantics are already present

One formal property is worth naming rather than importing. The open-world
assumption — that unasserted does not mean false — is already this system's
absence contract: an empty store is "no supplied records", not proof of
absence, and the three absence classifications describe corpus coverage rather
than the world. Nothing needs to change; the existing qualifiers are the
semantics, stated operationally.

The corollary constrains entailment usefully: under an open world, deduction
can only ever add edges, never rule one out. A reasoner can therefore never
justify an absence claim, and must never be presented as improving coverage.

## Decision

1. **Declare the vocabulary.** The relation terms, their legal endpoint
   types, their declared algebraic properties and the model's constraints move
   out of Python constants into versioned configuration that the validator
   reads. The shipped default reproduces today's enforced behavior exactly.

2. **Keep authority single-grade.** Catalog relationship records continue to
   require `basis` of `reported`. Entailed edges are refused by the catalog
   writer. No authority store gains a derived record.

3. **Adopt constraint checking.** Declared disjointness, cardinality and
   endpoint-type rules are enforced at write time, replacing equivalent
   hand-written checks and extending them where the declaration is richer.

4. **Defer entailment to the projection.** Declared properties such as
   transitivity and symmetry are *recorded but not applied* in this release.
   They become computable only inside a derived projection, which does not
   exist and remains gated on 0006's preconditions — including precondition 1,
   still unsatisfied. An entailed edge, when one is ever produced, carries its
   premise chain and is labeled as entailed at every surface.

5. **Correct 0006 precondition 3.** It is narrowed from "no inferred facts" to
   bar *synthesized* facts and *stored* entailments, while permitting computed,
   premise-carrying, labeled entailment inside the projection.

6. **Bind the vocabulary to the store contract.** A store records the
   vocabulary it was written under. A store written under one vocabulary is
   refused under a different one, with an explicit migration required, per
   [0003](0003-store-compatibility.md). A store predating this binding is
   readable only under the shipped default vocabulary; it is never silently
   adopted into a custom one.

## Consequences

- The enforced domain model becomes reviewable as a document rather than
  recoverable only by reading the validator.
- A deployment can extend relation terms without editing core code, at the
  cost of an explicit store migration — the cost is deliberate, because an
  extended vocabulary makes a store unreadable to an installation lacking it.
- No existing store is invalidated by this change: the default vocabulary
  hashes to the behavior those stores were already written under.
- Entailment remains unavailable. This decision makes it *admissible* under
  stated conditions; it does not make it present, and nothing here should be
  described as a reasoning capability.
- A graph engine remains unjustified. Declaring an ontology is a modeling
  change and supplies no new argument for a storage engine; 0006's scale and
  operational findings are untouched.
