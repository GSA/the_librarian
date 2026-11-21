"""PDF and image text renditions: the embedded PDF text layer first, Docling OCR/layout otherwise.

One file is converted in one limited child process (``child`` below), never in the toolkit process:
the child caps its own address space, CPU time and file size, holds a wall-clock alarm, and launches
the pinned-environment readers inside ``unshare -Urn``. A PDF's text layer is read by
``pdf_text_worker.py`` (pypdfium2 from the Docling lock); when every page has enough real text it is
the rendition (``payload.format == "pdf-text"``). Otherwise, and for every image, the pinned Docling
worker converts the file (``payload.format == "ocr"``). A crash, hang or limit fails that file alone
with an explicit code; a transient failure is retried once.

Renditions use the ordinary extraction envelope and lifecycle (``prepare`` publishes, ``register``
commits the catalog records) so cohort intake drives them through ``extract-prepare`` and
``extract-register`` stages. Machine conversion is not review, qualification or indexing.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import zlib

from tools import policy
from tools.catalog import catalog_store as catalog
from tools.context_index import renditions
from tools.extraction import docling_bridge as bridge
from tools.extraction import docling_worker
from tools.extraction import extract


VERSION = 1
HOME = extract.HOME
FORMATS = ("pdf", "image")
HERE = Path(__file__).resolve().parent
TOOLKIT = HERE.parents[1]
TEXT_WORKER = HERE / "pdf_text_worker.py"
LOCK = HERE / "requirements-docling.txt"
DOCLING_FILES = (HERE / "docling_adapter.py", HERE / "docling_bridge.py", HERE / "docling_worker.py")
MAX_SOURCE = bridge.MAX_SOURCE
MAX_RESULT = 64 * 1024 * 1024
# A conversion child copies each pinned model asset into its private directory before Docling
# runs, so its per-file size ceiling must admit the largest pin as well as its own output.
LARGEST_ASSET = max(size for size, _ in docling_worker.ASSET_PINS)
MIB = 1024**2
# A page's text layer is real text when it has at least ``min_page_chars`` letters or digits and they
# are at least ``min_alnum_ratio`` of its non-space characters (garbled or missing-font layers fail the
# ratio). A page below that is still fine when it holds no image object: OCR has nothing more to read
# on a blank or vector page. The whole document needs ``min_document_chars`` letters or digits.
# Every page must pass, or the whole file goes to Docling OCR (which handles mixed pages).
TEXT_LAYER = {"min_page_chars": 200, "min_alnum_ratio": 0.5, "min_document_chars": 200}
LIMITS = {
    "source_bytes": MAX_SOURCE, "pages": renditions.DOCLING_MAX_PAGES,
    "lines": renditions.DOCLING_MAX_RECORDS, "image_pixels": renditions.DOCLING_MAX_PAGE_PIXELS,
    "result_bytes": MAX_RESULT, "wall_seconds": 900, "cpu_seconds": 1800,
    "address_space_bytes": docling_worker.LIMITS["address_space_bytes"], "attempts": 2,
}
TEXT_WALL_SECONDS = 150
GRACE_SECONDS = 2
# Failures a second attempt may not repeat: a hang, a killed worker, or a lost result.
TRANSIENT = frozenset({"timeout", "worker_crashed", "worker_failed", "no_result"})
DEFAULT_CONCURRENCY = 2
NOT_PROVISIONED = "docling-not-provisioned"
PYTHON_ENV, ARTIFACTS_ENV = "LIBRARIAN_DOCLING_PYTHON", "LIBRARIAN_DOCLING_ARTIFACTS"
RUNTIME = Path("tools/extraction/.local")
TEXT_SCOPE = ("Embedded PDF text layer of every page (pypdfium2), one record per non-blank line in "
              "PDFium text order; layout, tables and images are flattened or not read.")
OCR_SCOPE = ("Machine OCR/layout text of every page (pinned Docling), one record per non-blank line in "
             "reading order; table rows are ' | '-joined cells; geometry and images are not retained.")
TEXT_WARNING = ("Embedded PDF text layer only: reading order follows the PDF and tables, layout and "
                "images are not reproduced. Machine extraction is not review.")
OCR_WARNING = ("Machine OCR/layout text: characters, reading order and table cells may be misread. "
               "Machine extraction is not review.")


class ConversionFailed(extract.ExtractionError):
    """One file's conversion failed with ``code`` (a ledger-safe identifier) after its attempts."""

    def __init__(self, code, message=None):
        self.raw_code = code
        self.code = "extract-" + code.replace("_", "-")
        self.transient = code in TRANSIENT
        super().__init__(message or code)


def require(condition, message):
    if not condition:
        raise extract.ExtractionError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


# Routing and limits: pure functions of bytes and numbers, testable without the runtime.

def runtime(root):
    """The Docling interpreter and model directory this instance uses (environment overrides)."""
    local = Path(root) / RUNTIME
    # Absolute (the child runs in the toolkit directory) but not resolved: a venv interpreter is a symlink.
    return {"python": Path(os.path.abspath(os.environ.get(PYTHON_ENV) or local / "venv-docling/bin/python")),
            "artifacts": Path(os.path.abspath(os.environ.get(ARTIFACTS_ENV) or local / "models"))}


