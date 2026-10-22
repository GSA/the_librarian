# Docling ingestion adapter design

**Audience:** maintainers, operators and implementation/review agents.
**Date:** October 20, 2026.
**Status:** owner-approved October 20, 16:09 EDT; subagent-driven implementation authorized at 16:19 EDT.
**Planning home:** [development and ingestion roadmap](../../../ROADMAP.md).

## Intent and agreed scope

Add Docling underneath the existing ingestion system to make scanned and
layout-sensitive documents readable without replacing the catalog, existing
extractors, review records, maintained knowledge or qualified reader.

The owner requested a specification and implementation using the Superpowers
design-first process, and chose both installations: this reusable, data-empty
toolkit and the originating corpus installation. Develop the common adapter
here and explicitly port the reviewed component into the other installation.
Neither installation imports executable code from the other's checkout.
Do not copy instance content or operational stores into this distribution.

The owner approved this complete design on October 20 at 16:09 EDT. The choices
below originated as proposed maintainer defaults and are part of that approved
design. At 16:19 the owner selected subagent-driven execution using Kata,
roborev pre-commit review and in-place implementation in both repositories.

## First-release behavior

An operator selects an already registered receipt, chooses Docling explicitly,
prepares a private rendition, inspects its scope/warnings, and registers it
using the catalog's current state token. Registration independently repeats
conversion and requires the exact prepared bytes. Standalone verification
remains available. The same command shape works in both installations.

Supported inputs are PDFs with embedded text, scanned pages or mixed content,
and single-image PNG/JPEG files. Enable layout analysis, reading order,
table-structure extraction and a fixed local OCR engine through Docling's
standard document pipeline. OCR may supplement embedded text; it must not
silently substitute a different source or pipeline after failure.

Explicit initial exclusions:

- Office/ODF documents, transcripts, audio/video and additional image formats.
  Their existing extraction paths remain available and unchanged.
- VLM conversion, generated picture descriptions, remote OCR/model APIs,
  arbitrary plugins, external-link retrieval and macro/script execution.
- Archive expansion, discovery/copy services, watchers, unattended scheduling
  and automatic review, intake-stage advancement or retrieval selection.
- Automatic indexing of Docling JSON. The existing reader continues to accept
  only its explicitly supported rendition formats. Docling output is available
  for deliberate local inspection and review/incorporation; adding a qualified
  Docling-record reader is a separate decision.
- Graph or OKF integration. Those proposals are not dependencies.

Do not interpret conversion success as visual equivalence, source authenticity,
substantive review, verified program claims or operational acceptance.

## Integration and compatibility

Add `tools/extraction/docling_extract.py` as the explicit entry point. Do not
change either installation's existing `extract.py`: earlier rendition identities
and parser-fingerprint checks must continue to mean what they meant.

Keep the conversion worker and common Docling lifecycle code identical in both
repositories. A small installation-specific bridge supplies the existing
catalog module, source-eligibility rules, handling boundary and extraction
output root. The toolkit is the common component's development home; the
corpus receives an explicit, versioned copy, not a symlink or runtime dependency
on another installation.

Reuse supported catalog validation, state-pinned reads, writer locking,
private publication and transactional submission APIs. Reuse unchanged
extraction helpers when their contracts fit; do not fork a catalog writer or
weaken a legacy extractor to accommodate this adapter.

For the toolkit, the first release selects receipt originals only. In the
corpus, also support an explicitly selected retained member through its existing
artifact/ancestor metadata API; that is already an ingestion surface there.
Do not port the broader member API or an unrelated cohort engine into the
toolkit merely for parity.

Illustrative command interface, with placeholders rather than supplied records:

```sh
python3 tools/extraction/docling_extract.py prepare RECEIPT \
  --format pdf --artifacts-path MODEL_DIRECTORY --python DOCLING_PYTHON
python3 tools/extraction/docling_extract.py verify RECEIPT \
  --format pdf --artifacts-path MODEL_DIRECTORY --python DOCLING_PYTHON
python3 tools/extraction/docling_extract.py register RECEIPT \
  --format pdf --artifacts-path MODEL_DIRECTORY --python DOCLING_PYTHON \
  --actor ACTOR --expected-state CURRENT_STATE
```

