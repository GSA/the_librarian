# Retrieval-quality evaluation

`python -m tools.evals --root ROOT` measures how well one instance's published
index finds what its cases expect, so a change to review policy, qualification,
confidence rules, selection or the engine can be shown not to degrade retrieval.
It reads the index exactly as the CLI and MCP adapter do (`load_manifest`,
`readonly`, `freshness`, `search`) and changes no store, selection, ranking or
qualification. Cases and scorecards are instance data: they live only in ignored
local files and never ship in the package.

## Cases (`retrieval-eval-cases-v1`)

The default file is `<root>/config/evals.local.json` (`--cases` overrides it), a
strict JSON object `{"schema": "retrieval-eval-cases-v1", "cases": [...]}`.
Duplicate keys, unknown fields, duplicate ids and contradictory expectations (for
example a must-not path that is also an expected document) are refused before
anything runs. Each case has:

- `id` (stable; lowercase letters, digits and `. _ : -`), `origin` (`authored` or
  `generated`), literal `query` (the same rules as `search`) and `k` (1..50, the
  search limit and the rank cutoff).
- Optional `tags`, a `path` filter and a `low_confidence` mode (`include`,
  `last`, `exclude`; default `include`).
- At least one expectation:
  - `expect_paths` / `expect_locators`: every listed document path or exact
    locator must appear within the top `k`.
  - `expect_confidence` (`normal` or `low`): the level of the first expected hit.
  - `must_not_paths`: none of these documents may be returned.
  - `expect_absent: true`: no hits, optionally with `expect_absence` naming the
    classification (`absent-corpus`, `absent-partial`, `present-uncombined`, or
    `low-confidence-excluded` when every match was excluded).
- `generator` (`{method, seed}`) on generated cases only.

## Generating a baseline

`generate --seed S --count N --absences M` (defaults 1, 200, 10) adds cases
without hand-authoring:

- **Known-item cases** sample documents stratified by role, confidence level and
  rendition format (proportional quotas, at least one per stratum when there is
  room). For each, a seeded section's indexed title and text supply up to
  `--terms` (default 3) distinctive terms: title terms first, then the rarest
  terms, with terms found in only one section last; stopwords, document-path words, hash-like
  tokens and terms in more than 1,000 sections are never used. The case expects
  that document within `k` (default 10) at its current confidence level.
  A query already used, or matching more than 200 sections (near-duplicate
  documents), is passed over for another section of the document; a document
  with no usable section within 25 tries is skipped and counted. The sample
  therefore leans toward documents that have something distinctive to find.
- **Absence cases** are random consonant tokens expected to be `absent-corpus`.

Every term of a generated query comes from one indexed section, so the document
is always findable; generation never looks at rank, so a baseline is not tuned to
pass. Generation reads the published index only (no freshness check; `run` does
that). It keeps authored cases and other seeds' cases, replaces only cases
generated with the same seed (so rerunning a seed on an unchanged index rewrites
an identical file), refuses an id clash, and writes the file 0600.

## Running and the scorecard (`retrieval-eval-scorecard-v1`)

`run [--tag T ...]` checks freshness once, runs every selected case in process,
then checks freshness once more. A stale index, or one that becomes stale during
the batch, is refused and no scorecard is written; rebuild first. A claim
correction recorded or resolved during the batch is refused the same way, so one
scorecard never mixes correction states. A case whose
search raises (for example a `path` no longer in the selection) is an `error`.

The scorecard is written 0600 under `<root>/tools/evals/.local/runs/` (0700,
ignored) or to `--output`. It is body-free: case ids, statuses, closed failure
codes (`expected-not-in-top-k`, `confidence-mismatch`, `must-not-returned`,
`unexpected-hits`, `absence-mismatch`, `error:<Exception>`), ranks, counts and
confidence levels; no query, path, locator, excerpt or error message. It holds:

- **Aggregates** overall and per origin and tag: pass rate; over known-item cases
  `hit_at_1`, `hit_at_k`, `recall_at_k` (mean fraction of expected targets found)
  and `mrr`; `must_not_violations` (cases); `absence_accuracy`;
  `confidence_match_rate`; the confidence distribution of all returned hits and of
  the first expected hits.
- **Timings**: each freshness check, total and per-search percentiles.
- **Provenance**: index metadata (engine fingerprint, manifest hash, policy hash,
  rendition identity, a hash of the corpus-root binding, document, section and
  confidence counts), the standing policy file's SHA-256, id, version and status
  (`--standing-policy`, default `config/standing-policy.local.json`; never its
  scope), the claim-correction store state, the cases file SHA-256 and the
  toolkit's Git commit and dirty flag.

## Comparing

`compare BASE NEW [--tolerance METRIC=VALUE ...] [--max-regressions N] [--json]`
reports per-metric deltas, which provenance inputs changed, and per-case
regressions (passed in BASE, not in NEW) and improvements over the cases both
runs share. It exits 1 when a rate drops by more than its tolerance (defaults:
`pass_rate`, `hit_at_k`, `recall_at_k` 0.01; `hit_at_1`, `mrr`,
`confidence_match_rate` 0.02; `absence_accuracy` 0), when `must_not_violations`
or `errors` rise at all, or when regressions exceed `--max-regressions` (default
0). Unreadable input, or a scorecard missing a gated metric or with malformed
cases, exits 2 rather than comparing as OK.

## Operating procedure

1. Before a standing-policy change, rereview, qualification or confidence-rule
   change, engine upgrade or publish, make sure the index is fresh and run
   `run` to capture a baseline scorecard. Keep the cases file unchanged between
   the two runs (`compare` reports a changed cases hash).
2. Make the change and rebuild or publish the index.
3. `run` again, then `compare BASELINE NEW`. Investigate every regression; an
   intended change (for example a deferral now published as low confidence)
   shows up as named case regressions and changed provenance, and is accepted
   explicitly with `--tolerance` or `--max-regressions` rather than by editing the
   baseline.

## Limits

- Generated known-item cases measure whether the index can still find what it
  could find, not whether answers are correct; authored cases from real
  questions carry that weight. Very short records (for example one line each)
  yield short queries and ranking among near-duplicates, so the absolute figures
  depend on the corpus; compare runs, not instances.
- A generated case expects the document's confidence level at generation time;
  a deliberate re-review changes it and is reported as a regression.
- Each freshness check costs what a `status` costs on the instance.
