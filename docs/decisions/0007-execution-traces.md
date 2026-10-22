# 0007 - Agent execution traces: diagnostics, not memory

**Audience:** maintainers and operators.
**Date:** October 22, 2026.
**Status:** Accepted as a boundary; the kept scope is now narrowed and
implemented. The memory half stays declined. Coverage-gap diagnostics are
implemented: search returns an absence classification, and
`tools/diagnostics/retrieval_log.py` is an opt-in, off-by-default body-free log
with report and prune commands. Reads are counted as bare events, but no
search-to-read ratio is reported - see *No ratio is derived from the read
count*. The dead-weight requirement is **withdrawn** - see *Dead weight
withdrawn*. Retrieval quality stays unimplemented and is largely unobservable
from a stdio adapter.

## Context

This corpus exists to supply other agents with business context gathered from
users, Google Drive and Confluence. A reasonable extension is to record what
those agents did with the context they were given: what was asked, what was
returned, what was used, what was produced. The stated motivation was
cross-session memory and self-learning.

An earlier draft of the roadmap specified that as an ingestion source, with a
submission endpoint, a versioned trace schema, producer instructions and a
retrieval path. On review the proposal contained two different capabilities
that were being treated as one, with very different value and very different
risk. This record separates them.

## Decision

### Traces as agent memory are declined

An agent reading its own prior conclusions back as context inverts the purpose
of this corpus.

The property that makes this system worth building is that every answer is
traceable to a specific Drive file or Confluence page, with a locator, a
content hash, a version and a review record, and that unsupported claims are
refused rather than softened. An agent's recollection has none of that. It has
no author, no upstream owner and no version, and it becomes wrong silently the
moment the underlying page changes - with nothing to detect the drift, because
there is no upstream to compare against.

Stored at the same retrieval surface as sourced material, unverified
recollection competes with it, and the consuming model cannot reliably
distinguish "a Confluence page states this" from "an agent concluded this last
Tuesday." Qualification labels help a careful reader and do not survive
summarization. The failure mode is specific and compounding: an agent reads
its own earlier unverified conclusion, treats it as established, records a new
conclusion that depends on it, and the corpus accumulates confident claims
with no external support. A corpus whose authority is provenance must not also
store content whose only provenance is that an agent once said it.

The self-learning case is also already served, and better. An agent finding
that genuinely deserves to persist has a route: the contribution and
disposition contract in `agents/contributions.md`, which requires the finding,
its basis, the evidence with locator and inspected scope, the affected
canonical context, handling limits and attribution - and which ends in an
actual review. That path is deliberately slower than appending a trace. The
friction is what makes the output trustworthy. A trace endpoint would be a
lower-friction parallel route that bypasses review, and it would be used
precisely because it is easier.

### Body-free retrieval diagnostics are worth building

The genuinely missing capability is different: the corpus cannot observe its
own performance. Three operator questions are unanswerable today.

- **Coverage gaps.** Which questions produced no usable result? This is the
  prioritized ingestion backlog, derived from real demand rather than guesswork
  about which Drive folder to connect next. Today a question the corpus cannot
  answer leaves no record anywhere. A zero-result search can be classified
  body-free by probing its terms against the index vocabulary on the existing
  read-only connection, which separates "never collected" from "collected but
  not findable"; the roadmap records the mechanism and its limits.
- **Retrieval quality.** How often does search surface the record that was
  ultimately used, versus the agent reaching it another way? This measures the
  search path rather than the corpus, and separates "we lack the document"
  from "we have it and could not find it."

A third candidate, dead weight, was specified in the original proposal and is
withdrawn below.

### Dead weight withdrawn

The original proposal asked which selected documents are never returned or
never read, so that selection could be pruned. That requirement is withdrawn.

It rests on an assumption this project does not accept: that a smaller corpus
is better. Removing a selected document is only an improvement if keeping it
demonstrably costs something, and nothing here demonstrates that. The index is
small, reads are local, and FTS5 ranking is not degraded in any measured way by
the presence of documents nobody has asked about yet. Against that, the cost of
being wrong is asymmetric: a document dropped for lack of traffic is
unavailable the first time someone needs it, and the gap it leaves is then
indistinguishable from a source that was never collected.

The measurement is also weak where it would be used. Observed traffic on the
populated installation is 29 searches and 11 reads across three days, touching
four distinct documents out of 67 selected. At that volume "never returned"
and "not yet asked about" are the same observation, and the signal would be
read as license to prune material that was correctly selected.

Finally, the signal costs the most privacy of the three. Attributing reads
means the log accumulates which specific documents an agent read - a usage
profile of the corpus, and of the asking agent's line of inquiry, over a
selection that includes restricted and procurement-scoped material. That is a
different artifact from a classification histogram, and it is not justified by
a pruning benefit nobody has shown.

What is kept instead is deliberately unattributed: reads are counted as bare
events, with no locator recorded. The log cannot say what was retrieved, and so
it cannot be used to prune.

### No ratio is derived from the read count

An earlier version of this decision reported reads per answered search as a
retrieval-quality indicator. That number is withdrawn from `report` while the
raw counts are kept.

The ratio cannot distinguish an unconvincing search excerpt from an agent that
read enough from the search result and never needed the full record. Those two
cases call for opposite responses - fix extraction or ranking, versus change
nothing - and a single scalar that conflates them invites a decision it cannot
support. A caveat string beside the number does not fix that; the number is
what gets quoted.

The counts themselves stay, because they are unambiguous facts about observed
traffic and cost nothing to keep. If a later interface can distinguish a
search result that satisfied a question from one that forced a follow-up read,
the ratio becomes meaningful and can be derived from exactly these records
with no schema change. Until then `report` states that no ratio is reported and
why.

