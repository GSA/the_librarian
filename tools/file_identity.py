"""File identity shared by the intake ledger hand-off (0014) and catalog receipts (0017)."""
import os
import stat as stat_module
import sqlite3


def file_identity(db, connection):
    """File identity plus SQLite's header file change counter, which every commit
    increments in rollback-journal mode; ``None`` when a journal is present.

    Never opens a descriptor on ``db``: on POSIX, closing any descriptor for a file drops every
    fcntl lock the process holds on it, including the SQLite lock ``connection`` holds, which
    would let a concurrent commit tear the reader's snapshot (Kata kkmv). The stat fields come
    from the path and the header comes from the connection's own view of page 1
    (``sqlite_dbpage``); a build without that table yields ``None``, so callers fall back to
    their byte-exact path.
    """
    if os.path.lexists(f"{db}-journal") or os.path.lexists(f"{db}-wal"):
        return None
    info = os.stat(db, follow_symlinks=False)
    if not stat_module.S_ISREG(info.st_mode):
        raise OSError(f"Not a regular file: {db}")
    try:
        # fetchall, not fetchone: an unfinished statement would keep a read lock open.
        rows = connection.execute("SELECT substr(data,1,100) FROM sqlite_dbpage WHERE pgno=1").fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return None
        raise
    header = bytes(rows[0][0]) if rows and rows[0][0] is not None else b""
    if len(header) < 100 or header[18:20] != b"\x01\x01":
        return None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, header[24:28].hex()]
