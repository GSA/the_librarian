"""Cheap proof that a read's inputs did not move, so the second freshness check can be skipped (Kata hep6).

A reader checks freshness before it reads and, until now, again after. The second check repeats the
whole memo lookup (about 1.2 s on a 37,000-rendition instance: one ``lstat`` for each of about 300,000
signed files, plus decoding a 25 MB memo). It runs only after the first check passed, so it needs to
show one thing: nothing the result was built from changed meanwhile.

``capture`` takes, before the first check, the identity of the two authority stores and of the freshness
memo (a first check that had to prove everything rewrites the memo, so that read still gets the full
second check). After the read ``unchanged`` shows that all three kept it, and re-derives everything else a freshness verdict
compares at once, for a few milliseconds: the manifest bytes, the policy, engine and instance
metadata, and each indexed document's hash. It never produces a verdict of its own: any difference or
doubt returns False and the caller runs the full second check, which reports exactly what it always did.

A store identity is device, inode, size, mtime, ctime and the absence of a journal or write-ahead
sidecar. It is not trusted when its ctime is within ``RACY_NS`` of the capture (a later write could
keep the same coarse timestamp; every write moves ctime, which ``utime`` cannot set).

What the cheap proof does not cover is a selected extraction, original or submission sidecar edited
outside the application during one read. The next read's freshness check (or, with a recorded hash, the
integrity audit) finds it; the application itself only changes those files by way of a catalog or
review-event commit, which moves a store identity.
"""

from pathlib import Path
import time

from tools import policy
from tools.context_index import context_index as engine, proof_cache

RACY_NS = 2_000_000_000
CATALOG_DB = "catalog.sqlite3"
EVENTS_DB = "events.sqlite3"
SIDECARS = ("-journal", "-wal", "-shm")
_now_ns = time.time_ns
_MISSES = (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, engine.IndexError_)


def _identity(path):
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _stores(root):
    roots = policy.validate_instance_roots(root)
    base = Path(root)
    return [base / roots["catalog_home"] / CATALOG_DB, base / roots["review_events_home"] / EVENTS_DB]


def _identities(root, db):
    result = {"memo": _identity(proof_cache.location(db))}
    for store in _stores(root):
        for suffix in ("", *SIDECARS):
            result[str(store) + suffix] = _identity(str(store) + suffix)
    return result


def capture(root, db):
    """The stores' and freshness memo's identities, or None when a store is racily new or has a sidecar."""
    started = _now_ns()
    try:
        found = _identities(root, db)
    except _MISSES:
        return None
    for path, identity in found.items():
        if path != "memo" and identity is not None and (path.endswith(SIDECARS) or identity[5] >= started - RACY_NS):
            return None
    return found


def unchanged(before, connection, root, db, manifest, config, manifest_hash):
    """True only when every input the first check proved is demonstrably as it was; False on any doubt."""
    if before is None or before["memo"] is None:
        return False
    try:
        if _identities(root, db) != before:
            return False
        if engine.digest(engine.no_symlinks(manifest).read_bytes()) != manifest_hash:
            return False
        actual = dict(connection.execute("SELECT key,value FROM metadata"))
        # The stored rendition identity stands for the proof the first check made; every other field is
        # re-derived from what is on disk now.
        expected = engine.metadata_expected(root, manifest_hash, config,
                                            rendition_identity=actual["rendition_identity"])
        if any(actual.get(key) != value for key, value in expected.items()):
            return False
        stored = dict(connection.execute(
            "SELECT path,hash FROM documents WHERE path NOT IN (SELECT path FROM rendition_documents)"))
        if set(stored) != {document["path"] for document in config["documents"]}:
            return False
        return all(engine.digest(engine.source_path(root, path).read_bytes()) == value
                   for path, value in stored.items())
    except _MISSES:
        return False
