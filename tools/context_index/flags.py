"""Integrity flags: what the last recorded audit found, applied to documents at read time (Kata ejm8).

``integrity.audit --record`` writes a private, checksummed report beside the index
(``<index>.integrity.json``). Readers never hash a file: they load that report once per call and mark
each indexed document whose extraction, original or submission sidecar the audit found changed with the
low-confidence reason ``integrity-changed``. The document stays readable (its indexed text is the text
verified at ingest); the label says the retained files behind its citations no longer match their pins.

A report applies only to the index database it was recorded against (its file identity). A rebuild
replaces that file, so its flags end with it, and the next audit re-establishes whatever is still wrong.
A missing, unreadable, forged or foreign report means no flags: detection is not an access control.
"""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from urllib.parse import quote
import uuid

from tools.context_index import confidence

FORMAT = 1
LIMIT = 64 * 1024 * 1024
# Every flagged document is recorded; only the changed files kept per document are bounded.
DOCUMENT_FILES = 3
LISTED = 20
DETAIL = 200
_MISSES = (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError)
RESOLVE = ("Restore the file from backup or re-ingest the document through intake (never edit files by "
           "hand), rebuild the index (python -m tools.context_index.cli build), then re-run "
           "python -m tools.context_index.integrity --root ROOT audit --record.")


def location(db):
    """The report beside index database ``db``; ignored like the database (``*.sqlite3*``)."""
    return Path(db).with_name(Path(db).name + ".integrity.json")


