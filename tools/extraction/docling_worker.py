"""Optional local-only worker. No ML imports or provisioning at module import.

The CLI accepts JOB RESULT paths; both are private files (RESULT must not exist).
Protocol v1 has distinct inspect/convert operations. The coordinator must launch
this worker in a network namespace and enforce the profile's process budgets.
"""
import hashlib
import importlib.metadata
from io import BytesIO
import json
import math
import os
from pathlib import Path
import platform
import resource
import signal
import stat
import sys
import warnings


MIB = 1024**2
LIMITS = {
    "source_bytes": 64 * MIB, "pdf_pages": 100, "page_pixels": 32_000_000,
    "result_bytes": 64 * MIB, "address_space_bytes": 16 * 1024**3,
    "wall_seconds": 600, "diagnostic_bytes": 64 * 1024,
}
REQUIRED_ASSETS = (
    "docling-project--docling-layout-heron/config.json",
    "docling-project--docling-layout-heron/preprocessor_config.json",
    "docling-project--docling-layout-heron/model.safetensors",
    "docling-project--docling-models/model_artifacts/tableformer/accurate/tm_config.json",
    "docling-project--docling-models/model_artifacts/tableformer/accurate/tableformer_accurate.safetensors",
    "RapidOcr/en_PP-OCRv3_det_infer.onnx",
    "RapidOcr/ch_ppocr_mobile_v2.0_cls_infer.onnx",
    "RapidOcr/en_PP-OCRv3_rec_infer.onnx",
)


# Public publisher revisions and approved content identities, not local paths.
ASSET_REVISIONS = {
    "layout": ("docling-project/docling-layout-heron", "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"),
    "table": ("docling-project/docling-models", "fc0f2d45e2218ea24bce5045f58a389aed16dc23"),
    "ocr": ("SWHL/RapidOCR", "1cfba2e90fc938db55889873735088de210cc173"),
}
ASSET_PINS = (
    (3268, "fdea30805ce2f5666b147fca941dcdd27ad468e27d6ed21902207d3da056a97d"),
    (444, "cd38cd59999e7a95d68e487fbe5132df3d4e5c32a0836add57e6126ba0c4eaf1"),
    (171658996, "00333a43451945aaf89db8ca9c0a17e75d1537c17db60fdb91aa95f4c7929e0c"),
    (7060, "984e122ceb8ccf84d84c9d2882f6f2302a44b4f1e577babd6289892c36f3cffd"),
    (212758388, "2a7d6c924b3cd12fb99a09280ca9c33a89c5d60b93253617d2e088c1a40374d9"),
    (2423224, "f139598bc2af4e4b6fe98dec11574e30edfdd91fc94ac1425c18ace3bd5a866b"),
    (585532, "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c"),
    (8967018, "ef7abd8bd3629ae57ea2c28b425c1bd258a871b93fd2fe7c433946ade9b5d9ea"),
)


class WorkerError(ValueError):
    def __init__(self, message, code="invalid_input"):
        super().__init__(message)
        self.code = code


