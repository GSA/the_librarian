"""Per-chunk reuse of path-safety and policy validation (Kata ajmk).

Outside a scope nothing is cached: every check runs in full, exactly as before. Inside a scope
(``scoped()``, opened by the ingest loop and renewed with ``boundary()`` at the start of each chunk)
three things are remembered for that thread only:

* ancestor directories already found free of symlinks, each with its lstat identity (``has_symlink``);
* results that are a pure function of file identities (``memo``): the policy for an instance root,
  the extractor fingerprint, the validated storage roots;
* nothing else. A refusal is never remembered.

What a scope never skips: the path being checked and its parent are always ``lstat``-ed, a path that
does not exist yet (one about to be created) is checked in full, and the walk up stops only at an
ancestor whose ``lstat`` identity still equals the recorded one. A symlink swapped in above that
ancestor while its own identity stays the same (the real directory renamed away and linked back) is
noticed at the next chunk boundary, not inside the chunk. The cache is thread-local, so another thread, another process and another root never
share it.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import threading

_local = threading.local()


class _Scope:
    __slots__ = ("clean", "values")

    def __init__(self):
        self.clean = {}
        self.values = {}


def current():
    return getattr(_local, "scope", None)


@contextmanager
def scoped():
    previous = current()
    _local.scope = _Scope()
    try:
        yield
    finally:
        _local.scope = previous


def boundary():
    """Forget everything this thread's scope remembers; the next check validates in full."""
    scope = current()
    if scope is not None:
        scope.clean.clear()
        scope.values.clear()


def identity(path):
    """(device, inode, size, mtime_ns, ctime_ns, mode) from ``lstat``, or None when absent."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


def memo(key, compute):
    """``compute()``, remembered under ``key`` for the scope; a raised refusal is not remembered."""
    scope = current()
    if scope is None:
        return compute()
    values = scope.values
    if key not in values:
        values[key] = compute()
    return values[key]


def _full(path):
    return any(part.is_symlink() for part in (path, *path.parents))


def _dir_id(path):
    """(device, inode, mode) of ``path`` itself (``lstat``); None when it does not exist."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (info.st_dev, info.st_ino, info.st_mode)


def has_symlink(path, probe=False):
    """True when ``path`` or any of its ancestors is a symlink.

    Outside a scope this is the plain walk. Inside one, ``path`` and its parent are always
    ``lstat``-ed; a higher ancestor already found clean is trusted only while an ``lstat`` of it still
    returns the (device, inode, mode) recorded then, and the walk stops at the first such ancestor.
    A path that does not exist yet is the target of a write that will create it, so it gets the full
    walk unless ``probe`` says the caller only tests whether it exists (a SQLite sidecar or journal).
    """
    path = Path(path)
    scope = current()
    if scope is None:
        return _full(path)
    first = _dir_id(path)
    if first is None and not probe:
        return _full(path)
    clean, pending, seen = scope.clean, [], 0
    for part in (path, *path.parents):
        found = first if part is path or part == path else _dir_id(part)
        if found is None:
            continue  # absent, so it cannot be a link
        if stat.S_ISLNK(found[2]):
            return True
        if seen >= 2 and clean.get(part) == found:
            break
        seen += 1
        pending.append((part, found))
    clean.update(pending)
    return False
