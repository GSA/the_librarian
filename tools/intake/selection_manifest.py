"""The shared selection manifest read cheaply and appended to in constant time (Kata m0tg).

``qualify`` adds one selection per qualified rendition and asks, per entry, whether the manifest
has room. Re-reading, revalidating and rewriting the whole file each time made filling it
quadratic (274 ms per select at 20,000 entries). Two things change the cost, not the file:

* **A view remembered by file identity.** One full, validated read (``index.load_manifest`` plus a
  byte-for-byte comparison with the canonical rendering) is kept in memory for the file's identity
  (device, inode, size, mtime and ctime). The count, the selected paths
  and the running SHA-256 of everything before the closing of the ``renditions`` array answer the
  per-entry questions without reading the file. A changed identity is a miss: the next call reads
  and validates in full, exactly as before. The view is never written to disk.
* **An in-place append.** The canonical manifest (``json.dumps(indent=2, sort_keys=True)``) ends in
  a fixed tail after its last selection, so one more selection is the bytes ``,\\n    {entry}`` plus
  that tail written at the end of the last selection. The result is byte-for-byte the file a full
  rewrite produces. Before it is written, the intended bytes go to a small fsynced intent file, so
  a crash between the intent and the manifest write is rolled forward by the next writer
  (``recover``) rather than leaving a torn file. Only the new entry is validated; every other
  entry was validated when the view was read, and publication still validates the whole file.

Writers hold the ledger writer lock (``standing-select`` runs inside a stage run, and
``qualify`` reconciles under it), so they never overlap. A reader outside the lock may, rarely,
see a write in flight; it sees invalid JSON and refuses, never a different valid manifest.
Anything the fast path cannot do (an empty list, a replacement, a file that is not canonical, a
view that no longer matches the file) takes the original full read, validate and atomic replace.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time

from tools.context_index import context_index as index
from tools.context_index import renditions

TAIL = b'\n  ],\n  "schema_version": 1\n}\n'
_LOCK = threading.Lock()
_VIEWS = {}
_LIMIT = 8
READ_TRIES = 4


class View:
    """What one validated read of a manifest file established; callers only read it."""

    def __init__(self, identity, count, paths, end, prefix):
        self.identity, self.count, self.paths = identity, count, paths
        self.end, self.prefix = end, prefix

    def has(self, path):
        return path in self.paths

    def digest(self):
        """SHA-256 of the whole file, from the running hash of its prefix and the fixed tail."""
        final = self.prefix.copy()
        final.update(TAIL)
        return final.hexdigest()


def _identity(path):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"Not a regular file: {path}")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def forget(path=None):
    with _LOCK:
        if path is None:
            _VIEWS.clear()
        else:
            _VIEWS.pop(str(path), None)


def _intent(path):
    return path.with_name("." + path.name + ".append-intent")


def intent_path(root, name):
    return _intent(Path(root) / name)


def _read(path, root):
    """One validated read: a View when the file is stable, canonical and has a selection."""
    before = _identity(path)
    data = path.read_bytes()
    config, digest = index.load_manifest(path, root)
    if index.digest(data) != digest or _identity(path) != before:
        raise OSError("Manifest changed while it was read; retry")
    items = config.get("renditions", [])
    raw = json.loads(data.decode("utf-8"), object_pairs_hook=index.strict_object)
    canonical = (json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    end = len(data) - len(TAIL) if items and data == canonical and data.endswith(TAIL) else None
    prefix = hashlib.sha256(data[:end] if end is not None else data)
    return before, View(before, len(items), {i["path"] for i in items}, end, prefix)


def view(root, name):
    """The manifest's view, served from memory while the file is exactly as last validated."""
    path = index.no_symlinks(Path(root) / name)
    key = str(path)
    try:
        identity = _identity(path)
    except OSError:
        forget(key)
        raise
    with _LOCK:
        known = _VIEWS.get(key)
    if known is not None and known.identity == identity:
        return known
    for attempt in range(READ_TRIES):
        try:
            _, fresh = _read(path, root)
            break
        except (index.IndexError_, ValueError, OSError):
            # A writer outside this thread may be mid-append (readers take no lock); a file that is
            # still invalid after a few short waits is refused exactly as a full read refuses it.
            if attempt == READ_TRIES - 1:
                raise
            time.sleep(0.02 * (attempt + 1))
    with _LOCK:
        if key not in _VIEWS and len(_VIEWS) >= _LIMIT:
            _VIEWS.clear()
        _VIEWS[key] = fresh
    return fresh


