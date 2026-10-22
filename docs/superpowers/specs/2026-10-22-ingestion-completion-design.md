# Ingestion completion and first-installation closure design

**Audience:** maintainers, operators and implementation/review agents.
**Date:** October 22, 2026.
**Status:** Owner-approved October 22, 2026, with one amendment: **no
second-installation obligation.** Nothing in this work may require a second
installation to exist, be installed into, be kept byte-identical with this
repository, or be available for a test to pass. The installation boundary is
proven against a clean copy of this repository's own release allowlist, and
October 20 acceptance item 5 reads "this toolkit and its clean copy run without
Docling for their existing workflows."
**Planning home:** [development and ingestion roadmap](../../../ROADMAP.md).
**Amends:** [Docling ingestion adapter design](2026-10-20-docling-ingestion-design.md),
whose intent, interfaces, source checks, runtime boundary, rendition identity,
publication rules and limits remain in force except where stated below.

## Why this amendment exists

The October 20 design assumed two live installations: this reusable toolkit and
the originating corpus installation that the capability was first requested for.
Three things changed afterwards.

**The first installation's proof of concept concluded.** It is configured,
stable and still in use, and it is no longer a development target. Its lessons
belong here instead. It receives no further ports from this repository, and this
repository holds no record of its contents, paths, stages or history. If it ever
needs rebuilding, that is its own operator action using its own retained intake,
not an upgrade performed from here.

**The pinned OCR assets are unobtainable on this host.** Official RapidOCR
provisioning resolves through a model host that this network answers with an
HTML block page. The worker correctly refuses those bytes, so real conversion
acceptance never ran. The blocker is asset availability, not worker behavior.

**Persisted store identity no longer has to match the first installation.**
The October 20 work inherited a compatibility constraint that the proof of
concept's conclusion removes.

This document decides what "complete" now means for the remaining ingestion
work, and nothing else. It does not reopen the approved adapter architecture.

## Decision 1: re-pin the OCR assets to reachable published models

Replace the three unobtainable RapidOCR checkpoints with the published ONNX
conversions of the same PaddleOCR lineage, pinned by content hash:

| Required asset | Bytes | SHA-256 |
|---|---|---|
| `RapidOcr/en_PP-OCRv3_det_infer.onnx` | 2423224 | `f139598bc2af4e4b6fe98dec11574e30edfdd91fc94ac1425c18ace3bd5a866b` |
| `RapidOcr/ch_ppocr_mobile_v2.0_cls_infer.onnx` | 585532 | `e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c` |
| `RapidOcr/en_PP-OCRv3_rec_infer.onnx` | 8967018 | `ef7abd8bd3629ae57ea2c28b425c1bd258a871b93fd2fe7c433946ade9b5d9ea` |

The layout and table assets are unchanged and already valid locally.

These three were chosen by measurement, not availability alone. The Chinese
PP-OCRv4 pair from the same publisher converts and repeats byte-identically but
loses inter-word spacing, which cannot satisfy the plan's required negation
assertion. The English PP-OCRv3 trio recovers exact spacing, digits and the
negation. Both candidate probes are recorded in the task ledger.

Provisioning stays a separate explicit operator step with no network access
during conversion. The documented procedure pins the publisher revision as well
as the content hashes, and verifies the hashes before first use. A fetched file
whose hash does not match is an actionable provisioning failure, never an input.
No alternate host, proxy or security-control bypass is introduced; if a future
host cannot reach the documented publisher, provisioning fails visibly and the
operator supplies the assets another approved way.

Because the selected OCR models are part of conversion identity, `profile()`
reports version 2 and names the three selected models explicitly instead of
implying them from a size label. Renditions prepared under version 1 keep their
own identity; they are different runs, not silently equivalent ones. Nothing
else in the fixed profile changes, and no check is relaxed: the asset manifest
still binds observed relative paths, sizes and hashes, and is still verified
before and after inference.

## Decision 2: prove the installation boundary generically, not by porting

The October 20 plan's third task copied the common files into the first
installation and wrote that installation's bridge. That task is withdrawn. No
remaining obligation requires a second installation to exist, to receive files,
to stay byte-identical with this repository, or to be present for any test to
pass.

What it was protecting is still worth proving: that `docling_bridge.py` is the
only installation-specific file, and that a second installation can supply its
own policy, catalog and output locations without editing the common engine.
Prove that against a clean copy of this repository's own release allowlist.

