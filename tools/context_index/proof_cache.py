"""Ignored local memo of the index freshness identity (Kata t5df; simplified by Kata xv0n).

``renditions.pinned_identity`` reads the catalog's pins for every selection and the review-event
state: seconds on a 40,000-selection instance, no selected file opened. A reader (``status``,
``search``, ``get``, ``get-many``, claim corrections, the progress page) reuses an identity computed
earlier while the toolkit code, runtime, policy, instance roots and selections are unchanged and
the catalog and review-event stores keep the exact file identity they had. Any other state, or any
doubt, recomputes it. Publication never reuses one; build leaves its identity behind.

The memo signs only the two stores. It no longer signs selected files (they are not inputs of the
identity) and has no daily expiry (a change to a retained file is ``integrity.audit``'s to report).
"""

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


FORMAT = 2
# A store changed this close to the start of a computation is not remembered: a later change could
# keep the same coarse timestamp (git's "racily clean" rule). Every write moves ctime, which utime
# cannot set.
RACY_NS = 2_000_000_000
LIMIT = 1024 * 1024
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
    """Whether the identity reads the review-event state for this selection."""
    return any(item["format"] == "docling" or "review_event_ids" in item
               for item in config.get("renditions", []))


def _code():
    """Every toolkit source and schema file, so any code a computation may run is part of its key."""
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
        "format": FORMAT, "root": str(root),
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


def _signature(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


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
    """The remembered identity when both stores are exactly as it was computed from, else None."""
    root = str(root)
    try:
        saved = _read(location(db))
        if saved is None or saved.get("key") != _key(root, config) or not isinstance(saved.get("identity"), str):
            return None
        stores = _stores(root, config)
        if not _sidecar_free(stores):
            return None
        for name, signature in saved["stores"].items():
            if _signature(os.path.join(root, name)) != signature:
                return None
        if set(saved["stores"]) != {os.path.relpath(store, root) for store in stores}:
            return None
        return saved["identity"]
    except _MISSES:
        return None


class Computation:
    """One computation of an identity; ``store`` remembers its result only if nothing changed under it."""

    def __init__(self, root, config):
        self.root, self.config = str(root), config
        self.started = _now_ns()
        try:
            self.key = _key(self.root, config) if config.get("renditions") else None
            self.stores = {name: _signature(os.path.join(self.root, name)) for name in (
                os.path.relpath(store, self.root) for store in _stores(self.root, config))}
        except _MISSES:
            self.key = None

    def store(self, db, identity):
        """Remember ``identity`` (best effort); True when written."""
        if self.key is None or not isinstance(identity, str) or not identity:
            return False
        try:
            stores = _stores(self.root, self.config)
            now = {name: _signature(os.path.join(self.root, name)) for name in self.stores}
            if (now != self.stores or any(signature[5] >= self.started - RACY_NS for signature in now.values())
                    or not _sidecar_free(stores) or _key(self.root, self.config) != self.key):
                return False
            _write(location(db), {"format": FORMAT, "key": self.key, "identity": identity, "stores": now})
            return True
        except _MISSES:
            return False


def identity(root, config, db, compute):
    """The remembered identity, else ``compute()`` (remembered when the stores did not change meanwhile)."""
    if not config.get("renditions"):
        return compute()
    found = lookup(root, config, db)
    if found is not None:
        return found
    computation = Computation(root, config)
    value = compute()
    computation.store(db, value)
    return value