def profile() -> dict:
    """Portable identity of every selected behavior; paths are runtime inputs."""
    return {
        "version": 2, "device": "cpu", "inference_threads": 1, "stage_executor_threads": 1,
        "pipeline": "standard", "pdf_backend": "docling_parse",
        "parser": {"threads": 1, "render_scale": 1.0, "max_concurrent_results": 1,
                   "release_native_memory_every_n_pages": 1,
                   "enforce_same_font": True, "include_bitmap_images": False},
        "image_backend": "image", "ocr": {
            "enabled": True, "engine": "rapidocr", "backend": "onnxruntime",
            "languages": ["en"], "scale": 3.0,
            # Name the models that produced the rendition. The OCR stage
            # ignores model_size whenever explicit paths are supplied, so
            # reporting it would claim a selection it did not make.
            "models": {key: REQUIRED_ASSETS[index]
                       for key, index in (("det", 5), ("cls", 6), ("rec", 7))},
            "force_full_page": False, "text_score": 0.5,
            "use_det": True, "use_cls": True, "use_rec": True,
            "inter_op_threads": 1, "intra_op_threads": 1,
        },
        "tables": {"enabled": True, "mode": "accurate"},
        "cell_matching": True,
        "layout": {"engine": "transformers", "model": "layout_heron",
                   "score_threshold": 0.3, "compile_model": False,
                   "keep_empty_clusters": False, "skip_cell_assignment": False,
                   "create_orphan_clusters": True},
        "render_scales": {"preprocessing": 1.0, "ocr": 3.0, "tables": 2.0},
        "batches": {"ocr": 1, "layout": 1, "table": 1},
        "queue_max_size": 1, "batch_poll_seconds": 0.5,
        "stage_shutdown_seconds": 15.0, "reading_order_separators": True,
        "heading_inference": False, "force_backend_text": False,
        "remote_services": False, "external_plugins": False,
        "picture_description": False, "picture_classification": False,
        "chart_extraction": False, "link_fetching": False,
        "local_file_fetching": False, "enrichments": False, "retain_images": False,
        "deterministic_algorithms": True, "seed": 0,
        "required_assets": list(REQUIRED_ASSETS), "limits": dict(LIMITS),
        "asset_lock": {"revisions": dict(ASSET_REVISIONS),
                       "files": [{"path": path, "size": size, "sha256": sha}
                                 for path, (size, sha) in zip(REQUIRED_ASSETS, ASSET_PINS)]},
    }


def canonical_bytes(value, *, max_bytes=None) -> bytes:
    """Serialize completely, checking the bound during encoding, not afterward."""
    limit = LIMITS["result_bytes"] if max_bytes is None else max_bytes
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, allow_nan=False,
                               separators=(",", ":"))
    chunks, size = [], 1
    try:
        for part in encoder.iterencode(value):
            raw = part.encode("utf-8")
            size += len(raw)
            if size > limit:
                raise WorkerError("serialized result exceeds byte limit", "output_limit")
            chunks.append(raw)
    except (ValueError, TypeError, UnicodeError) as exc:
        if isinstance(exc, WorkerError):
            raise
        raise WorkerError("noncanonical or non-finite output", "invalid_output") from exc
    return b"".join(chunks) + b"\n"


def _path_chain(path):
    path = Path(os.path.abspath(path))
    for entry in (path, *path.parents):
        if entry.is_symlink():
            raise WorkerError("symlink in selected path", "unsafe_path")
    return path