def status(root):
    """Whether the pinned runtime is provisioned: interpreter present and every pinned asset at its size.

    Cheap (no launch, no hashing); conversion still verifies every asset hash and dependency pin.
    """
    paths = runtime(root)
    python, artifacts = paths["python"], paths["artifacts"]
    missing = []
    if not (python.is_file() and os.access(python, os.X_OK)):
        missing.append("python")
    for relative, (size, _) in zip(docling_worker.REQUIRED_ASSETS, docling_worker.ASSET_PINS):
        path = artifacts / relative
        if artifacts.is_symlink() or not path.is_file() or path.stat().st_size != size:
            missing.append("models/" + relative)
    provisioned = not missing
    return {"provisioned": provisioned, "code": None if provisioned else NOT_PROVISIONED,
            "python": str(python), "artifacts": str(artifacts), "missing": missing,
            "action": None if provisioned else (
                "Provision the pinned Docling environment and models (tools/extraction/README.md) at "
                f"{RUNTIME}/ or set {PYTHON_ENV} and {ARTIFACTS_ENV}; PDF and image files stay "
                "needs-runtime until then.")}


def provisioned(root):
    return status(root)["provisioned"]


def image_size(raw):
    """(width, height) from a PNG IHDR or JPEG SOF header, or None when the header cannot be read."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and raw[12:16] == b"IHDR" and len(raw) >= 24:
        return struct.unpack(">II", raw[16:24])
    if not raw.startswith(b"\xff\xd8"):
        return None
    index, length = 2, len(raw)
    for _ in range(10000):
        while index < length and raw[index] == 0xFF:
            index += 1
        if index >= length:
            return None
        marker = raw[index]
        index += 1
        if marker in (0x01, *range(0xD0, 0xD8)):
            continue
        if index + 2 > length or marker == 0xD9:
            return None
        size = int.from_bytes(raw[index:index + 2], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if index + 7 > length:
                return None
            height, width = struct.unpack(">HH", raw[index + 3:index + 7])
            return width, height
        index += size
    return None


def pixel_refusal(raw, format_name):
    """``pixel_limit`` for an image whose header declares more pixels than the cap (a decompression
    bomb is refused before any decoder runs); None otherwise."""
    if format_name != "image":
        return None
    size = image_size(raw)
    if size is not None and size[0] * size[1] > LIMITS["image_pixels"]:
        return "pixel_limit"
    return None


OCR_SCALE = 3.0  # the pinned Docling profile's OCR render scale


def _fits(width, height, dpi):
    """The pinned Docling worker's geometry check: source pixels at ``dpi`` rendered at the OCR scale."""
    w, h = width * 72.0 / dpi, height * 72.0 / dpi
    return math.ceil(w * OCR_SCALE) * math.ceil(h * OCR_SCALE) <= LIMITS["image_pixels"]


def _png_ppm(dpi):
    return int(dpi / 0.0254 + 0.5)


def ocr_dpi(width, height, format_name):
    """The smallest whole DPI (at least 72) at which the 3x OCR raster fits the page pixel cap.

    Docling sizes an image page from its DPI metadata (72 when absent), so without this a 2560x1440
    screenshot would be refused although its pixels are well within the cap.
    """
    for dpi in range(72, 72 * 64):
        effective = _png_ppm(dpi) * 0.0254 if format_name == "png" else dpi
        if _fits(width, height, effective):
            return dpi
    raise ConversionFailed("pixel_limit", "image cannot be fitted to the OCR raster limit")


def _png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _with_png_dpi(raw, dpi):
    """``raw`` with every pHYs chunk replaced by one declaring ``dpi`` right after IHDR."""
    ppm = _png_ppm(dpi)
    out, index = [raw[:33]], 33
    out.append(_png_chunk(b"pHYs", struct.pack(">IIB", ppm, ppm, 1)))
    while index + 8 <= len(raw):
        size = int.from_bytes(raw[index:index + 4], "big")
        end = index + 12 + size
        if end > len(raw):
            raise ConversionFailed("malformed_source", "truncated PNG chunk")
        if raw[index + 4:index + 8] != b"pHYs":
            out.append(raw[index:end])
        index = end
    return b"".join(out) + raw[index:]


