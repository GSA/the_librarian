# Extraction workflows

## Ordinary strict UTF-8 text

A registered `.txt` original can use the stdlib extraction lifecycle without
Docling or model assets (substitute your registered `D<N>` receipt ID for `D1`):

```sh
PY=.venv/bin/python
$PY -m tools.extraction.extract prepare D1 --format text
$PY -m tools.extraction.extract verify D1 --format text
$PY -m tools.extraction.extract register D1 --format text \
  --actor operator --expected-state CATALOG_STATE_TOKEN
```

The format accepts strict UTF-8 with an optional UTF-8 BOM. It preserves CRLF,
LF and CR terminators, blank lines, final-line state and original byte offsets.
Malformed UTF-8, UTF-16/32 BOMs, NUL, excessive line records and oversized
renditions fail visibly; no replacement decoding or charset guessing occurs.
Preparation and registration create a deferred rendition only. They do not
review, qualify, select, index or publish its text. After a separate supported
catalog update marks the generated child `indexed`, an operator may explicitly
select it in `config/selection.local.json` with `format: "text"`, its exact
rendition hash, receipt ID and maintained Markdown context. Retrieval then uses
stable `#line-N` locators and reports original byte ranges and terminator kinds.
Empty and BOM-only renditions have no records and are refused for selection.

## Optional Docling ingestion

Docling ingestion is an explicit local `prepare` / `verify` / `register`
lifecycle. It produces a deferred machine OCR/layout rendition. It does not
review a source, index the rendition, select it for retrieval, advance intake,
or authorize publication.

## Dedicated environment

Use a separate environment; the parent toolkit remains stdlib-only for this
workflow:

```sh
python3.14 -m venv tools/extraction/.local/venv-docling
PIP_ONLY_BINARY=:all: tools/extraction/.local/venv-docling/bin/python -m pip install \
  --index-url https://download.pytorch.org/whl/cpu \
  torch==2.14.1+cpu torchvision==0.29.1+cpu
PIP_ONLY_BINARY=:all: tools/extraction/.local/venv-docling/bin/python -m pip install \
  -r tools/extraction/requirements-docling.txt
tools/extraction/.local/venv-docling/bin/python -m pip check
```

`requirements-docling.in` lists the direct dependencies, pinned exactly because
they define the accepted conversion profile. `requirements-docling.txt` is the
full resolved lock for Python 3.14 on Linux x86_64 CPU, including
`pypdfium2==5.14.0`; do not install a separate overriding PDFium version. The
worker reads the lock and refuses (`runtime_mismatch`/`missing_dependency`) any
installed version that differs. To change a pin, edit the `.in` file, then
regenerate the lock with the procedure in its header. Other platforms or Python
versions need their own resolved lock. Never substitute another project's
environment.

## Model assets

Provisioning is an explicit online operator step, separate from offline
conversion. Use the dedicated interpreter's `huggingface_hub` to fetch the
selected layout and accurate-table files from their publishers at immutable
revisions (no ONNX layout, fast-table or unrelated models are needed):

```sh
tools/extraction/.local/venv-docling/bin/python - <<'PY'
from pathlib import Path
from huggingface_hub import hf_hub_download

root = Path("tools/extraction/.local/models")
models = [
    ("docling-project/docling-layout-heron",
     "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8",
     "docling-project--docling-layout-heron",
     ["config.json", "preprocessor_config.json", "model.safetensors"]),
    ("docling-project/docling-models",
     "fc0f2d45e2218ea24bce5045f58a389aed16dc23",
     "docling-project--docling-models",
     ["model_artifacts/tableformer/accurate/tm_config.json",
      "model_artifacts/tableformer/accurate/tableformer_accurate.safetensors"]),
]
for repo, revision, directory, files in models:
    for filename in files:
        hf_hub_download(repo, filename, revision=revision,
                        local_dir=root / directory)
PY
```

Provision the three OCR files from the published `SWHL/RapidOCR` ONNX
conversions at revision `1cfba2e90fc938db55889873735088de210cc173` into
`tools/extraction/.local/models/RapidOcr/` under these names. Fetch directly
from the named publisher; an unreachable publisher is a provisioning failure,
not permission to use a proxy or alternate host.