def _stamp(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def _read_regular(path, limit, *, private=False):
    path = _path_chain(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise WorkerError("selected input must be a regular file", "unsafe_path")
            if private and (before.st_mode & 0o077 or before.st_uid != os.getuid()):
                raise WorkerError("job must be private and owned by this user", "unsafe_path")
            if before.st_size > limit:
                raise WorkerError("input exceeds byte limit", "source_limit")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise WorkerError("input exceeds byte limit", "source_limit")
            if _stamp(before) != _stamp(os.fstat(stream.fileno())):
                raise WorkerError("input changed during read", "mutation")
            if _stamp(before) != _stamp(path.stat(follow_symlinks=False)):
                raise WorkerError("input path changed during read", "mutation")
            return data, _stamp(before)
    except OSError as exc:
        raise WorkerError("selected file is unavailable or unsafe", "unsafe_path") from exc


def _observe_assets(artifacts_path):
    try:
        return _observe_asset_files(artifacts_path)
    except OSError as exc:
        raise WorkerError("model asset path changed or became unavailable", "mutation") from exc


def _observe_asset_files(artifacts_path):
    root = _path_chain(artifacts_path)
    if not root.is_dir():
        raise WorkerError("model assets directory is missing", "missing_assets")
    records, observation = [], []
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            entry = Path(parent) / name
            if entry.is_symlink():
                raise WorkerError("symlink in model assets", "unsafe_assets")
        observation.append((str(Path(parent).relative_to(root)),
                            _stamp(Path(parent).stat(follow_symlinks=False))))
        for name in sorted(files):
            entry = Path(parent) / name
            # Weights can exceed source/output limits; hash without retaining them.
            try:
                fd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise WorkerError("nonregular model asset", "unsafe_assets")
                    digest = hashlib.sha256()
                    while chunk := stream.read(MIB):
                        digest.update(chunk)
                    if (_stamp(before) != _stamp(os.fstat(stream.fileno())) or
                            _stamp(before) != _stamp(entry.stat(follow_symlinks=False))):
                        raise WorkerError("model assets changed during inspection", "mutation")
                    relative = entry.relative_to(root).as_posix()
                    records.append({"path": relative, "size": before.st_size,
                                    "sha256": digest.hexdigest()})
                    observation.append((relative, _stamp(before)))
            except OSError as exc:
                raise WorkerError("model asset unavailable during inspection", "mutation") from exc
    if not records:
        raise WorkerError("model assets directory is empty", "missing_assets")
    records.sort(key=lambda item: item["path"])
    manifest = {"files": records, "sha256": hashlib.sha256(canonical_bytes(records)).hexdigest()}
    return manifest, sorted(observation)


def asset_manifest(artifacts_path: Path) -> dict:
    return _observe_assets(artifacts_path)[0]


def assert_assets_unchanged(artifacts_path, manifest):
    if asset_manifest(artifacts_path) != manifest:
        raise WorkerError("model assets changed during operation", "mutation")


def _versions():
    if not (3, 11) <= sys.version_info[:2] <= (3, 14):
        raise WorkerError("supported worker Python is 3.11 through 3.14", "unsupported_python")
    requirements = Path(__file__).with_name("requirements-docling.txt").read_text()
    versions = {}
    for line in requirements.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, expected = line.split("==")
        name = name.split("[")[0]
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise WorkerError("missing optional dependency: " + name, "missing_dependency") from exc
        if actual != expected:
            raise WorkerError("pinned dependency version mismatch: " + name, "runtime_mismatch")
        versions[name] = actual
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "distributions": dict(sorted(versions.items()))}


def _environment():
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ[key] = "1"
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1",
                      TOKENIZERS_PARALLELISM="false", CUDA_VISIBLE_DEVICES="")


def _validate_assets(root, manifest):
    available = {r["path"]: r for r in manifest["files"]}
    for relative in REQUIRED_ASSETS:
        if relative not in available or not available[relative]["size"]:
            raise WorkerError("missing required model asset: " + relative, "missing_assets")
    for relative, (size, sha) in zip(REQUIRED_ASSETS, ASSET_PINS):
        record = available[relative]
        if record["size"] != size or record["sha256"] != sha:
            raise WorkerError("pinned model asset mismatch: " + relative, "invalid_assets")
    try:
        import onnxruntime as ort
        for relative in REQUIRED_ASSETS:
            path = root / relative
            if relative.endswith(".json"):
                _parse_json(_read_regular(path, MIB)[0])
            elif relative.endswith(".onnx"):
                # A bytes-only load cannot resolve external-data paths outside the root.
                options = ort.SessionOptions()
                options.intra_op_num_threads = options.inter_op_num_threads = 1
                ort.InferenceSession(path.read_bytes(), sess_options=options,
                                     providers=["CPUExecutionProvider"])
    except ImportError as exc:
        raise WorkerError("missing optional ONNX runtime", "missing_dependency") from exc
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError("invalid local model/config assets; provision valid pinned assets",
                          "invalid_assets") from exc


def inspect_runtime(artifacts_path: Path) -> dict:
    """Inspect only: no document argument, downloading, or model fallback."""
    _environment()
    versions = _versions()
    root = _path_chain(artifacts_path)
    manifest, observation = _observe_assets(root)
    _validate_assets(root, manifest)
    after, after_observation = _observe_assets(root)
    if manifest != after or observation != after_observation:
        raise WorkerError("model assets changed during inspection", "mutation")
    return {"schema": "docling-runtime-v1", "runtime": versions,
            "profile": profile(), "assets": manifest}