`--root` remains a global option before the subcommand. The corpus bridge also
accepts `--artifact ARTIFACT` on each operation. Image selection uses
`--format image`, accepting `.png`, `.jpg` and `.jpeg` with matching validated
content. An unsupported flag or input fails visibly, never selecting a default
extractor. Exact flag spelling may change during planning if existing CLI
conventions require it; documented behavior may not silently change.

No existing default route is redirected to Docling. No change to the existing
cohort selection schema is included. Operators can call the explicit command
for selected sources; orchestration can be added under its own later contract.

## Source and handling checks

Before reading source bytes, validate the catalog and resolve the exact
receipt/artifact membership. Reject missing, excluded or ineligible sources,
excluded ancestors, paths outside the installation's allowed original roots,
symlinks and non-regular files. Preserve each installation's existing handling
and filename/subtree rules.

Observe and compare source hash/size with registered metadata. A registered
hash mismatch requires identity/version reconciliation, not overwriting.
Read one bounded source snapshot for conversion. Recheck source bytes,
handling/eligibility, catalog state and engine/model identity before publication
and registration. Mutation cannot publish a mixed-source rendition.

An archive member is eligible only when already retained and registered; a
missing/deleted/excluded member stays ineligible. Preserve the corpus's existing
distinction between a deliberately deleted package wrapper and an excluded
ancestor. No extraction command assigns receipt or source identities.

## Runtime, dependencies and network boundary

Keep Docling optional and isolated from the MCP and lightweight extraction
environments. Add a dedicated, exactly pinned dependency manifest and document
creation of a separate supported Python environment. Choose compatible released
versions during implementation, then record the resolved runtime versions;
ordinary extraction and initialization must not import Docling or need models.

Model provisioning is a separate explicit operator action using public model
identifiers, never source documents. Document the required model assets and
local provisioning procedure. It must be possible to provision once and run
conversion without network access.

Conversion runs in a child process inside a Linux user/network namespace with
network access disabled. This prerequisite was demonstrated available on the
development host. Failure to establish isolation blocks conversion; no
network-enabled retry follows. This is an egress boundary, not a claim of full
filesystem or malicious-parser isolation.

Use an explicit CPU-only standard-pipeline profile and explicit local OCR
backend. Disable remote services, VLMs, picture descriptions, external plugins
and unrequested enrichments. Missing dependencies, unsupported Python,
missing/changed assets and unavailable OCR backends are actionable errors,
not empty results or a fallback to another extraction method.

Bind all model/configuration assets used by the profile to an observed manifest
of relative paths, sizes and SHA-256 values. Resolve only assets within the
selected model root. Verify the binding before and after conversion. Do not
claim that a mutable remote model name identifies installed model bytes.

## Rendition identity and output

Write a new adapter-specific versioned envelope under each installation's
existing private extraction home, retaining `extraction.json` as the primary
artifact. Do not overwrite older runs or change their schemas.

The identity binds source receipt/artifact/source IDs, source hash/size/path,
audience/handling, adapter and bridge code hashes, installation policy binding,
Docling/runtime versions, model-manifest hash and complete conversion profile.
Changing any of these creates a distinct run. Local absolute model/interpreter
paths are execution settings, not portable source identity.

Preserve the structured Docling document, including available hierarchy, item
types, tables and original page/bounding-box provenance. Also retain page
inventory, textless-page information, available conversion-confidence grades,
explicit omissions and warnings. Missing confidence is unknown, not a favorable
score; confidence does not establish table accuracy or source truth.

The JSON is the retained rendition. Do not make a second authoritative Markdown
copy. Generated page images, base64 image payloads and machine captions are not
included in the first-release output. Rendering needed internally for
OCR/layout does not authorize a claim of full visual equivalence.

Use stable canonical JSON serialization. Include no elapsed times, timestamps,
temporary paths or other incidental worker state in identity-bound content.
Do not remove meaningful source fields to make comparisons pass. Exact schema
keys and the numeric adapter/schema version are implementation choices to be
specified and regression-tested in the plan.

Use deterministic CPU settings where supported. `verify` and `register`
independently repeat conversion and compare the complete canonical rendition
byte-for-byte. A mismatch fails closed, preserving the prepared run; do not
replace independent verification with a hash of the same saved output.
If the chosen profile cannot satisfy this contract, report the blocker before
claiming an implemented ingestion path. Do not introduce a weaker contract
without a separate owner decision.

## Publication, registration and failure recovery