A clean-copy acceptance test installs the allowlisted files into a temporary
directory, initializes empty stores there, writes a local policy whose eligible
source roots differ from this checkout's, and runs the complete explicit
prepare, verify and register lifecycle as a subprocess inside that copy. It
asserts that the copy's own policy decides eligibility: a source placed under
this checkout's eligible root is refused there, and the same source placed under
the copy's own eligible root is accepted and registered into the copy's
previously empty catalog. It also asserts that removing the bridge breaks the
lifecycle with an actionable error while leaving the common files untouched,
which is what makes the bridge the single installation-specific file rather than
a claim that it is. Real inference may be substituted in this test, since what
it proves is the installation boundary, not conversion fidelity; fidelity is
proven once, separately, by Decision 1's fixture matrix. This extends the
existing clean-copy and package-check pattern, so it runs in ordinary
continuous verification rather than depending on a second checkout existing.

The bridge contract is therefore documented as a supported porting seam: three
functions, their exact signatures, what each must enforce, and what a new
installation must not change. A future installation of a different project is a
configuration and bridge exercise against that documented seam. Nothing in this
release claims such an installation has been exercised.

Two further commitments in the October 20 integration section lapse with the
port. This repository is no longer the development home of a copy maintained
elsewhere, so no versioned copy is delivered to another installation; and the
explicitly selected retained-member input, which existed only as that
installation's surface, is not part of this release. Receipt originals remain
the only selection, and `--artifact` is not implemented here. A later
installation that needs member selection adds it in its own bridge.

## Decision 3: rename persisted store identity

The three SQLite `application_id` values decode as `FCAT`, `FREV` and `FCTX`.
The leading letter is a fossil of the first installation's program name, and
[decision 0001](../../decisions/0001-project-identity.md) kept it only because
renaming would have cost a migration against that installation's populated
stores. That constraint is gone, and every store in this repository is empty.

Rename them to `LCAT` (`0x4C434154`), `LREV` (`0x4C524556`) and `LCTX`
(`0x4C435458`). Regenerate the schema-only snapshots from the changed
definitions. Re-initialize this repository's empty stores and rebuild the
derived index; verify emptiness by direct row count first, and do not treat a
snapshot as a backup.

The authority strings `catalog-metadata-only` and `review-events-only`, the
record locator syntax, receipt identifiers and selection-manifest fields are
not renamed. They describe what the stores are and carry no fossil. Do not
expand this rename into them. Also leave the ordinary word "corpus" alone: it
is this project's generic term for a body of collected documents, it names no
program, and it appears in persisted retrieval classifications such as
`absent-corpus`.

A store carrying a superseded identity must refuse to open with a message that
names the superseded identity and states that explicit migration or
re-initialization is required. "Not a catalog store" is loud but uninformative,
and an operator holding an older store deserves to know which case they are in.
No automatic migration, conversion or in-place rewrite is added, and refusing an
older store is not permission to rebuild an authority store anywhere else.

## Decision 4: record the closure, remove the remaining traces

Record the proof of concept's conclusion as a decision, generically: what it
proved, what it cost, which lessons this repository adopted, and the explicit
statement that it is not maintained from here and supplies no content, identity
or path to this repository. Amend decisions 0001 and 0003 where this design
changes what they recorded, and update the roadmap's capability state to match
shipped behavior rather than planned ports.

Remove the two remaining references that identify the first installation's
program: a schema-identity example in decision 0001 and an intake-stage mention
in the October 20 plan's final task. Replace them with the generic description
already used everywhere else. Scanning for traces is part of the work; finding
none is the expected result, not a skipped check.

## What completion requires

Carry forward acceptance items 1 through 5, 7 and 8 of the October 20 design
unchanged, with one substitution inside item 5: "both installations run without
Docling for their existing workflows" becomes "this toolkit and its clean copy
run without Docling for their existing workflows."

Item 6 is replaced. Instead of common files matching across two installations,
the clean-copy acceptance of Decision 2 must pass, and the bridge seam must have
its own regression coverage.

Three items are added:

1. The re-pinned assets convert the fixture matrix faithfully, twice, with no
   network access, and the recorded hashes match what provisioning fetched.
2. Renamed store identity is present in code, regenerated snapshots and
   freshly initialized stores; a store carrying the superseded identity is
   refused with an actionable message.
3. The closure record, amended decisions and roadmap state describe only what
   exists, and no trace identifying the first installation remains.

Completion is not claimed while any real conversion, repeatability or
registration acceptance case remains blocked. Synthetic fixtures establish
adapter behavior, not accuracy on any particular real document; measuring that
needs an explicit source selection and its own authorization.

## Out of scope

Unchanged from the October 20 design: Office and ODF formats, transcripts, audio
and video, additional image formats, VLM conversion, generated picture
descriptions, remote inference, arbitrary plugins, archive expansion, discovery
and copy services, watchers, scheduling, automatic review, intake-stage
advancement, retrieval selection, automatic indexing of Docling output, graph
integration and cohort automation.

Added to the exclusions: porting anything into the first installation,
rebuilding it, reading its contents, migrating a populated store of any
identity, and installing this capability into any other project's installation.
Those are separate decisions with their own authorization.