def _number(value, label, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or (positive and value <= 0)):
        raise WorkerError("invalid " + label, "invalid_geometry")
    return value


def validate_pages(pages, *, encrypted=False, scale=3.0):
    if encrypted:
        raise WorkerError("encrypted PDF is unsupported", "encrypted")
    if not pages or len(pages) > LIMITS["pdf_pages"]:
        raise WorkerError("invalid or excessive number of pages", "page_limit")
    _number(scale, "render scale", positive=True)
    for idx, page in enumerate(pages, 1):
        if type(page["page_no"]) is not int or page["page_no"] != idx:
            raise WorkerError("invalid page inventory", "invalid_geometry")
        width = _number(page["width"], "page width", positive=True)
        height = _number(page["height"], "page height", positive=True)
        # ceil also covers the parser's integer rounding/padding at the OCR scale.
        if (not math.isfinite(width * scale) or not math.isfinite(height * scale) or
                math.ceil(width * scale) * math.ceil(height * scale) > LIMITS["page_pixels"]):
            raise WorkerError("page raster exceeds pixels limit", "pixel_limit")
    return pages


def _inspect_source(source, name, format_name):
    if not isinstance(source, bytes) or not source:
        raise WorkerError("source must be nonempty bytes")
    if len(source) > LIMITS["source_bytes"]:
        raise WorkerError("source exceeds byte limit", "source_limit")
    if not isinstance(name, str) or not name or Path(name).name != name or "\\" in name:
        raise WorkerError("name must be a logical basename")
    suffix = Path(name).suffix.lower()
    if format_name == "pdf":
        if suffix != ".pdf" or not source.startswith(b"%PDF-"):
            raise WorkerError("PDF name/content does not match selected format", "format_mismatch")
        try:
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(source) as doc:
                encrypted = pdfium.raw.FPDF_GetSecurityHandlerRevision(doc.raw) != -1
                if encrypted:
                    raise WorkerError("encrypted PDF is unsupported", "encrypted")
                if not 0 < len(doc) <= LIMITS["pdf_pages"]:
                    raise WorkerError("invalid or excessive number of PDF pages", "page_limit")
                pages = []
                for i in range(len(doc)):
                    width, height = doc.get_page_size(i)
                    pages.append({"page_no": i + 1, "width": width, "height": height})
                return validate_pages(pages)
        except ImportError as exc:
            raise WorkerError("missing optional PDF runtime", "missing_dependency") from exc
        except WorkerError:
            raise
        except Exception as exc:
            # PDFium reports password-protected documents at open, before a handle exists.
            if "password" in str(exc).lower():
                raise WorkerError("encrypted PDF is unsupported", "encrypted") from exc
            raise WorkerError("malformed PDF", "malformed_source") from exc
    if format_name != "image" or suffix not in (".png", ".jpg", ".jpeg"):
        raise WorkerError("unsupported selected format or filename", "format_mismatch")
    try:
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(source)) as image:
                expected = "PNG" if suffix == ".png" else "JPEG"
                if image.format != expected:
                    raise WorkerError("image bytes do not match selected format", "format_mismatch")
                if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
                    raise WorkerError("animated or multi-frame image is unsupported", "multiframe")
                w, h = image.size
                if w * h > LIMITS["page_pixels"]:
                    raise WorkerError("image exceeds pixels limit", "pixel_limit")
                dpi = image.info.get("dpi")
                dpi = (72.0, 72.0) if dpi is None or dpi == (1, 1) else dpi
                if not isinstance(dpi, tuple) or len(dpi) != 2:
                    raise WorkerError("invalid image DPI", "invalid_geometry")
                dx = _number(dpi[0], "image DPI", positive=True)
                dy = _number(dpi[1], "image DPI", positive=True)
                width, height = w * 72.0 / dx, h * 72.0 / dy
                if image.getexif().get(274, 1) in (5, 6, 7, 8):
                    width, height = height, width
                pages = validate_pages([{"page_no": 1, "width": width, "height": height}])
            # PNG EXIF inspection can load pixels/close its stream. Verification
            # requires a fresh image, not that metadata-inspected object.
            with Image.open(BytesIO(source)) as image:
                image.verify()
            # Decode only after both source pixels and effective 3x OCR geometry pass.
            with Image.open(BytesIO(source)) as image:
                image.load()
            return pages
    except ImportError as exc:
        raise WorkerError("missing optional image runtime", "missing_dependency") from exc
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError("malformed image", "malformed_source") from exc


