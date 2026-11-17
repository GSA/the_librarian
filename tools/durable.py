"""Durable private-file primitives shared by the stores.

Each function opens with ``O_NOFOLLOW`` and applies no other path policy: callers check their own
path rules (symlink-free components, private modes) first and raise their own errors.
"""
import hashlib
import os


def fsync_directory(path):
    """Make entry changes in a directory durable; a symlink at ``path`` is refused."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
