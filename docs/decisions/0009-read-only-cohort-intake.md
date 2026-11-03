# 0009 - Read-only cohort intake and model-assisted standing review

**Audience:** maintainers and operators.
**Date:** November 1, 2026.
**Status:** Implemented (classification, local-path intake, ledger extraction,
model findings and sample evaluation); bulk review remains gated per instance.

## Context

An instance must be populated from a folder of real files that the operator may
only read. Files arrive in many formats, with names that are not safe
identifiers, alongside web assets, archive copies and operating-system
sidecars. Review of thousands of renditions cannot be a per-file human click,
but parser or model success must never stand in for review.

## Decision

- Every inbox file receives exactly one counted disposition. Formats the
  toolkit can already read are admitted (strict UTF-8 text, Gemini transcripts,
  ODT); PDF and images wait for a verified Docling runtime; formats with no
  reader are `needs-reader` and get their own work item. Nothing is dropped
  silently, and a suffix is never format proof.
- Admitted files are copied in through the shipped upload path as
  `local-path` origins: a system label plus the percent-encoded relative
  locator and pinned hash. A local copy never claims a remote identity or
  version, and the inbox is never written.
- Text, transcript and ODT extraction runs through the durable ledger like
  Docling, pinning source and extractor identity and refusing drift.
- Standing review of extraction renditions requires inspection of every
  payload record and warning. A model reviewer's reply is recorded as findings
  exactly as reported; the standing policy, not the model, decides
  qualification.
  Internal contradictions and evidence the rendition itself lacks block
  qualification; references to cited material outside the rendition are
  recorded as advisory (`standing-findings-v2`) and do not block. Earlier
  `standing-findings-v1` records remain valid.
- The reviewing model is chosen by a body-free sample evaluation over the
  configured provider's models, including synthetic garbled and
  instruction-injection controls, and model traffic stays within that provider.
- Before a bulk review the operator records, privately, the chosen model and
  rationale, a token estimate extrapolated from the sample, and an approved
  budget ceiling; afterwards, actual usage against the estimate.

## Consequences

Classification and extraction can run unattended and repeatably, while review,
selection and publication stay explicit, budgeted and attributable. Counts and
evaluation reports carry no document text; per-file reports with paths and
hashes are private instance data. Model-reviewed findings are only as complete
as the model reports; incomplete inspection stays visibly pending.