def _confidence(value, warnings_out, path="confidence"):
    if isinstance(value, dict):
        return {str(k): _confidence(v, warnings_out, path + "." + str(k))
                for k, v in value.items()}
    if path.rsplit(".", 1)[-1] in ("mean_grade", "low_grade"):
        if value in ("poor", "fair", "good", "excellent"):
            if value == "poor":
                warnings_out.append({"code": "low_confidence", "field": path, "value": value})
            return value
        if value == "unspecified":
            value = None
        elif value is not None:
            raise WorkerError("invalid confidence grade", "invalid_output")
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        warnings_out.append({"code": "unknown_confidence", "field": path})
        return {"state": "unknown"}
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        if value < 0 or value > 1:
            raise WorkerError("out of range confidence", "invalid_output")
        if value < 0.5:
            warnings_out.append({"code": "low_confidence", "field": path, "value": value})
        return value
    raise WorkerError("invalid confidence field", "invalid_output")


def _provenance(document, pages):
    inventory = {p["page_no"]: p for p in pages}
    text_pages, located = set(), 0

    def walk(value):
        nonlocal located
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for prov in value.get("prov", []):
                p = inventory.get(prov.get("page_no"))
                bbox = prov.get("bbox", {})
                if p is None or not all(k in bbox for k in ("l", "t", "r", "b")):
                    raise WorkerError("invalid source page provenance", "invalid_output")
                l, t, r, b = (_number(bbox[k], "bounding box") for k in ("l", "t", "r", "b"))
                if (bbox.get("coord_origin") not in ("TOPLEFT", "BOTTOMLEFT") or
                        not 0 <= l <= r <= p["width"] or
                        not 0 <= min(t, b) <= max(t, b) <= p["height"]):
                    raise WorkerError("bounding box outside original page", "invalid_output")
                located += 1
                table_text = any(str(cell.get("text", "")).strip()
                                 for cell in value.get("data", {}).get("table_cells", []))
                if str(value.get("text", "")).strip() or table_text:
                    text_pages.add(prov["page_no"])
            for child in value.values():
                walk(child)
    walk(document)
    return text_pages, located


def build_payload(*, document, format_name, pages, processed_pages, status,
                  confidence, errors):
    if status != "success":
        raise WorkerError("conversion status is not success", "conversion_failed")
    if errors:
        raise WorkerError("conversion reported errors", "conversion_failed")
    validate_pages(pages)
    expected = [p["page_no"] for p in pages]
    if sorted(processed_pages) != expected:
        raise WorkerError("not all source pages completed successfully", "conversion_failed")
    document_pages = document.get("pages", {})
    if sorted(int(k) for k in document_pages) != expected:
        raise WorkerError("document page inventory is incomplete", "conversion_failed")
    for p in pages:
        entry = document_pages.get(str(p["page_no"]), document_pages.get(p["page_no"]))
        size = entry.get("size", {})
        for dim in ("width", "height"):
            if not math.isclose(_number(size.get(dim), "document page size", positive=True),
                                p[dim], rel_tol=1e-5, abs_tol=0.01):
                raise WorkerError("document/source page geometry mismatch", "invalid_output")
    text_pages, located = _provenance(document, pages)
    if not text_pages or not located:
        raise WorkerError("entire conversion is empty or unlocated", "empty_conversion")
    notices = []
    confidence = _confidence(confidence if confidence else None, notices)
    payload = {
        "schema": "docling-conversion-v1", "format": format_name, "document": document,
        "pages": [dict(p, textless=p["page_no"] not in text_pages) for p in pages],
        "confidence": confidence, "warnings": sorted(notices, key=lambda w: w["field"]),
        "omissions": ["generated_page_images", "generated_picture_images",
                      "generated_table_images", "picture_descriptions"],
    }
    canonical_bytes(payload)
    return payload