| File | Bytes | SHA-256 |
|---|---:|---|
| `en_PP-OCRv3_det_infer.onnx` | 2423224 | `f139598bc2af4e4b6fe98dec11574e30edfdd91fc94ac1425c18ace3bd5a866b` |
| `ch_ppocr_mobile_v2.0_cls_infer.onnx` | 585532 | `e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c` |
| `en_PP-OCRv3_rec_infer.onnx` | 8967018 | `ef7abd8bd3629ae57ea2c28b425c1bd258a871b93fd2fe7c433946ade9b5d9ea` |

The publisher-relative paths are respectively
`PP-OCRv4/en_PP-OCRv3_det_infer.onnx`,
`PP-OCRv1/ch_ppocr_mobile_v2.0_cls_infer.onnx`, and
`PP-OCRv3/en_PP-OCRv3_rec_infer.onnx`. Provision them explicitly:

```sh
tools/extraction/.local/venv-docling/bin/python - <<'PY'
from pathlib import Path
from shutil import copyfile
from huggingface_hub import hf_hub_download

root = Path("tools/extraction/.local/models/RapidOcr")
root.mkdir(parents=True, exist_ok=True)
for filename in ("PP-OCRv4/en_PP-OCRv3_det_infer.onnx",
                 "PP-OCRv1/ch_ppocr_mobile_v2.0_cls_infer.onnx",
                 "PP-OCRv3/en_PP-OCRv3_rec_infer.onnx"):
    cached = hf_hub_download("SWHL/RapidOCR", filename,
                            revision="1cfba2e90fc938db55889873735088de210cc173")
    copyfile(cached, root / Path(filename).name)
PY
```
Do not rename the parent layout/table directories or omit configuration files.

Verify before first use:

```sh
wc -c tools/extraction/.local/models/RapidOcr/*.onnx
sha256sum tools/extraction/.local/models/RapidOcr/*.onnx
```

Run the `inspect` command below after provisioning: it validates all eight
required files against the approved sizes and SHA-256 identities in
`docling_worker.py`'s `ASSET_PINS`, fingerprints the layout/table/OCR bytes and
enforces runtime pins. A same-name compatible substitute is refused before
model loading; changing approved assets requires an explicit code/profile
update and renewed acceptance.
Retain its JSON output with the local installation record; compare the asset
manifest when reproducing an installation. Layout/table content is identified
by that observed manifest, not an assumption that a mutable branch is stable.
Weights and environments are ignored operational files and are never packaged.

## Commands

```sh
PY=.venv/bin/python
DOCLING_PY=$PWD/tools/extraction/.local/venv-docling/bin/python
MODELS=$PWD/tools/extraction/.local/models

$PY -m tools.extraction.docling_extract inspect \
  --artifacts-path "$MODELS" --python "$DOCLING_PY"
$PY -m tools.extraction.docling_extract prepare D1 --format pdf \
  --artifacts-path "$MODELS" --python "$DOCLING_PY"
$PY -m tools.extraction.docling_extract verify D1 --format pdf \
  --artifacts-path "$MODELS" --python "$DOCLING_PY"
$PY -m tools.extraction.docling_extract register D1 --format pdf \
  --artifacts-path "$MODELS" --python "$DOCLING_PY" \
  --actor operator --expected-state CATALOG_STATE_TOKEN
```

Use `--format image` only for `.png`, `.jpg` or `.jpeg`. PDF and those image
formats are the complete supported set. There is no member-selection option.
The registered original must remain present, eligible under current source
policy, hash-consistent, and within its inherited audience and handling rules.

## Limits and failures

The Docling source limit is 64 MiB; PDFs allow at most 100 pages and
32,000,000 pixels per page. Worker and final results are limited to 64 MiB,
diagnostics to 64 KiB, address space to 16 GiB, and wall time to 600 seconds.
Conversion always runs in `unshare -Urn` with offline model flags, reading a private
read-only copy of the model assets (about 0.15 s and 380 MB of temporary disk per
running conversion, removed with the operation) so a change to the shared model
directory mid-run cannot affect it. Native
(stdlib) formats parse inside the toolkit process without that sandbox, so
their source limit is lower: 16 MiB, with renditions limited to 64 MiB.

