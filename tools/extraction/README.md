# Extraction workflows

## Ordinary strict UTF-8 text

A registered `.txt` original can use the stdlib extraction lifecycle without
Docling or model assets:

```sh
PY=.venv/bin/python
$PY tools/extraction/extract.py prepare D-receipt --format text
$PY tools/extraction/extract.py verify D-receipt --format text
$PY tools/extraction/extract.py register D-receipt --format text \
  --actor operator --expected-state CATALOG_STATE_TOKEN
```

The format accepts strict UTF-8 with an optional UTF-8 BOM. It preserves CRLF,
LF and CR terminators, blank lines, final-line state and original byte offsets.
Malformed UTF-8, UTF-16/32 BOMs, NUL, excessive line records and oversized
renditions fail visibly; no replacement decoding or charset guessing occurs.
Preparation and registration create a deferred rendition only. They do not
review, qualify, select, index or publish its text. Selected retrieval of this
format is not yet implemented.

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

These pins describe the Python 3.14 Linux CPU environment, including
`pypdfium2==5.14.0`; do not install a separate overriding PDFium version.
Follow the requirement file's current two-step comments if its pins change.
Never substitute another project's environment.

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

$PY tools/extraction/docling_extract.py inspect \
  --artifacts-path "$MODELS" --python "$DOCLING_PY"
$PY tools/extraction/docling_extract.py prepare D-receipt --format pdf \
  --artifacts-path "$MODELS" --python "$DOCLING_PY"
$PY tools/extraction/docling_extract.py verify D-receipt --format pdf \
  --artifacts-path "$MODELS" --python "$DOCLING_PY"
$PY tools/extraction/docling_extract.py register D-receipt --format pdf \
  --artifacts-path "$MODELS" --python "$DOCLING_PY" \
  --actor operator --expected-state CATALOG_STATE_TOKEN
```

Use `--format image` only for `.png`, `.jpg` or `.jpeg`. PDF and those image
formats are the complete supported set. There is no member-selection option.
The registered original must remain present, eligible under current source
policy, hash-consistent, and within its inherited audience and handling rules.

## Limits and failures

The source limit is 64 MiB; PDFs allow at most 100 pages and 32,000,000 pixels
per page. Worker and final results are limited to 64 MiB, diagnostics to 64
KiB, address space to 16 GiB, and wall time to 600 seconds. Conversion always
runs in `unshare -Urn` with offline model flags.

Common codes are actionable: `missing_dependency` means provision the dedicated
interpreter; `missing_assets` or `invalid_assets` means provision and verify the
pinned files; `isolation_unavailable` means user/network namespaces are not
available; `timeout` or `output_limit` means the bounded operation did not
complete; `mutation` means source, worker, policy or assets changed and the
operator must retry after reconciling them. Worker-specific format, encryption,
geometry and incomplete-conversion errors are non-successful. Preserve saved
`registration.json` after a committed-but-unconfirmed write and retry the exact
same request.
