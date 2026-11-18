"""Embedded PDF text-layer reader for one bounded source. No imports beyond stdlib at module import.

Runs as ``python -I pdf_text_worker.py JOB RESULT`` in the pinned Docling environment (which provides
``pypdfium2``), inside ``unshare -Urn``, launched by ``pdf_image``. JOB is a private JSON file naming the
source; RESULT must not exist. The worker never renders pages, runs OCR or loads models: it reports
each page's text layer and the number of image objects on it, and the coordinator decides whether
that text is enough. Encrypted or password-protected PDFs are refused.
"""
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import signal
import sys


MIB = 1024**2
LIMITS = {"source_bytes": 64 * MIB, "pdf_pages": 100, "page_chars": 1_000_000,
          "page_image_objects": 10_000, "result_bytes": 64 * MIB,
          "address_space_bytes": 2 * 1024**3, "cpu_seconds": 120, "wall_seconds": 120}


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


def read(source):
    pdfium, version = _pinned_pdfium()
    if not source.startswith(b"%PDF-"):
        raise WorkerError("source is not a PDF", "format_mismatch")
    try:
        document = pdfium.PdfDocument(source)
    except Exception as error:
        if "password" in str(error).lower():
            raise WorkerError("encrypted PDF is unsupported", "encrypted") from error
        raise WorkerError("malformed PDF", "malformed_source") from error
    try:
        if pdfium.raw.FPDF_GetSecurityHandlerRevision(document.raw) != -1:
            raise WorkerError("encrypted PDF is unsupported", "encrypted")
        count = len(document)
        if not 0 < count <= LIMITS["pdf_pages"]:
            raise WorkerError("invalid or excessive number of PDF pages", "page_limit")
        pages = []
        for index in range(count):
            page = document[index]
            try:
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
            pages.append({"page": index + 1, "text": text, "image_objects": images})
        return {"reader": "pypdfium2", "version": version, "pages": pages}
    except WorkerError:
        raise
    except Exception as error:
        raise WorkerError("malformed PDF", "malformed_source") from error
    finally:
        document.close()


def _limit(kind, ceiling):
    # Never loosen a finite limit the coordinator already imposed.
    for inherited in resource.getrlimit(kind):
        if inherited != resource.RLIM_INFINITY:
            ceiling = min(inherited, ceiling)
    resource.setrlimit(kind, (ceiling, ceiling))


def _timeout(_signal, _frame):
    raise WorkerError("text-layer wall timeout exceeded", "timeout")


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


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: pdf_text_worker.py JOB RESULT", file=sys.stderr)
        return 2
    try:
        _limit(resource.RLIMIT_AS, LIMITS["address_space_bytes"])
        _limit(resource.RLIMIT_CPU, LIMITS["cpu_seconds"])
        _limit(resource.RLIMIT_FSIZE, LIMITS["result_bytes"])
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(LIMITS["wall_seconds"])
        job = json.loads(_read_private(args[0], 64 * 1024))
        if not isinstance(job, dict) or job.get("schema") != "pdf-text-job-v1" or set(job) != {
                "schema", "source_path"}:
            raise WorkerError("invalid job", "invalid_job")
        result = {"schema": "pdf-text-result-v1", "ok": True,
                  "payload": read(_read_private(job["source_path"], LIMITS["source_bytes"]))}
        status = 0
    except MemoryError:
        result = {"schema": "pdf-text-result-v1", "ok": False,
                  "error": {"code": "memory_limit", "message": "text-layer reader exceeded memory"}}
        status = 1
    except Exception as error:
        known = isinstance(error, WorkerError)
        result = {"schema": "pdf-text-result-v1", "ok": False,
                  "error": {"code": error.code if known else "runtime_error",
                            "message": str(error)[:4096] if known else "text-layer reader failed"}}
        status = 1
    raw = (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(raw) > LIMITS["result_bytes"]:
        raw = (json.dumps({"schema": "pdf-text-result-v1", "ok": False, "error": {
            "code": "output_limit", "message": "text-layer result exceeds byte limit"}}) + "\n").encode()
        status = 1
    descriptor = os.open(args[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
    return status


if __name__ == "__main__":
    sys.exit(main())