def _run_conversion(source, name, format_name, root, pages):
    from concurrent.futures import ThreadPoolExecutor
    from docling.backend.docling_parse_backend import ThreadedDoclingParseDocumentBackend
    from docling.backend.image_backend import ImageDocumentBackend
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.backend_options import PdfBackendOptions, ThreadedDoclingParseBackendOptions
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.object_detection_engine_options import (
        TransformersObjectDetectionEngineOptions,
    )
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions, RapidOcrOptions, TableStructureOptions, TableFormerMode,
        LayoutObjectDetectionOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption, ImageFormatOption
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
    import torch
    import cv2

    torch.set_num_threads(1)
    if torch.get_num_interop_threads() != 1:
        torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    torch.use_deterministic_algorithms(True)
    cv2.setNumThreads(1)
    p = profile()
    ocr = RapidOcrOptions(
        backend="onnxruntime", lang=["en"], model_size="small", scale=3.0,
        force_full_page_ocr=False, text_score=0.5,
        use_det=True, use_cls=True, use_rec=True,
        det_model_path=str(root / REQUIRED_ASSETS[5]),
        cls_model_path=str(root / REQUIRED_ASSETS[6]),
        rec_model_path=str(root / REQUIRED_ASSETS[7]),
        rapidocr_params={"EngineConfig.onnxruntime.inter_op_num_threads": 1,
                         "EngineConfig.onnxruntime.intra_op_num_threads": 1},
    )
    options = PdfPipelineOptions(
        artifacts_path=root, document_timeout=LIMITS["wall_seconds"],
        accelerator_options=AcceleratorOptions(device="cpu", num_threads=1,
                                               cuda_use_flash_attention2=False),
        enable_remote_services=False, allow_external_plugins=False,
        do_ocr=True, ocr_options=ocr,
        do_table_structure=True, table_structure_options=TableStructureOptions(
            mode=TableFormerMode.ACCURATE, do_cell_matching=True),
        layout_options=LayoutObjectDetectionOptions(
            engine_options=TransformersObjectDetectionEngineOptions(
                compile_model=False, score_threshold=0.3),
            keep_empty_clusters=False, skip_cell_assignment=False, create_orphan_clusters=True),
        do_picture_classification=False, do_picture_description=False,
        do_chart_extraction=False, do_code_enrichment=False, do_formula_enrichment=False,
        generate_page_images=False, generate_picture_images=False,
        generate_table_images=False, generate_parsed_pages=False, images_scale=1.0,
        force_backend_text=False, use_reading_order_separators=True,
        ocr_batch_size=1, layout_batch_size=1, table_batch_size=1,
        queue_max_size=1, batch_polling_interval_seconds=0.5,
        stage_shutdown_timeout_seconds=15.0,
    )
    options.heading_hierarchy_options.enabled = False

    def guard_page(page):
        size = page.get_size()
        validate_pages([{"page_no": 1, "width": size.width, "height": size.height}])
        original = page.get_page_image

        def bounded_image(scale=1, cropbox=None):
            _number(scale, "render scale", positive=True)
            if scale > max(p["render_scales"].values()):
                raise WorkerError("unexpected backend render scale", "pixel_limit")
            validate_pages([{"page_no": 1, "width": size.width, "height": size.height}],
                           scale=scale)
            return original(scale=scale, cropbox=cropbox)
        page.get_page_image = bounded_image
        return page

    class BoundedPdfBackend(ThreadedDoclingParseDocumentBackend):
        def iter_pages(self):
            for page in super().iter_pages():
                yield guard_page(page)

    class BoundedImageBackend(ImageDocumentBackend):
        def load_page(self, page_no):
            return guard_page(super().load_page(page_no))

    class SingleThreadStandardPipeline(StandardPdfPipeline):
        def _borrow_model_threads(self):
            # The pinned standard pipeline otherwise runs five separate model
            # executors, even with accelerator_options.num_threads=1.
            with self._model_threads_lock:
                if self._idle_model_threads:
                    return self._idle_model_threads.pop()
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="DoclingCPU")
            return {name: executor for name in
                    ("ocr", "layout", "layout_postprocess", "table", "assemble")}

    backend_options = (
        ThreadedDoclingParseBackendOptions(
            enable_remote_fetch=False, enable_local_fetch=False, include_bitmap_images=False,
            enforce_same_font=True, parser_threads=1, max_concurrent_results=1,
            render_pages=True, render_scale=1.0, release_native_memory_every_n_pages=1)
        if format_name == "pdf" else
        PdfBackendOptions(enable_remote_fetch=False, enable_local_fetch=False,
                          include_bitmap_images=False))
    selected = InputFormat.PDF if format_name == "pdf" else InputFormat.IMAGE
    format_options = (
        PdfFormatOption(backend=BoundedPdfBackend, pipeline_cls=SingleThreadStandardPipeline,
                        pipeline_options=options, backend_options=backend_options)
        if format_name == "pdf" else
        ImageFormatOption(backend=BoundedImageBackend, pipeline_cls=SingleThreadStandardPipeline,
                          pipeline_options=options, backend_options=backend_options))
    converter = DocumentConverter(allowed_formats=[selected], format_options={selected: format_options})
    result = converter.convert(DocumentStream(name=name, stream=BytesIO(source)),
                               raises_on_error=False, max_num_pages=LIMITS["pdf_pages"],
                               max_file_size=LIMITS["source_bytes"])
    # Export the document, not ConversionResult (which carries timestamps/timings).
    document = result.document.export_to_dict()
    # Image generation is disabled. Refuse unexpected images rather than retaining
    # image/base64 data or recursively deleting meaningful source fields.
    def no_images(value):
        if isinstance(value, dict):
            if value.get("image") is not None or (
                    isinstance(value.get("uri"), str) and value["uri"].startswith("data:")):
                raise WorkerError("unexpected retained image payload", "invalid_output")
            for child in value.values():
                no_images(child)
        elif isinstance(value, list):
            for child in value:
                no_images(child)
    no_images(document)
    confidence = result.confidence.model_dump(mode="python")
    if "pages" in confidence:
        for page in pages:
            confidence["pages"].setdefault(page["page_no"], {
                metric: None for metric in ("parse_score", "layout_score", "table_score", "ocr_score")
            })
    return build_payload(
        document=document, format_name=format_name, pages=pages,
        processed_pages=[page.page_no for page in result.pages],
        status=result.status.value, confidence=confidence,
        errors=result.errors)