def _with_jpeg_dpi(raw, dpi):
    """``raw`` with every JFIF APP0 density set to ``dpi`` (one inserted after SOI if there is none)."""
    data, index, found = bytearray(raw), 2, False
    while index + 4 <= len(data) and data[index] == 0xFF:
        marker = data[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in (0xD9, 0xDA):
            break
        size = int.from_bytes(data[index + 2:index + 4], "big")
        if marker == 0xE0 and size >= 16 and data[index + 4:index + 9] == b"JFIF\x00":
            data[index + 11:index + 16] = struct.pack(">BHH", 1, dpi, dpi)
            found = True
        index += 2 + size
    if not found:
        app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01" + struct.pack(">BHH", 1, dpi, dpi) + b"\x00\x00"
        data[2:2] = app0
    return bytes(data)


def ocr_source(raw, format_name):
    """``(bytes for Docling, dpi or None)``: images get DPI metadata so the OCR raster fits the cap.

    Only the declared DPI changes; pixel data is untouched and the rendition still names the
    original bytes. PDFs pass through unchanged.
    """
    if format_name != "image":
        return raw, None
    size = image_size(raw)
    if size is None:
        return raw, None
    kind = "png" if raw.startswith(b"\x89PNG") else "jpeg"
    dpi = ocr_dpi(size[0], size[1], kind)
    return (_with_png_dpi(raw, dpi) if kind == "png" else _with_jpeg_dpi(raw, dpi)), dpi


def page_chars(text):
    """(letters or digits, non-space characters) of one page's text."""
    alnum = sum(c.isalnum() for c in text)
    return alnum, sum(not c.isspace() for c in text)


def text_layer_decision(pages):
    """(use the text layer, reason) for the worker's per-page ``text``/``image_objects`` report."""
    weak, total = [], 0
    for page in pages:
        alnum, visible = page_chars(page["text"])
        total += alnum
        real = alnum >= TEXT_LAYER["min_page_chars"] and alnum >= TEXT_LAYER["min_alnum_ratio"] * visible
        if not real and page["image_objects"]:
            weak.append(page["page"])
    if total < TEXT_LAYER["min_document_chars"]:
        return False, "PDF text layer has fewer than %d letters or digits" % TEXT_LAYER["min_document_chars"]
    if weak:
        shown = ", ".join(str(p) for p in weak[:20]) + (", ..." if len(weak) > 20 else "")
        return False, "PDF text layer is insufficient on image-bearing page(s) " + shown
    return True, None


def _lines(items):
    """Numbered non-blank line records from ``(page, text)`` pairs; refuses past the record cap."""
    lines = []
    for page, text in items:
        for line in text.split("\n"):
            line = line.rstrip()
            if line.strip():
                if len(lines) >= LIMITS["lines"]:
                    raise ConversionFailed("record_limit", "rendition exceeds the line record limit")
                lines.append({"line": len(lines) + 1, "page": page, "text": line})
    return lines


def text_payload(report, format_name="pdf"):
    pages = report["pages"]
    return {
        "format": "pdf-text", "source_format": format_name, "scope": TEXT_SCOPE,
        "warnings": [TEXT_WARNING],
        "lines": _lines((page["page"], page["text"]) for page in pages),
        "pages": [{"page": page["page"], "text_chars": page_chars(page["text"])[0],
                   "image_objects": page["image_objects"]} for page in pages],
        "method": {"kind": "pdf-text-layer", "reader": report["reader"], "version": report["version"],
                   "threshold": dict(TEXT_LAYER)},
    }


def _page_spans(text, prov):
    """``[(page, text)]`` of a text item whose provenance spans pages, split at each page's charspan
    start; None when the spans are not ordered, in-range character offsets."""
    starts = []
    for item in prov:
        span = item.get("charspan") if isinstance(item, dict) else None
        if (type(item.get("page_no") if isinstance(item, dict) else None) is not int
                or not isinstance(span, list) or len(span) != 2
                or not all(type(n) is int for n in span) or not 0 <= span[0] <= span[1] <= len(text)
                or (starts and span[0] < starts[-1][1])):
            return None
        starts.append((item["page_no"], span[0]))
    bounds = [0] + [start for _, start in starts[1:]] + [len(text)]
    return [(page, text[bounds[i]:bounds[i + 1]]) for i, (page, _) in enumerate(starts)]


def _flatten(document, spanning=None):
    """``(page, text)`` of a Docling document in body then furniture order, then any item not reached.

    Text items give their text; one whose provenance spans pages is split at its charspans, or, when
    they cannot place it, cited at its first page and counted in ``spanning``. Tables give one
    ' | '-joined line per row (a spanning cell in its first row).
    """
    seen, items, page = set(), [], 1

    def node(ref):
        parts = ref.split("/") if isinstance(ref, str) else []
        if len(parts) != 3 or parts[0] != "#" or not parts[2].isdigit():
            return None
        values = document.get(parts[1])
        index = int(parts[2])
        return values[index] if isinstance(values, list) and index < len(values) else None

    roots = [child.get("$ref") for name in ("body", "furniture")
             for child in (document.get(name) or {}).get("children") or [] if isinstance(child, dict)]
    leftovers = [f"#/{kind}/{i}" for kind in ("texts", "tables") for i in range(len(document.get(kind) or []))]
    work = list(reversed(roots + leftovers))
    while work:
        ref = work.pop()
        if not isinstance(ref, str) or ref in seen:
            continue
        value = node(ref)
        if not isinstance(value, dict):
            continue
        seen.add(ref)
        prov = value.get("prov") or []
        if prov and isinstance(prov[0], dict) and type(prov[0].get("page_no")) is int:
            page = prov[0]["page_no"]
        if ref.startswith("#/texts/") and isinstance(value.get("text"), str):
            pages = {p.get("page_no") for p in prov if isinstance(p, dict)}
            split = _page_spans(value["text"], prov) if len(pages) > 1 else None
            if split:
                items.extend(split)
                page = split[-1][0]
            else:
                items.append((page, value["text"]))
                if len(pages) > 1 and spanning is not None:
                    spanning.append(ref)
        elif ref.startswith("#/tables/"):
            rows = {}
            for cell in (value.get("data") or {}).get("table_cells") or []:
                if not isinstance(cell, dict):
                    continue
                row, column = cell.get("start_row_offset_idx"), cell.get("start_col_offset_idx")
                text = " ".join(str(cell.get("text", "")).split())
                if type(row) is int and type(column) is int and text:
                    rows.setdefault(row, []).append((column, text))
            for row in sorted(rows):
                items.append((page, " | ".join(text for _, text in sorted(rows[row]))))
        for child in reversed(value.get("children") or []):
            if isinstance(child, dict):
                work.append(child.get("$ref"))
    return items


def _confidence_warnings(warnings):
    grouped = {}
    for warning in warnings:
        if isinstance(warning, dict):
            label = warning.get("field", "?") + ("=" + str(warning["value"]) if "value" in warning else "")
            grouped.setdefault(warning.get("code", "warning"), []).append(label)
    notes = []
    for code, fields in sorted(grouped.items()):
        note = "%s (%d): %s" % (code, len(fields), ", ".join(fields))
        notes.append(note if len(note) <= 2000 else note[:1997] + "...")
    return notes


def ocr_payload(report, format_name, fallback=None):
    conversion = report["conversion"]
    notes = _confidence_warnings(conversion.get("warnings", []))
    spanning = []
    lines = _lines(_flatten(conversion["document"], spanning))
    if spanning:
        notes.append("%d text item(s) span pages without usable character spans; each is cited at "
                     "its first page" % len(spanning))
    return {
        "format": "ocr", "source_format": format_name, "scope": OCR_SCOPE,
        "warnings": [OCR_WARNING] + ([fallback] if fallback else []) + notes,
        "lines": lines,
        "pages": [{"page": page["page_no"], "textless": page["textless"]} for page in conversion["pages"]],
        "method": {"kind": "docling-ocr", "fallback": fallback, "runtime": report["runtime"],
                   "profile_version": report["profile"]["version"],
                   "assets_sha256": digest(docling_worker.canonical_bytes(report["assets"])),
                   "confidence": conversion["confidence"]},
    }


# The limited child process: one file, then exit.

def _limit(kind, soft, hard=None):
    hard = soft if hard is None else hard
    for inherited in resource.getrlimit(kind):
        if inherited != resource.RLIM_INFINITY:
            soft, hard = min(inherited, soft), min(inherited, hard)
    resource.setrlimit(kind, (min(soft, hard), hard))


def file_ceiling(output_bytes):
    """The ``RLIMIT_FSIZE`` of a child that writes ``output_bytes`` and copies the model assets."""
    return max(output_bytes, LARGEST_ASSET)


def _alarm(_signal, _frame):
    raise ConversionFailed("timeout", "conversion wall timeout exceeded")


def _kill(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _read_result(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(MAX_RESULT + 1)
    if len(raw) > MAX_RESULT:
        raise ConversionFailed("output_limit", "conversion result exceeds 64 MiB")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise ConversionFailed("invalid_result", "conversion result is not JSON") from error


def _text_layer(home, raw, python):
    """The text worker's report for ``raw``, run in the pinned interpreter inside a network namespace."""
    work = home / "text"
    work.mkdir(mode=0o700)
    worker = work / TEXT_WORKER.name
    worker.write_bytes(TEXT_WORKER.read_bytes())
    (work / LOCK.name).write_bytes(LOCK.read_bytes())
    source = work / "source.pdf"
    source.write_bytes(raw)
    job = work / "job.json"
    job.write_text(json.dumps({"schema": "pdf-text-job-v1", "source_path": str(source)}))
    for path in (worker, work / LOCK.name, source, job):
        path.chmod(0o600)
    result = work / "result.json"
    env = {"PATH": os.defpath, "LANG": "C.UTF-8"}
    try:
        process = subprocess.Popen(["unshare", "-Urn", str(python), "-I", str(worker), str(job), str(result)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    except OSError as error:
        raise ConversionFailed("isolation_unavailable", "text-layer worker launch unavailable") from error
    try:
        process.wait(timeout=TEXT_WALL_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise ConversionFailed("timeout", "text-layer worker exceeded wall timeout") from error
    finally:
        _kill(process)
    envelope = _read_result(result)
    if envelope is None:
        if process.returncode == -signal.SIGXCPU:
            raise ConversionFailed("cpu_limit", "text-layer worker exceeded CPU time")
        raise ConversionFailed("worker_crashed", "text-layer worker produced no result")
    if not envelope.get("ok"):
        error = envelope.get("error") or {}
        raise ConversionFailed(str(error.get("code") or "worker_failed"), error.get("message"))
    return envelope["payload"]


def extract_file(home, raw, name, format_name, python, artifacts):
    """The rendition payload of one file: the PDF text layer when it suffices, else Docling OCR."""
    from tools.extraction import docling_adapter
    refusal = pixel_refusal(raw, format_name)
    if refusal:
        raise ConversionFailed(refusal, "image header declares more pixels than the limit")
    fallback = None
    if format_name == "pdf":
        report = _text_layer(home, raw, python)
        use, fallback = text_layer_decision(report["pages"])
        if use:
            return text_payload(report)
    source, dpi = ocr_source(raw, format_name)
    try:
        report = docling_adapter.convert(source, name, format_name, Path(artifacts), Path(python))
    except docling_adapter.AdapterError as error:
        raise ConversionFailed(error.code, str(error)) from error
    payload = ocr_payload(report, format_name, fallback)
    payload["method"]["ocr_dpi"] = dpi
    return payload


def child(home):
    """Entry of the limited child: convert ``home/source`` per ``home/job.json``, write ``home/result.json``."""
    home = Path(home)
    started = time.monotonic()
    try:
        job = json.loads((home / "job.json").read_text())
        _limit(resource.RLIMIT_AS, job["address_space_bytes"])
        # The soft CPU limit sends SIGXCPU; the hard limit kills a process that ignores it.
        _limit(resource.RLIMIT_CPU, job["cpu_seconds"], job["cpu_seconds"] + 10)
        _limit(resource.RLIMIT_FSIZE, file_ceiling(MAX_RESULT))
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(job["wall_seconds"])
        raw = (home / "source").read_bytes()
        payload = extract_file(home, raw, job["name"], job["format"], job["python"], job["artifacts"])
        signal.alarm(0)
        result = {"ok": True, "payload": payload}
    except ConversionFailed as error:
        result = {"ok": False, "code": error.raw_code, "message": str(error)[:4096]}
    except MemoryError:
        result = {"ok": False, "code": "memory_limit", "message": "conversion exceeded its address space"}
    except Exception as error:  # noqa: BLE001 - every other failure is this file's runtime error
        result = {"ok": False, "code": "runtime_error", "message": type(error).__name__}
    signal.alarm(0)
    usage = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    result["stats"] = {"seconds": round(time.monotonic() - started, 3), "peak_rss_kib": usage,
                       "kind": result.get("payload", {}).get("method", {}).get("kind")}
    raw = (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(raw) > MAX_RESULT:
        raw = json.dumps({"ok": False, "code": "output_limit", "message": "result exceeds 64 MiB",
                          "stats": result["stats"]}).encode()
    descriptor = os.open(home / "result.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
    return 0 if result["ok"] else 1


def _child_argv(home):
    return [sys.executable, "-E", "-s", "-m", "tools.extraction.pdf_image", "child", str(home)]


def _attempt(raw, name, format_name, paths, work, limits):
    with tempfile.TemporaryDirectory(prefix="pdf-image-", dir=work) as temporary:
        home = Path(temporary)
        home.chmod(0o700)
        (home / "source").write_bytes(raw)
        (home / "job.json").write_text(json.dumps({
            "name": name, "format": format_name, "python": str(paths["python"]),
            "artifacts": str(paths["artifacts"]), "address_space_bytes": limits["address_space_bytes"],
            "cpu_seconds": limits["cpu_seconds"], "wall_seconds": max(1, limits["wall_seconds"] - 30)}))
        # Docling's adapter puts its own scratch files below TMPDIR: keep them inside this directory.
        env = {"PATH": os.defpath, "LANG": "C.UTF-8", "TMPDIR": str(home), "PYTHONHASHSEED": "0"}
        try:
            process = subprocess.Popen(_child_argv(home), cwd=TOOLKIT, env=env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            raise ConversionFailed("worker_failed", "conversion child launch failed") from error
        try:
            process.wait(timeout=limits["wall_seconds"])
        except subprocess.TimeoutExpired as error:
            raise ConversionFailed("timeout", "conversion child exceeded wall timeout") from error
        finally:
            _kill(process)
        result = _read_result(home / "result.json")
    if result is None:
        if process.returncode in (-signal.SIGXCPU, -signal.SIGKILL):
            raise ConversionFailed("cpu_limit" if process.returncode == -signal.SIGXCPU else "worker_crashed",
                                   "conversion child was killed")
        raise ConversionFailed("worker_crashed", "conversion child produced no result")
    if not result.get("ok"):
        failure = ConversionFailed(str(result.get("code") or "worker_failed"), result.get("message"))
        failure.stats = result.get("stats")
        raise failure
    return result["payload"], result.get("stats", {})


def convert(raw, name, format_name, paths, work, limits=None):
    """``(payload, stats)`` of one file, retrying a transient failure once; raises ``ConversionFailed``."""
    limits = dict(LIMITS, **(limits or {}))
    require(format_name in FORMATS, "Unsupported PDF/image format")
    if len(raw) > limits["source_bytes"]:
        raise ConversionFailed("source_limit", "source exceeds byte limit")
    refusal = pixel_refusal(raw, format_name)
    if refusal:
        raise ConversionFailed(refusal, "image header declares more pixels than the limit")
    for attempt in range(limits["attempts"]):
        try:
            return _attempt(raw, name, format_name, paths, work, limits)
        except ConversionFailed as error:
            if not error.transient or attempt + 1 >= limits["attempts"]:
                raise


# Identity and lifecycle: the ordinary extraction envelope, registered as ``A-extract-<run>``.

def extractor(root):
    """The extraction identity: this code, the text worker, the lock, the Docling code and profile,
    the routing thresholds and limits, and the instance policy. Computed without converting."""
    parts = {
        "code": digest(Path(__file__).read_bytes() + b"\0" + TEXT_WORKER.read_bytes()),
        "lock": digest(LOCK.read_bytes()),
        "docling": {"code": digest(b"\0".join(path.read_bytes() for path in DOCLING_FILES)),
                    "profile": docling_worker.profile()},
        "profile": {"text_layer": TEXT_LAYER, "limits": LIMITS},
        "policy": policy.fingerprint(root),
    }
    return {"version": VERSION, "sha256": digest(extract.encoded(parts))}


def bundle(root, receipt, format_name):
    """``(state, original, path, value)``; ``value["payload"]`` is None until the rendition is published.

    The run ID depends only on the source and the extractor identity, so it is known before converting.
    """
    root = catalog.safe_path(root)
    require(format_name in FORMATS, "Unsupported PDF/image format")
    state, original, identity, _ = bridge.source(root, receipt, format_name)
    settings = policy.for_root(root)[0]
    home = policy.validate_instance_roots(root)["extractions_home"]
    engine = extractor(root)
    run = digest(extract.encoded({"source": identity, "extractor": engine, "format": format_name}))
    value = {"schema_version": VERSION, "run_id": run, "extractor": engine, "source": identity,
             "boundary": extract.BOUNDARY + " " + settings["handling"], "payload": None}
    path = root / home / receipt / run / "extraction.json"
    if path.exists():
        value = _published(path, value)
    return state, original, path, value


def _published(path, expected):
    raw = catalog.private_file(path).read_bytes()
    value = json.loads(raw.decode("utf-8"))
    require(isinstance(value, dict) and {k: v for k, v in value.items() if k != "payload"}
            == {k: v for k, v in expected.items() if k != "payload"}
            and isinstance(value.get("payload"), dict)
            and value["payload"].get("format") in ("pdf-text", "ocr")
            and extract.encoded(value) == raw,
            "Existing extraction differs from this source and extractor; preserve it and investigate")
    return value


def unchanged(root, value):
    raw = bridge._read_source(catalog.safe_path(root / value["source"]["path"]))
    require(digest(raw) == value["source"]["sha256"], "Source changed during extraction/registration")
    require(extractor(root) == value["extractor"], "Extractor changed during operation")


def work_directory(root):
    path = catalog.safe_path(root / HOME / "work")
    catalog.private_directory(root / HOME)
    catalog.private_directory(path)
    return path


# Conversion failures of this process, by (receipt, run) -> (code, message): the ledger stage that
# follows a failed ahead-of-time conversion consumes the entry and records that failure instead of
# converting again under the ledger lock. Only the code and message are kept, never the exception.
_FAILED = {}


def _payload(root, value, format_name, paths=None, limits=None):
    """Convert the pinned source of ``value`` (re-read and re-hashed) in a limited child process.

    A failure is remembered for this process (``_FAILED``) until the next call consumes it, and raised.
    """
    key = (value["source"]["receipt_id"], value["run_id"])
    if key in _FAILED:
        raise ConversionFailed(*_FAILED.pop(key))
    raw = bridge._read_source(catalog.safe_path(root / value["source"]["path"]))
    require(digest(raw) == value["source"]["sha256"], "Source changed during extraction")
    try:
        payload, _ = convert(raw, Path(value["source"]["path"]).name, format_name,
                             paths or runtime(root), work_directory(root), limits)
    except ConversionFailed as error:
        _FAILED[key] = (error.raw_code, str(error))
        raise
    return {**value, "payload": payload}


def prepare(root, receipt, format_name, prepared=None, paths=None, limits=None):
    """Convert (unless published) and publish one rendition; returns the extract-style result."""
    root = catalog.safe_path(root)
    state, original, path, value = prepared or bundle(root, receipt, format_name)
    require(value["source"]["receipt_id"] == receipt and path.is_relative_to(root),
            "Prepared bundle is for a different receipt or root")
    if value["payload"] is None:
        value = _payload(root, value, format_name, paths, limits)
    data = extract.encoded(value)
    require(len(data) <= MAX_RESULT, "Complete rendition exceeds output limit")
    with catalog.writer_lock(root / HOME):
        catalog.private_directory(path.parent.parent)
        catalog.private_directory(path.parent)
        unchanged(root, value)
        created = extract.publish(path, data)
        unchanged(root, value)
    return {"command": "prepare", "receipt_id": receipt, "run_id": value["run_id"],
            "rendition": path.relative_to(root).as_posix(), "created": created,
            "source_sha256": value["source"]["sha256"], "rendition_sha256": digest(data),
            "catalog_state": state, "boundary": extract.BOUNDARY, "warnings": value["payload"]["warnings"],
            "kind": value["payload"]["method"]["kind"]}


PREPARE_ERRORS = (extract.ExtractionError, bridge.BridgeError, catalog.CatalogError, OSError,
                  sqlite3.Error, ValueError, KeyError)


def prepare_many(root, requests, concurrency=DEFAULT_CONCURRENCY, paths=None, limits=None):
    """``prepare`` for many ``(receipt, format)`` pairs, converting up to ``concurrency`` files at once.

    Catalog reads and publication run in this thread; only the conversions (each its own limited
    child process) overlap. Returns ``{receipt: prepare result or the exception that refused it}``;
    one file's failure never stops another.
    """
    require(type(concurrency) is int and concurrency >= 1, "Extraction concurrency must be a positive integer")
    from concurrent.futures import ThreadPoolExecutor
    root = catalog.safe_path(root)
    outcomes, bundles = {}, {}
    for receipt, format_name in requests:
        try:
            bundles[receipt] = (format_name, bundle(root, receipt, format_name))
        except PREPARE_ERRORS as error:
            outcomes[receipt] = error
    todo = [receipt for receipt, (_, prepared) in bundles.items() if prepared[3]["payload"] is None]

    def one(receipt):
        format_name, prepared = bundles[receipt]
        try:
            return _payload(root, prepared[3], format_name, paths, limits)
        except PREPARE_ERRORS as error:
            return error
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        converted = dict(zip(todo, pool.map(one, todo)))
    for receipt, (format_name, prepared) in bundles.items():
        value = converted.get(receipt, prepared[3])
        if isinstance(value, Exception):
            outcomes[receipt] = value
            continue
        try:
            outcomes[receipt] = prepare(root, receipt, format_name, (*prepared[:3], value), paths, limits)
        except PREPARE_ERRORS as error:
            outcomes[receipt] = error
    return outcomes


def forget_failures():
    _FAILED.clear()


def registration_records(root, original, path, value):
    relative = path.relative_to(root).as_posix()
    ref = extract.reference(relative, digest(extract.encoded(value)))
    what = ("embedded PDF text layer" if value["payload"]["format"] == "pdf-text" else "machine OCR/layout text")
    parent = deepcopy(original)
    data = parent["data"]
    data.update(sha256=value["source"]["sha256"], hash_basis="observed", bytes=value["source"]["bytes"],
                extraction="existing-rendition", coverage="deferred",
                reason="Text rendition (" + what + ") exists; visual content is not reproduced. "
                       "Review/indexing remain separate.")
    if ref not in data["provenance"]:
        data["provenance"].append(ref)
    identifier = "A-extract-" + value["run_id"]
    child = {"id": identifier, "kind": "artifact", "data": {
        "path": relative, "parent_id": original["id"], "receipt_id": value["source"]["receipt_id"],
        "role": "generated-rendition", "audience": data["audience"], "coverage": "deferred",
        "reason": "Machine text rendition (" + what + "); inherits source restrictions; not reviewed, "
                  "indexed or independent evidence.",
        "presence": "present", "sha256": ref["sha256"], "hash_basis": "observed",
        "bytes": len(extract.encoded(value)), "extraction": "not-required",
        "handling_refs": deepcopy(data["handling_refs"]),
        "provenance": [extract.reference(value["source"]["path"], value["source"]["sha256"]), ref],
    }}
    relation = {"id": "R-extract-" + value["run_id"], "kind": "relationship", "data": {
        "from_id": identifier, "to_id": original["id"], "relation": "rendition-of",
        "scope": "Machine text-only rendition (" + what + "); not visual equivalence, independent "
                 "evidence, review or acceptance.",
        "basis": "reported", "provenance": [ref],
    }}
    return [parent, child, relation]


def _recipe(root, original, path, value, state, actor, marker):
    """The registration to submit, or the one an earlier attempt saved beside the rendition."""
    recipe = {"actor": actor, "request_id": "extract-" + value["source"]["receipt_id"] + "-" + value["run_id"],
              "reason": "Explicit machine-extraction registration; no review, index or intake-stage change.",
              "records": registration_records(root, original, path, value)}
    catalog.validate_records(recipe["records"])
    saved_path = path.parent / "registration.json"
    if not saved_path.exists():
        extract.publish(saved_path, extract.encoded(recipe))
        return recipe
    saved = catalog.decode(catalog.private_file(saved_path).read_text())
    require(set(saved) == set(recipe) and all(saved[k] == recipe[k] for k in ("actor", "request_id", "reason")),
            "Saved registration differs in actor, request or reason")
    catalog.validate_records(saved["records"])
    if saved != recipe:
        history = catalog.read_records(root, kind="artifact", path=value["source"]["path"],
                                       include_history=True, limit=1, expected_state=state["state_token"])
        require(catalog.marker_present(root, marker)
                or any(version["request_id"] == recipe["request_id"] for record in history["records"]
                       if record["id"] == original["id"] for version in record["versions"]),
                "Uncommitted registration metadata drift; preserve the saved request and reconcile")
    return saved


def _finish(root, path, value, recipe, outcome):
    try:
        completion = path.parent / "completion.json"
        if not completion.exists():
            extract.publish(completion, extract.encoded(outcome))
        else:
            saved = catalog.decode(catalog.private_file(completion).read_text())
            require(saved["request_id"] == recipe["request_id"]
                    and saved["committed_state"] == outcome["committed_state"],
                    "Committed registration differs from saved completion")
        unchanged(root, value)
    except (extract.ExtractionError, catalog.CatalogError, OSError, ValueError, KeyError) as error:
        raise extract.ExtractionError("Catalog registration committed, but completion/final check failed; "
                                      "preserve registration.json and retry the same request: " + str(error)) from error


REGISTER_ERRORS = (extract.ExtractionError, bridge.BridgeError, catalog.CatalogError, OSError,
                   sqlite3.Error, ValueError)


def register_batch(root, items, *, rebase=True):
    """``extract.register_batch`` for PDF/image renditions: one catalog revision for the batch.

    Each item needs its published rendition (``prepared`` from ``bundle``); the source and extractor
    are re-checked under the extraction lock before and after its saved registration is read.
    """
    root = catalog.safe_path(root)
    outcomes = [None] * len(items)
    with catalog.writer_lock(root / HOME):
        ready = []
        for index, item in enumerate(items):
            try:
                state, original, path, value = item["prepared"]
                require(value["source"]["receipt_id"] == item["receipt"] and path.is_relative_to(root),
                        "Prepared bundle is for a different receipt or root")
                require(value["payload"] is not None and path.exists(),
                        "Prepared rendition missing; run prepare for this source/extractor version")
                raw = extract.encoded(value)
                require(catalog.private_file(path).read_bytes() == raw,
                        "Rendition differs from the prepared one; preserve it and investigate")
                require(item["actor"] is not None and item["expected_state"] is not None,
                        "Registration requires actor and explicit expected catalog state")
                unchanged(root, value)
                marker = "A-extract-" + value["run_id"]
                recipe = _recipe(root, original, path, value, state, item["actor"], marker)
                unchanged(root, value)
            except REGISTER_ERRORS as error:
                outcomes[index] = error
                continue
            result = {"command": "register", "receipt_id": item["receipt"], "run_id": value["run_id"],
                      "rendition": path.relative_to(root).as_posix(), "created": False,
                      "source_sha256": value["source"]["sha256"], "rendition_sha256": digest(raw),
                      "catalog_state": state, "boundary": extract.BOUNDARY,
                      "warnings": value["payload"]["warnings"]}
            ready.append((index, item, path, value, recipe, result, marker))
        groups = {}
        for entry in ready:
            groups.setdefault((entry[4]["actor"], entry[4]["reason"]), []).append(entry)
        for (actor, reason), entries in groups.items():
            members = [{"marker": marker, "records": recipe["records"], "expected_state": item["expected_state"]}
                       for _, item, _, _, recipe, _, marker in entries]
            try:
                submitted = catalog.submit_batch(root, members, actor=actor, reason=reason, rebase=rebase)
            except REGISTER_ERRORS as error:
                for entry in entries:
                    outcomes[entry[0]] = error
                continue
            for (index, item, path, value, recipe, result, _), member in zip(entries, submitted["members"]):
                if member["status"] == "refused":
                    outcomes[index] = member["error"]
                    continue
                outcome = {"command": "submit", "idempotent": member["status"] == "present",
                           **member["committed_state"], "state": member["committed_state"],
                           "request_id": recipe["request_id"], "committed_revision": member["committed_revision"],
                           "committed_state": member["committed_state"], "backup": None, "retention": None}
                try:
                    _finish(root, path, value, recipe, outcome)
                except REGISTER_ERRORS as error:
                    outcomes[index] = error
                    continue
                result.update(catalog_state=outcome["state"], registered=True, idempotent=outcome["idempotent"],
                              request_id=recipe["request_id"], committed_revision=member["committed_revision"])
                outcomes[index] = result
    return outcomes


def run(root, command, receipt, format_name, actor=None, expected_state=None, rebase=False, prepared=None):
    """``extract.run`` for PDF/image renditions: ``prepare`` converts and publishes, ``register`` commits."""
    root = catalog.safe_path(root)
    if command == "prepare":
        return prepare(root, receipt, format_name, prepared)
    require(command == "register", "Unsupported PDF/image extraction command")
    prepared = prepared or bundle(root, receipt, format_name)
    outcome = register_batch(root, [{"receipt": receipt, "format_name": format_name, "actor": actor,
                                     "expected_state": expected_state, "prepared": prepared}],
                             rebase=rebase)[0]
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("status", help="Report whether the pinned Docling runtime is provisioned")
    command.add_argument("--root", type=Path, default=TOOLKIT)
    command = commands.add_parser("probe", help="Convert local files without storing anything; print stats")
    command.add_argument("--root", type=Path, default=TOOLKIT)
    command.add_argument("--work", type=Path, required=True, help="Private scratch directory")
    command.add_argument("files", nargs="+", type=Path)
    command = commands.add_parser("child")
    command.add_argument("home", type=Path)
    args = parser.parse_args(argv)
    if args.command == "child":
        return child(args.home)
    if args.command == "status":
        print(json.dumps(status(args.root), sort_keys=True))
        return 0
    paths = runtime(args.root)
    for path in args.files:
        fmt = "pdf" if path.suffix.lower() == ".pdf" else "image"
        started = time.monotonic()
        record = {"file": path.name, "format": fmt, "bytes": path.stat().st_size}
        try:
            payload, stats = convert(path.read_bytes(), path.name, fmt, paths, args.work)
            record.update(ok=True, kind=payload["method"]["kind"], lines=len(payload["lines"]),
                          pages=len(payload["pages"]), stats=stats)
        except ConversionFailed as error:
            record.update(ok=False, code=error.code, stats=getattr(error, "stats", None))
        record["seconds"] = round(time.monotonic() - started, 3)
        print(json.dumps(record, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