def _identity(db):
    info = os.stat(db)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _checksum(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _read(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(descriptor)
        # Only a private regular file this user wrote is ever trusted.
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid()
                or info.st_size > LIMIT):
            return None
        raw = stream.read(LIMIT + 1)
    record = json.loads(raw.decode("utf-8"))
    if not isinstance(record, dict) or record.get("format") != FORMAT:
        return None
    signed = {key: value for key, value in record.items() if key != "checksum"}
    return signed if record.get("checksum") == _checksum(signed) else None


def _write(path, record):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({**record, "checksum": _checksum(record)}, stream, sort_keys=True, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def record(db, report, audited_at, before):
    """Record ``report`` (an ``integrity.audit`` result listing every mismatch) against index ``db``.

    True when written. ``before`` is ``identity(db)`` taken before the audit started: a report is not
    recorded for an index that was replaced while the audit ran. The documents each mismatch affects
    are resolved here, from the index, so every flagged document is recorded however many files
    changed; only the changed files listed per document are bounded.
    """
    if _identity(db) != before:
        return False
    items = [{key: item.get(key) for key in ("path", "kind", "problem", "expected", "found", "receipt")}
             for item in report["items"]]
    connection = sqlite3.connect(f"file:{quote(str(db), safe='/')}?mode=ro", uri=True)
    try:
        documents = _documents(connection, items)
    finally:
        connection.close()
    if _identity(db) != before:
        return False
    _write(location(db), {
        "format": FORMAT, "index": before, "audited_at": audited_at, "checked": report["checked"],
        "mismatches": report["mismatches"],
        "documents": {path: found[:DOCUMENT_FILES] for path, found in documents.items()},
        "document_files": {path: len(found) for path, found in documents.items()}})
    return True


def identity(db):
    return _identity(db)


class Flags:
    """The flagged documents of one index as of the recorded audit (empty without a matching report)."""

    def __init__(self, recorded=False, audited_at=None, checked=0, mismatches=0, documents=None, files=None):
        self.recorded, self.audited_at, self.checked = recorded, audited_at, checked
        self.mismatches = mismatches
        self.documents = documents or {}
        self.files = files or {}

    def apply(self, path, current):
        """``current`` (a confidence label) with ``integrity-changed`` when ``path`` is flagged."""
        found = self.documents.get(path)
        if not found:
            return current
        details = [self._detail(item) for item in found]
        return confidence.with_reason(current, "integrity-changed", details)

    def label_text(self, path, text):
        """The stored label text of ``path`` with the flag applied (unchanged when not flagged)."""
        if path not in self.documents:
            return text
        return json.dumps(self.apply(path, json.loads(text)), ensure_ascii=False)

    def _detail(self, item):
        return ("%s %s: %s; found by the audit of %s" % (
            item["kind"], _short(item["path"]), item["problem"], self.audited_at))

    def summary(self):
        """The body-free ``integrity`` block of ``status`` and the progress page."""
        value = {"audit": "recorded" if self.recorded else "none-for-this-index"}
        if not self.recorded:
            value["notice"] = ("No integrity audit is recorded for this index, so no document can be flagged; "
                               "run python -m tools.context_index.integrity --root ROOT audit --record.")
            return value
        value.update(audited_at=self.audited_at, checked=self.checked, mismatches=self.mismatches,
                     flagged_documents=len(self.documents))
        if self.mismatches:
            listed = []
            for path in sorted(self.documents)[:LISTED]:
                listed.append({"path": _short(path), "changed_files": self.files.get(path, len(self.documents[path])),
                               "files": [
                    {key: _short(item[key]) if isinstance(item[key], str) else item[key]
                     for key in ("kind", "path", "problem", "expected", "found")}
                    for item in self.documents[path]]})
            value["documents"] = listed
            if len(self.documents) > LISTED:
                value["documents_not_listed"] = len(self.documents) - LISTED
            value["resolve"] = RESOLVE
        return value


def _short(text):
    text = str(text)
    return text if len(text) <= DETAIL else text[:DETAIL] + "…"


def _database(connection):
    for _, name, file in connection.execute("PRAGMA database_list"):
        if name == "main" and file:
            return Path(file)
    return None


def load(connection, root=None):
    """The ``Flags`` of the index ``connection`` reads; none without a report recorded for this very index."""
    try:
        db = _database(connection)
        saved = None if db is None else _read(location(db))
        if saved is None or saved.get("index") != _identity(db):
            return Flags()
        documents, files = {}, {}
        indexed = {row[0] for row in connection.execute("SELECT path FROM documents")}
        for path, found in saved["documents"].items():
            if path not in indexed or not isinstance(found, list) or not found:
                continue
            items = [item for item in found if isinstance(item, dict)
                     and all(isinstance(item.get(key), str) for key in ("path", "kind", "problem"))]
            for item in items:
                item.setdefault("expected", None)
                item.setdefault("found", None)
            if items:
                documents[path] = items
                files[path] = max(int(saved["document_files"].get(path, 0)), len(items))
        return Flags(True, str(saved["audited_at"]), int(saved["checked"]), int(saved["mismatches"]),
                     documents, files)
    except _MISSES:
        return Flags()


def _documents(connection, items):
    """``{document path: [mismatch]}``: a document is flagged by its own path, its original or its sidecar."""
    if not items:
        return {}
    flagged = {}
    indexed = {row[0] for row in connection.execute("SELECT path FROM documents")}
    by_original, by_receipt = {}, {}
    for item in items:
        if item["path"] in indexed:
            flagged.setdefault(item["path"], []).append(item)
        if item["kind"] == "original":
            by_original.setdefault(item["path"], []).append(item)
        elif item["kind"] == "sidecar" and item.get("receipt"):
            by_receipt.setdefault(item["receipt"], []).append(item)
    if by_original or by_receipt:
        for path, source, receipt in connection.execute(
                "SELECT path, json_extract(payload,'$.source.path'), json_extract(payload,'$.source.receipt_id') "
                "FROM rendition_documents"):
            for item in (*by_original.get(source, ()), *by_receipt.get(receipt, ())):
                # An entry exists only for a document some changed file affects.
                if item not in flagged.get(path, ()):
                    flagged.setdefault(path, []).append(item)
    return flagged