def recover(root, name):
    """Complete or discard an append a crashed writer left behind; call under the writer lock."""
    path = index.no_symlinks(Path(root) / name)
    intent = _intent(path)
    if not os.path.lexists(intent):
        return False
    forget(path)
    try:
        record = json.loads(intent.read_text(encoding="utf-8"))
        offset, prefix, text = record["offset"], record["prefix_sha256"], record["text"].encode("utf-8")
        if type(offset) is not int:
            raise ValueError("offset")
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        # An intent that was itself torn before its fsync completed never reached the manifest.
        _remove(intent)
        return True
    data = path.read_bytes()
    # Only a file in one of the states the append passes through is completed: exactly as the writer
    # saw it (the closing tail follows the prefix), or holding some leading part of the new bytes.
    # Anything else is not this writer's file any more, and its content is the validator's
    # business. A failed write keeps the intent (the error propagates) so the next writer retries.
    if (len(data) >= offset and hashlib.sha256(data[:offset]).hexdigest() == prefix
            and _in_flight(data[offset:], text)):
        descriptor = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
        try:
            os.pwrite(descriptor, text, offset)
            os.ftruncate(descriptor, offset + len(text))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _remove(intent)
    return True


def _in_flight(rest, text):
    """True when ``rest`` (the file after the prefix) is the old tail, or the old tail overwritten
    by a leading part of ``text``, or a leading part of ``text`` alone: what an interrupted write
    of ``text`` over the tail can leave."""
    if rest == TAIL or text.startswith(rest):
        return True
    for written in range(1, len(TAIL)):
        if rest == text[:written] + TAIL[written:]:
            return True
    return False


def _remove(intent):
    try:
        os.unlink(intent)
    except FileNotFoundError:
        return
    _sync_directory(intent.parent)


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _element(entry):
    text = json.dumps(entry, indent=2, sort_keys=True, ensure_ascii=False)
    return "\n".join("    " + line for line in text.split("\n"))


def append(root, name, entry, current):
    """Add ``entry`` after the last selection in constant time; return the file's SHA-256.

    ``current`` is ``view(root, name)``, read under the same writer lock. Returns ``None``, with
    nothing written, when the fast path does not apply (the caller then takes the full rewrite).
    The caller has already checked capacity and that the path is not selected.
    """
    path = index.no_symlinks(Path(root) / name)
    if current.end is None or _identity(path) != current.identity:
        return None
    engine = index.rendition_api(root)
    pattern = renditions._eligible_pattern(root)
    # The caller has refused a selected path already; only this entry's own shape is checked.
    renditions.validate_item(entry, engine, pattern, set(), set())
    tail = TAIL.decode()
    text = (",\n" + _element(entry) + tail).encode("utf-8")
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        if os.pread(descriptor, len(TAIL), current.end) != TAIL:
            return None
        prefix = current.prefix.copy()
        prefix.update(text[:-len(TAIL)])
        record = json.dumps({"offset": current.end, "prefix_sha256": current.prefix.hexdigest(),
                             "text": text.decode("utf-8")})
        intent = _intent(path)
        forget(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        written = os.open(intent, flags, 0o600)
        try:
            os.write(written, record.encode("utf-8"))
            os.fsync(written)
        finally:
            os.close(written)
        _sync_directory(path.parent)
        os.pwrite(descriptor, text, current.end)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _remove(_intent(path))
    final = prefix.copy()
    final.update(TAIL)
    with _LOCK:
        # In place: copying the path set per append would make filling the manifest quadratic.
        current.paths.add(entry["path"])
        current.count += 1
        current.end += len(text) - len(TAIL)
        current.prefix, current.identity = prefix, _identity(path)
        _VIEWS[str(path)] = current
    return final.hexdigest()