def convert(source: bytes, name: str, format_name: str, artifacts_path: Path) -> dict:
    """Convert one bounded snapshot. This function never downloads model assets."""
    _environment()
    pages = _inspect_source(source, name, format_name)
    runtime = _versions()
    root = _path_chain(artifacts_path)
    manifest, observation = _observe_assets(root)
    _validate_assets(root, manifest)
    if _observe_assets(root) != (manifest, observation):
        raise WorkerError("model assets changed before inference", "mutation")
    try:
        payload = _run_conversion(source, name, format_name, root, pages)
    except WorkerError:
        raise
    except ImportError as exc:
        raise WorkerError("optional conversion dependency unavailable", "missing_dependency") from exc
    except Exception as exc:
        raise WorkerError("local Docling inference failed", "conversion_failed") from exc
    finally:
        if _observe_assets(root) != (manifest, observation):
            raise WorkerError("model assets changed during inference", "mutation")
    report = {"conversion": payload, "runtime": runtime,
              "profile": profile(), "assets": manifest}
    canonical_bytes(report)
    return report


def _parse_json(raw):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise WorkerError("duplicate JSON field", "invalid_job")
            result[k] = v
        return result

    def invalid_constant(_):
        raise WorkerError("non-finite JSON number", "invalid_job")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, WorkerError):
            raise
        raise WorkerError("malformed JSON", "invalid_job") from exc


