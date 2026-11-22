"""Durable private-file primitives shared by the stores.

Each function opens with ``O_NOFOLLOW`` and applies no other path policy: callers check their own
path rules (symlink-free components, private modes) first and raise their own errors.
"""
from contextlib import contextmanager
import hashlib
import os
import threading


def fsync_directory(path):
    """Make entry changes in a directory durable; a symlink at ``path`` is refused."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class DirectorySyncs:
    """Directories whose entry changes still need an fsync, each synced once by ``flush``.

    ``sync`` is the function that syncs one directory (``fsync_directory`` or a caller's own, which may
    check its own path rules first); a directory is queued once per ``sync``. Deepest directories go first.
    """

    def __init__(self):
        self.pending = {}

    def add(self, path, sync):
        self.pending[(sync, path)] = None

    def flush(self):
        """Sync every queued directory; the queue is emptied first, so a failure leaves nothing half-queued
        (a rerun queues the directories again when it finds their entries already present)."""
        queued, self.pending = sorted(self.pending, key=lambda item: -len(item[1].parts)), {}
        for sync, path in queued:
            sync(path)


_scope = threading.local()


@contextmanager
def batched_directory_syncs():
    """Defer the directory fsyncs of ``fsync_directory_later`` calls on this thread to one ``flush``.

    The caller must call ``flush`` on the yielded object before anything that depends on the new entries
    being durable, such as a ledger or catalog commit that names the files; the outermost scope also
    flushes what is left when it exits normally, and a scope inside another joins it (and its queue). If
    the block raises nothing is flushed: no commit followed, and a rerun finds the entries present and
    syncs them again.
    """
    active = getattr(_scope, "syncs", None)
    if active is not None:
        yield active
        return
    active = _scope.syncs = DirectorySyncs()
    try:
        yield active
    finally:
        _scope.syncs = None
    active.flush()


def batching():
    """Whether directory syncs on this thread are being deferred to a flush."""
    return getattr(_scope, "syncs", None) is not None


def fsync_directory_later(path, sync=fsync_directory):
    """``sync(path)`` now, or once at the flush of the active ``batched_directory_syncs`` scope."""
    active = getattr(_scope, "syncs", None)
    if active is None:
        sync(path)
    else:
        active.add(path, sync)


def fsync_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def reserve(path):
    """Create an empty 0600 file; an existing entry, including a symlink, is refused."""
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600))


def write_new_private(path, data):
    """Write ``data`` to a new 0600 file and fsync it; an existing entry is refused.

    The directory entry is not synced here; callers sync the directory once its changes are done.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def sha256_file(path):
    digest = hashlib.sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