Common codes are actionable: `missing_dependency` means provision the dedicated
interpreter; `missing_assets` or `invalid_assets` means provision and verify the
pinned files; `isolation_unavailable` means user/network namespaces are not
available; `timeout` or `output_limit` means the bounded operation did not
complete; `mutation` means source, worker, policy or assets changed and the
operator must retry after reconciling them. Worker-specific format, encryption,
geometry and incomplete-conversion errors are non-successful. Preserve saved
`registration.json` after a committed-but-unconfirmed write and retry the exact
same request.

## PDF and image text in cohort intake

Cohort intake (`python -m tools.intake.cohort ingest|qualify`) reads `.pdf` files
page by page (`tools/extraction/pdf_pages.py`) and `.png`, `.jpg` and `.jpeg`
files whole (`tools/extraction/pdf_image.py`) through the pinned environment
above. Simplest reader first:

- **PDFs are unstapled into pages.** `pdf_page_worker.py inventory` reads each
  page's embedded text layer, image-object count and size with the locked
  `pypdfium2`, run `-I` in the Docling interpreter inside `unshare -Urn` (no
  network). A page keeps its **text layer** when it has at least 200 letters or
  digits that are at least half of its non-space characters, or when it draws no
  image and the document has at least 200 letters or digits overall (a short
  page without images has nothing to OCR). Tables and layout are not
  reconstructed; that loss is accepted.
