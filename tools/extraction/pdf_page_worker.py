"""Per-page PDF reader for one bounded source. No imports beyond stdlib at module import.

Runs as ``python -I pdf_page_worker.py JOB RESULT`` in the pinned Docling environment (which provides
``pypdfium2``), inside ``unshare -Urn``, launched by ``pdf_pages``. JOB is a private JSON file; RESULT
must not exist. Two operations, both on the untrusted PDF named by the job:

- ``inventory``: the page count and, for each of the first ``max_pages`` pages, its text layer, the
  number of image objects and its size in points. A page that cannot be read is reported with its
  own error code; it does not fail the document. Encrypted or password-protected PDFs are refused.
- ``render``: one page rasterised (grayscale) to a PNG at ``scale`` (at most 3, i.e. 216 DPI), reduced
  further so the raster fits ``max_pixels``. The PNG is written to ``image_path``.

The worker never runs OCR or loads models; the coordinator decides what each page needs.
"""
import importlib.metadata
import json
import math
import os
from pathlib import Path
import resource
import signal
import struct
import sys
import zlib


MIB = 1024**2
LIMITS = {"source_bytes": 64 * MIB, "page_chars": 1_000_000, "page_image_objects": 10_000,
          "result_bytes": 64 * MIB, "image_bytes": 128 * MIB, "address_space_bytes": 2 * 1024**3,
          "cpu_seconds": 300, "wall_seconds": 300}
JOB_FIELDS = {"inventory": {"schema", "operation", "source_path", "max_pages"},
              "render": {"schema", "operation", "source_path", "page", "scale", "max_pixels", "image_path"}}


class WorkerError(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def _pinned_pdfium():
    """The pypdfium2 module at exactly the version the Docling lock pins."""
    lock = Path(__file__).with_name("requirements-docling.txt").read_text()
    expected = next((line.split("==", 1)[1].strip() for line in lock.splitlines()
                     if line.startswith("pypdfium2==")), None)
    try:
        actual = importlib.metadata.version("pypdfium2")
        import pypdfium2
    except (importlib.metadata.PackageNotFoundError, ImportError) as error:
        raise WorkerError("pypdfium2 is not installed", "missing_dependency") from error
    if expected is None or actual != expected:
        raise WorkerError("pypdfium2 version differs from the lock", "runtime_mismatch")
    return pypdfium2, actual


def _clean(text):
    """Lines of one page: CR/CRLF become LF, C0 controls other than tab become spaces."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(" " if (ord(c) < 32 and c not in "\n\t") or c == "\x7f" else c for c in text)


def _open(pdfium, source):
    if not source.startswith(b"%PDF-"):
        raise WorkerError("source is not a PDF", "format_mismatch")
    try:
        document = pdfium.PdfDocument(source)
    except Exception as error:
        if "password" in str(error).lower():
            raise WorkerError("encrypted PDF is unsupported", "encrypted") from error
        raise WorkerError("malformed PDF", "malformed_source") from error
    if pdfium.raw.FPDF_GetSecurityHandlerRevision(document.raw) != -1:
        document.close()
        raise WorkerError("encrypted PDF is unsupported", "encrypted")
    return document


def _page(pdfium, document, index):
    page = document[index]
    try:
        width, height = page.get_size()
        textpage = page.get_textpage()
        try:
            if textpage.count_chars() > LIMITS["page_chars"]:
                raise WorkerError("page text exceeds character limit", "output_limit")
            text = _clean(textpage.get_text_range())
        finally:
            textpage.close()
        images = 0
        for _ in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE], max_depth=4):
            images += 1
            if images >= LIMITS["page_image_objects"]:
                break
    finally:
        page.close()
    return {"page": index + 1, "text": text, "image_objects": images,
            "width": round(float(width), 3), "height": round(float(height), 3)}


def inventory(source, max_pages):
    pdfium, version = _pinned_pdfium()
    document = _open(pdfium, source)
    try:
        count = len(document)
        if count < 1:
            raise WorkerError("PDF has no pages", "page_limit")
        pages = []
        for index in range(min(count, max_pages)):
            try:
                pages.append(_page(pdfium, document, index))
            except WorkerError as error:
                pages.append({"page": index + 1, "error": error.code})
            except Exception:  # noqa: BLE001 - one unreadable page is that page's failure
                pages.append({"page": index + 1, "error": "malformed_page"})
        return {"reader": "pypdfium2", "version": version, "page_count": count, "pages": pages}
    except WorkerError:
        raise
    except Exception as error:
        raise WorkerError("malformed PDF", "malformed_source") from error
    finally:
        document.close()


def png(width, height, rows, channels):
    """PNG bytes of 8-bit gray (1 channel) or RGB (3 channels) ``rows``."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", width, height, 8, 0 if channels == 1 else 2, 0, 0, 0)
    compressor = zlib.compressobj(6)
    data = [compressor.compress(b"\x00" + row) for row in rows]
    data.append(compressor.flush())
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", b"".join(data)) + chunk(b"IEND", b"")


