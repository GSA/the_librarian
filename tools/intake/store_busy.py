"""Bounded waits for SQLite stores that are briefly locked by another process.

In rollback-journal mode a writer's commit holds PENDING while it waits for readers to leave, and
meanwhile every new reader fails with "database is locked" once its own busy timeout ends. The
instance-identity probe (``tools/policy.py``) then reports "Invalid store identity", although the
store is intact and only busy. A long cohort run must wait that out, not die (Kata 8as8).
``retry`` repeats a call that failed only because a store was busy, with backoff, for a bounded
time, then raises ``StoreBusy`` naming the condition. Any other error is raised at once.
"""
import sqlite3
import time

from tools import policy
from .contracts import IntakeError, LedgerBusy

WAIT_SECONDS = 300.0
FIRST_PAUSE, LAST_PAUSE = 0.1, 5.0
BUSY_CODES = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


class StoreBusy(IntakeError):
    """A store stayed locked by another process past the bounded wait; retry later."""
    retryable = True

    def __init__(self):
        super().__init__("store_busy")


def busy(error):
    """True when ``error``, or an error it was raised from, is a SQLite busy or locked error."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (LedgerBusy, StoreBusy)):
            return True
        if isinstance(error, sqlite3.OperationalError):
            code = getattr(error, "sqlite_errorcode", None)
            if code is not None:
                if code & 0xFF in BUSY_CODES:
                    return True
            elif "database is locked" in str(error) or "database is busy" in str(error):
                return True
        error = error.__cause__ or error.__context__
    return False


def retry(call, *, seconds=None, clock=None, sleep=None):
    """``call()``, repeated with backoff while it fails only because a store is busy."""
    seconds = WAIT_SECONDS if seconds is None else seconds
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    deadline, pause = clock() + seconds, FIRST_PAUSE
    while True:
        try:
            return call()
        except Exception as error:
            if not busy(error):
                raise
            if clock() + pause > deadline:
                raise StoreBusy() from error
        sleep(pause)
        pause = min(pause * 2, LAST_PAUSE)


def instance_roots(root):
    """``policy.validate_instance_roots(root)``, waiting out a store that is only busy."""
    return retry(lambda: policy.validate_instance_roots(root))