These are observations about the system's own behavior. They are not claims
about the world, they are read by operators rather than fed back to agents,
and they do not need to be trusted as evidence to be useful.

### Why these are safe where memory is not

The distinction is enforced structurally, not by labeling:

- A diagnostic is never retrieval context. Not indexed, not searchable
  alongside corpus records, not returned by read tools.
- A diagnostic is not an authority. Not in the catalog store, not in the
  review-event store, never countable as a review, never support for a
  qualified answer.
- A diagnostic is body-free. No query text, no prompt, no model output, no
  source excerpt. A record carrying any of them is refused, not truncated.

The body-free constraint deserves emphasis because it is also the privacy and
handling answer. A trace that quotes returned source text inherits the
restrictions of every source it quotes and is the likeliest place for
restricted content - or a credential - to land in a file nobody classified.
Excluding bodies removes that class of problem rather than managing it.

The precedent exists. The originating installation carries a 247-line
`metrics.py` that already enforces a closed field set rejecting bodies and
queries outright, a fixed operation vocabulary, exception class names instead
of error messages, a per-record byte budget, private file modes and locked
appends, and that is explicitly documented as "never a catalog or review
authority." Diagnostics should extend that, not introduce a second format.

## Consequences

- An agent's useful findings persist only through the contribution contract and
  review. Sessions remain stateless with respect to the corpus. This is
  intended.
- The ingestion backlog becomes demand-driven rather than speculative, which is
  the main practical gain available here.
- Selection gains no pruning signal, by choice. Documents leave the selection
  through an operator decision with a stated reason, never through a traffic
  threshold.
- Reads are counted but never attributed, so the log cannot become a usage
  profile of the corpus or of an asking agent.
- Diagnostics are opt-in, per-instance and off by default. Logging failures are
  reported rather than swallowed and never become job authority.
- Diagnostics need a stated size and retention policy with deliberate verified
  pruning from the first version. They accumulate with no upstream owner, so
  they do not inherit the no-automatic-deletion rule that protects authority
  stores and retained originals.
- Nothing derived from diagnostics changes selection, ranking or qualification
  automatically. A gap report is input to an operator decision. Any automatic
  use requires its own decision record.
- No diagnostic content ships in the distribution; see
  [0005](0005-source-onboarding.md).

## Implementation as built

What exists now, in the terms this decision set:

- `context_index.search` adds an `absence` object to a zero-hit result only.
  It reports the classification, the query-term count, the absent-term count,
  the number of sections matching any single term, the scope (whole selection
  or one path), and a caveat stating the report describes what is indexed and
  is not evidence that a fact does not exist. Classification uses single-term
  `MATCH` probes on the same read-only connection: no new table, no index
  schema bump, no rebuild. No query term, locator or excerpt appears in the
  object.
- `tools/diagnostics/retrieval_log.py` writes the classification without the
  question. Its field set is closed, so query text, locators and excerpts are
  unrepresentable rather than filtered. It additionally refuses records that
  are internally inconsistent - a classification disagreeing with its own term
  counts, an empty outcome reporting hits, an error type on a successful
  outcome - which closes the gap where a caller could over-report without
  storing a body.
- `search` is recorded in full; `get` and `get_many` are recorded only as an
  unattributed read count, which the writer enforces by refusing any locator,
  count or classification field on a read record. `status`, `catalog` and
  `review_events` write nothing. The log therefore reports how many reads
  happened and cannot reconstruct which records were read.
- The log is off unless the operator passes `--diagnostics-log <path>.jsonl`.
  A write failure raises instead of returning the search result, so the
  reported-not-swallowed rule holds in practice rather than on paper.
- `report` separates the collection-gap rate from the findability-gap rate,
  reports raw search and read counts, and states the signal's own limits in its
  output - including that a read count is not a basis for pruning selection and
  that no search-to-read ratio is reported. `prune --keep N` is explicit and
  reports before and after counts. Nothing
  reads the log to change selection, ranking or qualification.

This satisfies the coverage-gap requirement and withdraws dead weight. No
retrieval-quality conclusion is drawn. The recurrence question
below is still open.

## Open questions

- Whether recurrence of the same unanswerable question can be clustered without
  storing its terms. Classifying a zero-result search is solved without storing
  the query - the index vocabulary distinguishes "no term is known to the
  corpus" from "all terms are known but never co-occur", which is the
  collection-versus-findability distinction the roadmap needs. Clustering
  repeats is not solved: a hash of the normalized term set would group them,
  but the index vocabulary is itself a dictionary against which short term sets
  can be reversed, so hashing is not a privacy-preserving shortcut. The
  consequence is a counted histogram rather than a ranked list of missing
  subjects.
- Whether use-versus-return is observable from a stdio adapter at all, or
  whether it requires cooperation from the calling agent - which would make the
  signal self-reported and substantially weaker. The adapter hands back text
  and learns nothing about what the model did with it. Read counts are
  recorded, but they do not separate a weak search result from a question the
  search result already answered, so no quality figure is published from them.
- Whether gap reporting belongs in a diagnostic log or in the existing intake
  tracking, which already owns ingestion backlog and disposition.
- Whether any write surface is exposed through MCP. Doing so would end the
  adapter's read-only property and needs its own decision; a local writer
  invoked by the harness would not.

## Revisit if

A concrete, recorded case shows an agent repeating substantial work that the
contribution contract could not have captured - and the proposal for storing it
can state, in advance, how a consuming model is prevented from treating the
stored recollection as sourced evidence. Absent that second part, this stays
declined.
