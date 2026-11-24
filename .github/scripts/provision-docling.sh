#!/usr/bin/env bash
# Provision the pinned Docling runtime and model assets under tools/extraction/.local
# (the layout tools/extraction/README.md describes). Used by .github/workflows/docling.yml;
# every asset is re-verified against the size and SHA-256 pins by `docling_extract inspect`.
set -euo pipefail
unset PYTHONPATH  # a stray path must not satisfy pip requirements the venv itself lacks

root="${1:-$PWD}"
cd "$root"
venv=tools/extraction/.local/venv-docling
models=tools/extraction/.local/models

if [ ! -x "$venv/bin/python" ]; then
  python3.14 -m venv "$venv" 2>/dev/null || python3 -m venv "$venv"
fi
export PIP_ONLY_BINARY=:all:
if ! "$venv/bin/python" -m pip check >/dev/null 2>&1 || ! "$venv/bin/python" -c "import docling" 2>/dev/null; then
  "$venv/bin/python" -m pip install --index-url https://download.pytorch.org/whl/cpu \
    torch==2.14.1+cpu torchvision==0.29.1+cpu
  "$venv/bin/python" -m pip install -r tools/extraction/requirements-docling.txt
fi
"$venv/bin/python" -m pip check

"$venv/bin/python" - <<'PY'
from pathlib import Path
from shutil import copyfile
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
        hf_hub_download(repo, filename, revision=revision, local_dir=root / directory)

ocr = root / "RapidOcr"
ocr.mkdir(parents=True, exist_ok=True)
for filename in ("PP-OCRv4/en_PP-OCRv3_det_infer.onnx",
                 "PP-OCRv1/ch_ppocr_mobile_v2.0_cls_infer.onnx",
                 "PP-OCRv3/en_PP-OCRv3_rec_infer.onnx"):
    cached = hf_hub_download("SWHL/RapidOCR", filename,
                             revision="1cfba2e90fc938db55889873735088de210cc173")
    copyfile(cached, ocr / Path(filename).name)
PY

# Fails (non-zero) when any asset or dependency differs from the pins.
.venv/bin/python -m tools.extraction.docling_extract inspect \
  --artifacts-path "$PWD/$models" --python "$PWD/$venv/bin/python" >/dev/null
echo "Docling runtime provisioned and verified"
