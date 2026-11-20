"""Ignored local memo of the expensive half of the index freshness proof (Kata t5df).

``renditions.inputs`` replays the whole review-event history, validates the catalog and re-reads
and re-validates every selected extraction, original and submission sidecar to compute an index's
``rendition_identity``. A reader (``status``, ``search``, ``get``, ``get-many``, claim corrections and
the progress page) reuses an identity proven earlier only while the toolkit code, runtime, policy,
instance roots, selections and UTC date are unchanged and every file and directory the proof read
keeps its exact identity. Any other state, or any doubt, recomputes it in full. Publication (build and
the publication guards) never reuses one; build only leaves one behind.
"""

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
import unicodedata
import uuid

from tools import policy


FORMAT = 1
# A proof whose inputs changed this close to its start is not remembered: a later change could keep
# the same coarse timestamp (git's "racily clean" rule). Every write moves ctime, which utime cannot set.
RACY_NS = 2_000_000_000
LIMIT = 256 * 1024 * 1024
TOOLKIT = Path(__file__).resolve().parents[1]
CODE_SUFFIXES = (".py", ".json", ".sql")
RUNTIME_DISTRIBUTIONS = ("jsonschema", "referencing")
STORE_SIDECARS = ("-journal", "-wal", "-shm")
CATALOG_DB = "catalog.sqlite3"
EVENTS_DB = "events.sqlite3"
_now_ns = time.time_ns
_MISSES = (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError)


def location(db):
    """The memo beside index database ``db``; ignored like the database (``*.sqlite3*``)."""
    return Path(db).with_name(Path(db).name + ".freshness.json")


def needs_review(config):
    """Whether ``renditions.inputs`` reads the review-event history for this selection."""
    return any(item["format"] == "docling" or "review_event_ids" in item
               for item in config.get("renditions", []))


def _code():
    """Every toolkit source and schema file, so any code a proof may run is part of its key."""
    result = hashlib.sha256()
    for directory, names, files in os.walk(TOOLKIT):
        names[:] = sorted(name for name in names if not name.startswith(".") and name != "__pycache__")
        for name in sorted(files):
            if name.endswith(CODE_SUFFIXES):
                path = os.path.join(directory, name)
                with open(path, "rb") as stream:
                    content = hashlib.sha256(stream.read()).digest()
                result.update(os.path.relpath(path, TOOLKIT).encode("utf-8") + b"\0" + content)
    return result.hexdigest()


def _runtime():
    versions = {}
    for name in RUNTIME_DISTRIBUTIONS:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return [sys.version, sqlite3.sqlite_version, unicodedata.unidata_version, versions]


def _key(root, config):
    root = Path(root)
    return hashlib.sha256(json.dumps({
        "format": FORMAT, "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "root": str(root),
        "renditions": json.dumps(config.get("renditions", []), sort_keys=True),
        "policy": policy.fingerprint(root), "roots": policy.validate_instance_roots(root),
        "code": _code(), "runtime": _runtime(),
    }, sort_keys=True).encode("utf-8")).hexdigest()


def _stores(root, config):
    roots = policy.validate_instance_roots(root)
    stores = [Path(root) / roots["catalog_home"] / CATALOG_DB]
    if needs_review(config):
        stores.append(Path(root) / roots["review_events_home"] / EVENTS_DB)
    return stores


def _sidecar_free(stores):
    return not any(os.path.lexists(str(store) + suffix) for store in stores for suffix in STORE_SIDECARS)


def _file(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _directory(path):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("not a directory")
    return [info.st_dev, info.st_ino, info.st_mode]


def _relative(root, path):
    name = os.path.relpath(os.path.abspath(path), root)
    if name == os.curdir or name == os.pardir or name.startswith(os.pardir + os.sep) or os.path.isabs(name):
        raise ValueError("outside the instance root")
    return name


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


def lookup(root, config, db):
    """The remembered identity when every recorded input is exactly as proven, else None."""
    root = str(root)
    try:
        saved = _read(location(db))
        if saved is None or saved.get("key") != _key(root, config) or not isinstance(saved.get("identity"), str):
            return None
        if not _sidecar_free(_stores(root, config)):
            return None
        for name, signature in saved["directories"].items():
            if _directory(os.path.join(root, name) if name != os.curdir else root) != signature:
                return None
        for name, signature in saved["files"].items():
            if _file(os.path.join(root, name)) != signature:
                return None
        return saved["identity"]
    except _MISSES:
        return None


class Proof:
    """One full computation: ``reads`` collects every file it reads; ``store`` remembers its result."""

    def __init__(self, root, config):
        self.root, self.config, self.reads = str(root), config, set()
        self.started = _now_ns()
        try:
            self.key = _key(self.root, config) if config.get("renditions") else None
        except _MISSES:
            self.key = None

    def store(self, db, identity):
        """Remember ``identity`` (best effort); True when written. Nothing changed during the proof."""
        if self.key is None or not isinstance(identity, str) or not identity:
            return False
        try:
            stores = _stores(self.root, self.config)
            files, directories = {}, {os.curdir: _directory(self.root)}
            for path in {*self.reads, *stores}:
                name = _relative(self.root, path)
                files[name] = _file(path)
                parent = os.path.dirname(name)
                while parent and parent not in directories:
                    directories[parent] = _directory(os.path.join(self.root, parent))
                    parent = os.path.dirname(parent)
            if (any(signature[5] >= self.started - RACY_NS for signature in files.values())
                    or not _sidecar_free(stores) or _key(self.root, self.config) != self.key):
                return False
            _write(location(db), {"format": FORMAT, "key": self.key, "identity": identity,
                                  "files": files, "directories": directories})
            return True
        except _MISSES:
            return False


def identity(root, config, db, compute):
    """``compute(reads)`` returns the identity and adds each file it reads to the set ``reads``."""
    if not config.get("renditions"):
        return compute(set())
    found = lookup(root, config, db)
    if found is not None:
        return found
    proof = Proof(root, config)
    value = compute(proof.reads)
    proof.store(db, value)
    return value