- **Every other page is OCRed alone.** `pdf_page_worker.py render` draws just
  that page as a grayscale PNG at 216 DPI (Docling's own 3x OCR scale), reduced
  to fit 98% of the 32,000,000-pixel cap, so an oversized drawing sheet is read
  smaller rather than refused (`render_scale` records it). The PNG goes through
  the unchanged `docling_adapter` and worker.
- **Images** always use Docling OCR/layout through `pdf_image.py`. Docling sizes
  an image page from its DPI metadata; the bytes handed to Docling get a DPI
  (`method.ocr_dpi`, at least 72) at which the 3x OCR raster fits the 32,000,000
  pixel cap, so ordinary screenshots are not refused. Pixel data is unchanged
  and the rendition names the original bytes.

Each rendition is an ordinary `A-extract-<run>` extraction with a page-located
`lines` payload (`{"line", "page", "text"}`). The machine-readable marker is
`payload.format`: a PDF is `pdf-pages` (`method.kind: "pdf-pages"`) and an image
`ocr` (`method.kind: "docling-ocr"`). A `pdf-pages` payload records every page's
outcome in `pages`: `{"page", "outcome": "text-layer", "text_chars",
"image_objects"}`, `{"page", "outcome": "ocr", "textless", "attempts",
"render_scale", "confidence"}` or `{"page", "outcome": "failed", "code",
"attempts"}`, plus `page_count` and `unread_pages`. A failed page leaves one
placeholder line in its place, for example `[Page 7 not extracted:
extract-timeout after 2 attempts]`, and a warning. Retrieval labels the union of
the pages' kinds (`confidence.page_kinds`): text-layer pages add
`converted-format`, OCR pages `machine-ocr`, and failed or unread pages
`incomplete-rendition` naming them; an image `ocr` is `machine-ocr`.
PDF renditions written by `pdf_image` (`pdf-text`, `ocr` from a PDF) stay valid
and keep their labels. The `pdf-pages` identity hashes `pdf_pages.py`,
`pdf_page_worker.py`, `pdf_image.py` (shared helpers), the lock, the Docling code
and profile, the thresholds, render settings and limits, and the instance policy;
the image identity is unchanged. Changing any of them is a new run.

**PDF hardening and retry.** The inventory and every OCR page are separate
units, each in its own child process (`python -m tools.extraction.pdf_pages unit`)
in a private directory below `data/extractions/.local/work/` that is removed
afterwards, with a 16 GiB address-space limit and 600 CPU seconds. The inventory
has 330 wall seconds; an OCR page 300 (the slowest measured page took 160 under
4-way load), ending its Docling conversion 15 seconds earlier. Each unit runs as PID 1 of its own PID namespace (`unshare -Ur --pid
--fork --kill-child`), so when it ends or is killed every process it started dies
with it, including workers in sessions of their own; its process group is killed
too, and every unit alarm fires before its parent's. The page worker itself is limited to 2 GiB, 300 CPU and wall seconds.
**A unit that times out or crashes is retried once**; a page that still fails, or
is refused (`extract-pixel-limit`, `extract-malformed-page`, ...), becomes a
failed page with its code and attempts, and the rest of the document is kept.
Budgets per document replace the old 100-page refusal: at most 5,000 pages are
read (the rest are recorded in `unread_pages` with code `extract-page-budget`
and one placeholder line; pages after 24 MiB of text-layer text likewise, with
`extract-output-limit`), 200,000 line records including placeholders (the page
that would not fit and all later ones fail as `extract-record-limit` behind one
range placeholder), and 7,200 seconds of OCR units (later OCR pages fail as
`extract-time-budget` without running). The document is refused only when it
cannot be opened (`extract-encrypted`, `extract-malformed-source`, an inventory
`extract-timeout` after two attempts), when every page failed (their shared
code, else `extract-all-pages-failed`) or when no page has any text
(`extract-empty-conversion`). Sources are limited to 64 MiB.

**Image hardening.** Every image converts in its own child process
(`python -m tools.extraction.pdf_image child`) in a private directory below
`data/extractions/.local/work/` that is removed afterwards, with an address-space
limit of 16 GiB, 1,800 CPU seconds and a 900-second wall clock (its process
group is killed on expiry); inside it Docling's own 600-second limit ends an OCR
conversion first, so a file that times out costs about 20 minutes over its two attempts. The text worker is limited to 2 GiB, 120 CPU seconds
and 120 wall seconds. Sources are limited to 64 MiB, PDFs to 100 pages, results
to 64 MiB and 10,000 line records. Image headers declaring more than 32,000,000
pixels are refused before any decoder runs (cohort `over-limit`, code
`pixel-limit`). A timeout or crashed worker is retried once; every refusal is
then a blocked extraction stage with an `extract-<code>` code, for example
`extract-encrypted` (any password or security handler), `extract-malformed-source`,
`extract-page-limit`, `extract-pixel-limit`, `extract-record-limit`,
`extract-output-limit`, `extract-empty-conversion`, `extract-timeout`,
`extract-cpu-limit` or `extract-worker-crashed`. A refused file never stops the
cohort or changes another file's ledger entries, and a rerun does not convert it
again.

**Concurrency.** Each chunk's conversions run before its ledger stages, PDFs
then images, `--extract-concurrency N` child processes at once (default 2): for
PDFs, inventory and page units of all the chunk's documents share that bound. The ledger's prepare stage then
finds the published rendition (or the file's refusal) and does not convert
under the ledger. Budget about 1.5 GiB resident per OCR conversion. With
`--batch-size 1` conversions run one at a time inside the prepare stage.

**Provisioning an instance.** Install the environment and models above at
`tools/extraction/.local/venv-docling` and `tools/extraction/.local/models` below
the instance root, or point `LIBRARIAN_DOCLING_PYTHON` and
`LIBRARIAN_DOCLING_ARTIFACTS` at an existing installation. Check it with:

```sh
.venv/bin/python -m tools.extraction.pdf_image status --root ROOT
```

The MCP preflight reports the same as `pdf_image_runtime` (informational). Until
the runtime is provisioned, cohort `classify` and `ingest` keep these files
`needs-runtime` with code `docling-not-provisioned`; after provisioning, the next
`ingest` or `qualify` reads them. Use
`pdf_pages probe --root ROOT --work DIR [--concurrency N] FILE...` (PDFs; one
JSON line per document with every unit's seconds and peak resident memory) or
`pdf_image probe --root ROOT --work DIR FILE...` (images) to time conversions
without storing anything. `DIR` must be a private scratch directory.

**Upgrading from whole-file PDFs.** A PDF whose extraction already succeeded or
was refused under `pdf_image` keeps that outcome and is not converted again (its
`pdf-text`/`ocr` rendition stays valid). Re-reading a refused one page by page
needs a new linked request (`correction_of`); there is no cohort shortcut yet.
Let in-flight cohorts finish registering before upgrading: a PDF prepared by
`pdf_image` but not yet registered is refused as `changed_extraction_identity`.

## MIME HTML (Confluence export)

`--format mhtml` reads a text-suffix file whose content is a MIME message
(for example a Confluence "Exported From Confluence" `.doc`). Only non-attachment
`text/html` parts are decoded (strict transfer encoding and charset) and converted
to plain-text lines; images, attachments, links and scripts are ignored. Malformed
MIME, an unknown encoding or charset, or a message with no HTML part is refused
with a stable code (`mime-malformed`, `mime-encoding`, `mime-charset`,
`mime-no-html`, `mime-empty`) instead of being read as raw text. The payload
records `method` (`mhtml-html-text` and its version) and the parts read.
Select it in `config/selection.local.json` with `format: "mhtml"`.

## Converted documents (Office, RTF)

`tools/extraction/convert.py` turns DOCX, XLSX, PPTX, VSDX and RTF into a derived Markdown line
rendition (`payload.format: "converted"`). It needs only the standard library: no
virtual environment, pins or lock. Cohort intake picks the format from content (package part names,
`{\rtf`), not the suffix. Each file converts in a separate `python -I` process
(`convert_worker.py`) with a 300 s wall clock, an 80 MiB output cap and rlimits. It reads zip
members through count, size and ratio caps and refuses unsafe, duplicate or encrypted
members. It parses XML with expat and refuses any DTD or entity. Output is capped at 100,000
lines, 10,000 rows, 64 columns and 1,000 characters per sheet cell.

- DOCX: the Title style as `#` and Heading N as N+1 `#` (capped at six), numbered and bulleted lists, tables as Markdown tables,
  text boxes as `>` quotes, then footnotes, endnotes and comments. Headers, footers, tracked
  deletions and images are omitted; images are counted in `fidelity.dropped`.
- PPTX: `## Slide N` in presentation order, the title as `###`, other text as bullets, tables,
  then `#### Notes` (slide numbers and dates skipped).
- XLSX: `## Sheet: name`, each 50 rows as a Markdown table under the first row as header with
  the spreadsheet row number. Shared strings, booleans, dates, times of day and elapsed durations (`[h]:mm`) are resolved; formulas
  show their cached value.
- VSDX: `## Page: name` with one bullet per shape text; geometry and connectors are omitted.
- RTF: text with `\par` lines, literal and `\'hh` bytes decoded by `\ansicpg`, `\u` characters (surrogate
  pairs combined), and table cells joined
  with `|`. Hidden destinations (font tables, `\*` groups, pictures, metadata) are skipped.
- Legacy `.doc`/`.xls`/`.ppt`/`.vsd` (`office-legacy`): converted first with
  `soffice --headless --convert-to` (private profile, extraction home, same timeout) to
  DOCX/XLSX/PPTX/ODG, then as above. This only happens when LibreOffice's `soffice` is on `PATH`.
  It is not installed or required; without it the file stays `needs-reader`
  (`needs-converter-runtime`).

`payload.conversion` records `source_format`, `via`, `converter` (`librarian-convert`), `version`,
`code_sha256` (both converter files), `lock_sha256` (`null`), `runtime` and `fidelity`
(`truncated`, `dropped`, `notes`). The engine identity covers the converter code and policy (and
the `soffice` version), and cohort extraction requests carry a `-c<12 hex>` suffix of the same
  identity (like MHTML's `-mN`), so changing either extracts again, even under `--trust-stat`. The catalogued source remains the original
bytes. Failures are blocked stages with a code: `container-invalid`, `container-limit`,
`unsafe-member`, `encrypted-document`, `zip-bomb`, `xml-dtd`, `xml-invalid`, `xml-limit`,
`ooxml-invalid`, `rtf-invalid`, `conversion-empty`, `conversion-limit`, `conversion-timeout`,
`source-changed`, `converter-runtime-failed` or `legacy-conversion-failed`. The `converted`
kind is labelled low confidence (`converted-format`). Select these renditions with
`format: "converted"`; they retrieve by `#line-N` like text.