def render(source, number, scale, max_pixels):
    """``(png bytes, report)`` of page ``number`` at ``scale``, reduced to fit ``max_pixels``."""
    pdfium, version = _pinned_pdfium()
    document = _open(pdfium, source)
    try:
        if not 1 <= number <= len(document):
            raise WorkerError("page out of range", "page_limit")
        page = document[number - 1]
        try:
            width, height = page.get_size()
            if not (width > 0 and height > 0):
                raise WorkerError("page has no area", "malformed_page")
            # Rotation swaps the raster sides but not its area.
            scale = min(scale, math.sqrt(max_pixels / (width * height)))
            while math.ceil(width * scale) * math.ceil(height * scale) > max_pixels:
                scale *= 0.999  # pdfium rounds the bitmap size up
            bitmap = page.render(scale=scale, grayscale=True, rev_byteorder=True)
            try:
                w, h, stride = bitmap.width, bitmap.height, bitmap.stride
                if w * h > max_pixels:
                    raise WorkerError("rendered page exceeds pixel limit", "pixel_limit")
                channels = {pdfium.raw.FPDFBitmap_Gray: 1, pdfium.raw.FPDFBitmap_BGR: 3}.get(bitmap.format)
                if channels is None:
                    raise WorkerError("unexpected bitmap format", "runtime_error")
                buffer = bytes(bitmap.buffer)
                raw = png(w, h, (buffer[y * stride:y * stride + w * channels] for y in range(h)), channels)
            finally:
                bitmap.close()
        finally:
            page.close()
        return raw, {"reader": "pypdfium2", "version": version, "width": w, "height": h,
                     "scale": round(scale, 6)}
    except WorkerError:
        raise
    except Exception as error:
        raise WorkerError("page cannot be rendered", "malformed_page") from error
    finally:
        document.close()


def _limit(kind, ceiling):
    # Never loosen a finite limit the coordinator already imposed.
    for inherited in resource.getrlimit(kind):
        if inherited != resource.RLIM_INFINITY:
            ceiling = min(inherited, ceiling)
    resource.setrlimit(kind, (ceiling, ceiling))


def _timeout(_signal, _frame):
    raise WorkerError("page worker wall timeout exceeded", "timeout")


def _read_private(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise WorkerError("job inputs must be private files", "invalid_job")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise WorkerError("source exceeds byte limit", "source_limit")
    return raw


def _write_new(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


def _job(raw):
    job = json.loads(raw)
    fields = JOB_FIELDS.get(job.get("operation")) if isinstance(job, dict) else None
    if fields is None or job.get("schema") != "pdf-page-job-v1" or set(job) != fields:
        raise WorkerError("invalid job", "invalid_job")
    numbers = [job[k] for k in ("max_pages", "page", "max_pixels") if k in job]
    if not all(type(n) is int and n > 0 for n in numbers) or (
            "scale" in job and not (type(job["scale"]) in (int, float) and 0 < job["scale"] <= 3)):
        raise WorkerError("invalid job", "invalid_job")
    return job


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: pdf_page_worker.py JOB RESULT", file=sys.stderr)
        return 2
    try:
        _limit(resource.RLIMIT_AS, LIMITS["address_space_bytes"])
        _limit(resource.RLIMIT_CPU, LIMITS["cpu_seconds"])
        _limit(resource.RLIMIT_FSIZE, LIMITS["image_bytes"])
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(LIMITS["wall_seconds"])
        job = _job(_read_private(args[0], 64 * 1024))
        source = _read_private(job["source_path"], LIMITS["source_bytes"])
        if job["operation"] == "inventory":
            payload = inventory(source, job["max_pages"])
        else:
            image, payload = render(source, job["page"], job["scale"], job["max_pixels"])
            _write_new(job["image_path"], image)
        result = {"schema": "pdf-page-result-v1", "ok": True, "payload": payload}
        status = 0
    except MemoryError:
        result = {"schema": "pdf-page-result-v1", "ok": False,
                  "error": {"code": "memory_limit", "message": "page worker exceeded memory"}}
        status = 1
    except Exception as error:
        known = isinstance(error, WorkerError)
        result = {"schema": "pdf-page-result-v1", "ok": False,
                  "error": {"code": error.code if known else "runtime_error",
                            "message": str(error)[:4096] if known else "page worker failed"}}
        status = 1
    signal.alarm(0)
    raw = (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(raw) > LIMITS["result_bytes"]:
        raw = (json.dumps({"schema": "pdf-page-result-v1", "ok": False, "error": {
            "code": "output_limit", "message": "page worker result exceeds byte limit"}}) + "\n").encode()
        status = 1
    _write_new(args[1], raw)
    return status


if __name__ == "__main__":
    sys.exit(main())