def execute_job(job):
    if not isinstance(job, dict) or job.get("schema") != "docling-worker-job-v1":
        raise WorkerError("unsupported worker job schema", "invalid_job")
    common = {"schema", "operation", "artifacts_path"}
    operation = job.get("operation")
    expected = common if operation == "inspect" else common | {"source_path", "name", "format"}
    if operation not in ("inspect", "convert") or set(job) != expected:
        raise WorkerError("invalid operation or job fields", "invalid_job")
    if not all(isinstance(v, str) and v for v in job.values()):
        raise WorkerError("job fields must be nonempty strings", "invalid_job")
    if operation == "inspect":
        return inspect_runtime(Path(job["artifacts_path"]))
    source, _ = _read_regular(Path(job["source_path"]), LIMITS["source_bytes"], private=True)
    return convert(source, job["name"], job["format"], Path(job["artifacts_path"]))


def _timeout(_signal, _frame):
    raise WorkerError("worker wall timeout exceeded", "timeout")


def _result_directory(path):
    """Retain a nonsymlinked parent, even if its pathname is later replaced."""
    path = _path_chain(path)
    if path.name in ("", ".", ".."):
        raise WorkerError("result must have a basename")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parent.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        parent = os.fstat(descriptor)
        if parent.st_mode & 0o077 or parent.st_uid != os.getuid():
            raise WorkerError("result directory must be private and user-owned")
        return descriptor, path.name
    except BaseException:
        os.close(descriptor)
        raise


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: docling_worker.py JOB RESULT", file=sys.stderr)
        return 2
    try:
        parent_fd, result_name = _result_directory(Path(args[1]))
    except (OSError, WorkerError):
        print("result path must be private, user-owned and nonsymlinked", file=sys.stderr)
        return 2
    try:
        for kind, ceiling in ((resource.RLIMIT_AS, LIMITS["address_space_bytes"]),
                              (resource.RLIMIT_FSIZE, LIMITS["result_bytes"])):
            hard = resource.getrlimit(kind)[1]
            if hard != resource.RLIM_INFINITY:
                ceiling = min(hard, ceiling)
            resource.setrlimit(kind, (ceiling, ceiling))
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(LIMITS["wall_seconds"])
        job = _parse_json(_read_regular(Path(args[0]), 64 * 1024, private=True)[0])
        result = {"schema": "docling-worker-result-v1", "ok": True, "payload": execute_job(job)}
        raw = canonical_bytes(result)
        status_code = 0
    except Exception as exc:
        error = exc if isinstance(exc, WorkerError) else WorkerError(
            "worker runtime failed", "runtime_error")
        result = {"schema": "docling-worker-result-v1", "ok": False,
                  "error": {"code": error.code, "message": str(error)[:4096]}}
        raw = canonical_bytes(result)
        status_code = 1
    # Conversion is bounded by SIGALRM; publication must finish a complete
    # envelope without a late/pending alarm interrupting its write or fsync.
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    signal.alarm(0)
    try:
        fd = os.open(result_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        print("cannot create private result file without replacement", file=sys.stderr)
        return 2
    finally:
        signal.alarm(0)
        os.close(parent_fd)
    return status_code


if __name__ == "__main__":
    raise SystemExit(main())
