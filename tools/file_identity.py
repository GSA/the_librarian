"""File identity shared by the intake ledger hand-off (0014) and catalog receipts (0016)."""
import os


def file_identity(db):
    """File identity plus SQLite's header file change counter, which every commit
    increments in rollback-journal mode; ``None`` when a journal is present."""
    if os.path.lexists(f"{db}-journal") or os.path.lexists(f"{db}-wal"):
        return None
    fd = os.open(db, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        header = os.pread(fd, 100, 0)
    finally:
        os.close(fd)
    if len(header) < 100 or header[18:20] != b"\x01\x01":
        return None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, header[24:28].hex()]