Create private directories/files with the existing 0700/0600 protections.
Stage output privately; publish atomically without replacement and remove only
this operation's temporary files. Model caches and source/rendition bodies
are not telemetry.

`prepare` writes no catalog records. `register` verifies the prepared rendition,
saves its exact request before submission and uses the supported catalog writer
with a fresh expected state. Register one generated-rendition artifact and
one scoped `rendition-of` relationship, inheriting the selected original's
audience and handling references. Retain stable source identities.

The child remains deferred, not indexed. Record extraction as a partial
OCR/layout/text rendition, not an approved visual equivalent. Do not promote
the parent's coverage, create review events or move intake stages. Existing
record shapes and backup/retention policies suffice; no authoritative schema
migration is planned.

Use stable request IDs and saved request/completion records. An ambiguous or
postcommit failure must identify possible/actual commit and the recovery route.
An identical committed retry is idempotent and cannot overwrite newer parent
metadata. Uncommitted metadata drift requires reconciliation.

Malformed/encrypted sources, conversion errors, partial conversion, exceeded
budgets, crashes and timeouts return a nonzero result with a bounded diagnostic.
Do not publish/register an incomplete rendition. Blank pages within an
otherwise usable conversion are enumerated; an entirely unreadable/empty
conversion is not a successful OCR result. Low confidence stays visible as a
warning for inspection, not an automatic review or acceptance decision.

Proposed initial per-operation limits are 64 MiB input, 100 PDF pages,
32 million pixels per rasterized page/image, 64 MiB serialized rendition and
600 seconds wall time. Bound worker memory and threads explicitly; the exact
memory limit is an implementation choice based on the supported CPU runtime.
Validate dimensions/page count before expensive processing where supported;
enforce output and wall/memory limits during processing, not just afterward.
Refuse over-budget inputs rather than silently dropping pages or reducing
scope. These limits apply only to the new adapter, not existing extractors.

## Acceptance evidence

Use temporary isolated workspaces, structural fixtures and public/toolkit text;
never add customer documents, records, model weights or private configuration
to code, fixtures, design documents or the release allowlist.

Completion requires:

1. Actual local Docling conversion, not only mocked workers: an embedded-text
   PDF, an image-only PDF, a mixed PDF, PNG and JPEG, with text, two-column order
   and a simple table exercised across the set. Check expected strings
   (including a negation), numeric values, row/column associations and valid
   source page/bounding-box references.
2. Repeated preparation produces the same rendition bytes; independent
   verification and registration succeed; source/model/profile changes create
   new identities or explicitly refuse mutation during an operation.
3. Policy/ancestor exclusions, malformed/encrypted/oversize inputs, unsupported
   formats, missing assets, namespace failure, partial/empty conversion,
   resource limits and altered prepared output fail visibly without
   registration or an unqualified success.
4. Registration retry/recovery, stale-state refusal, concurrent mutation,
   private permissions and non-overwrite behavior preserve existing contracts.
   Prove no automatic review, intake or retrieval-selection changes occur.
5. Existing extraction tests and legacy fingerprint checks still pass. Original
   wrapper bytes remain unchanged. Both installations run without Docling for
   their existing workflows.
6. Common component files match across the two installations at handoff. Each
   bridge's real policy and source-selection behavior has regression coverage.
7. Documentation, roadmap and explicit release allowlist reflect shipped
   behavior. The reusable source package remains data-empty. No existing
   corpus originals or populated authoritative stores are processed merely
   to demonstrate installation.
8. A fresh implementation reviewer receives the original request, approved
   design, scoped diffs and actual results. Unresolved blockers and unmeasured
   real-document fidelity/resource needs remain explicit.

Synthetic checks establish adapter behavior, not accuracy for every scan.
Representative private-source evaluation needs an explicit source selection
and the installation's handling permissions; it is not automatic authorization
from this design.

## Next approval and implementation plan

The October 20 fresh builder check reported "nothing important left" to resolve
before planning. This is a design-completeness check, not conversion or
implementation evidence.

The owner subsequently approved the design. The
[implementation plan](../plans/2026-10-20-docling-ingestion.md) sequences the
remaining work using the Superpowers writing-plans process, including the
subsequently requested Kata and roborev delivery workflow. Execution is approved
in place with fresh implementation/review agents. Keep the generic plan here;
the corpus records only its own scoped direction, integration and verified
outcome.
