"""PDF and image files in cohort intake: classification and ahead-of-ledger conversion.

PDFs (``.pdf``) and images (``.png``, ``.jpg``, ``.jpeg``) are read through the pinned offline Docling
runtime (``tools/extraction/pdf_image.py``) when this instance has it provisioned, and otherwise stay
``needs-runtime`` with code ``docling-not-provisioned``. Conversion is slow, so a chunk's conversions run
before its ledger stages, up to ``concurrency`` at once, each in its own limited child process; the
ledger's prepare stage then finds the rendition already published (or the file's recorded refusal)
and never converts under the ledger.
"""
import hashlib

from tools.extraction import pdf_image
from . import store, upload
from .contracts import IntakeError

SUFFIXES = {".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image"}
NOT_PROVISIONED = pdf_image.NOT_PROVISIONED
DEFAULT_CONCURRENCY = pdf_image.DEFAULT_CONCURRENCY
# Ledger stage states that will run again; any other head was already decided.
OPEN = {"staged", "running", "retryable"}


def ready(root):
    """Whether this instance can read PDFs and images (checked once per classification)."""
    return pdf_image.provisioned(root)


def unread_disposition(path, size, provisioned):
    """The disposition of a PDF/image from its name and size, or None when it must be read."""
    fmt = SUFFIXES[path.suffix.lower()]
    if size > upload.FORMATS[fmt][1]:
        return {"format": fmt, "disposition": "over-limit"}
    if not provisioned:
        return {"format": fmt, "disposition": "needs-runtime", "code": NOT_PROVISIONED}
    return None


def read_disposition(path, entry):
    """The disposition of a PDF/image from its bytes; ``entry`` gains its format and hash."""
    fmt = SUFFIXES[path.suffix.lower()]
    try:
        raw = upload.read_file(path, upload.FORMATS[fmt][1])
    except IntakeError as error:
        return {**entry, "disposition": "unsafe-path", "code": error.code}
    entry.update(format=fmt, sha256=hashlib.sha256(raw).hexdigest())
    try:
        upload.check_type(raw, fmt, path.name)
    except IntakeError as error:
        return {**entry, "disposition": "unreadable", "code": error.code}
    refusal = pdf_image.pixel_refusal(raw, fmt)
    if refusal:
        # A header declaring a decompression bomb is refused here, before any decoder sees it.
        return {**entry, "disposition": "over-limit", "code": refusal.replace("_", "-")}
    return {**entry, "disposition": "admit"}


def convert_ahead(root, entries, heads, concurrency, prepare_name):
    """Convert the chunk's PDF/image entries whose prepare stage is still to run; returns how many.

    ``prepare_name(entry)`` is the prepare request's correlation ID. Outcomes are not reported here:
    the ledger's prepare stage that follows records each one (a published rendition or the file's
    refusal code), so a crash between the two is completed by the next run.
    """
    requests = []
    for entry in entries:
        if entry["format"] not in pdf_image.FORMATS:
            continue
        try:
            current = store.find_head(root, prepare_name(entry), heads)
        except IntakeError:
            continue  # a chain the store cannot follow is reported by the prepare stage
        if current is None or current["stages"][0]["status"] in OPEN:
            requests.append((entry["receipt_id"], entry["format"]))
    if requests:
        pdf_image.prepare_many(root, requests, concurrency)
    return len(requests)
