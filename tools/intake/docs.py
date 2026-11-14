"""Derived per-document state in the intake ledger (Kata e22r).

One ``docs`` row per cohort document answers "which files changed", "what is left" and "what did this
path produce" with indexed queries instead of joins through request JSON. The workflow requests stay
authoritative: every ledger-derived column is a function of requests, stages and attempts, a row is
refreshed in the same transaction as the stage update that changes it, and ``docsync.audit``
recomputes the whole table from the requests and reports any difference. The table is optional: a
ledger without it behaves exactly as before, and dropping it loses nothing.

This module reads and writes rows through a connection it is handed and imports neither the store
nor the runner, so both can call it inside their own transactions.
"""
import time

from .contracts import decode, decode_request

# Attempts that lost a race with another catalog writer (``runner.CONFLICT_CODES`` is the same set).
CONFLICT_CODES = ("catalog-stale-state", "catalog-busy", "sqlite-operational")
FAILED = ("blocked", "dead-letter")
STAT = ("dev", "ino", "size", "mtime_ns", "ctime_ns")
# Columns a recomputation from the requests reproduces. Stat identity and last_run are observations
# of the inbox and the clock, not of the ledger, so ``audit`` does not compare them.
DERIVED = ("format", "submission_id", "suffix", "sha256", "receipt_id", "original", "state",
           "conflicts", "rendition", "rendition_sha256", "extract_head", "register_head")
COLUMNS = ("system", "path", *DERIVED, *STAT, "last_run")


def present(c):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs'").fetchone() is not None


def _status(c, correlation):
    if correlation is None:
        return None
    row = c.execute("SELECT status FROM stages WHERE correlation_id=? AND ordinal=0", (correlation,)).fetchone()
    return None if row is None else row[0]


def state_of(upload, extract, register):
    """The document state implied by the statuses of its three requests (``None``: no such request)."""
    for status in (upload, extract, register):
        if status in FAILED:
            return status
    if upload != "succeeded":
        return "staged"
    if extract is None:
        return "received"
    return "extracted" if extract == "succeeded" and register == "succeeded" else "extracting"


def measure(c, submission_id, extract_head, register_head):
    """State, conflict count and rendition of one document from its request heads."""
    heads = ("upload-" + submission_id, extract_head, register_head)
    state = state_of(*(_status(c, head) for head in heads))
    marks = ",".join("?" * len(CONFLICT_CODES))
    conflicts = sum(c.execute("SELECT count(*) FROM attempts WHERE correlation_id=? AND code IN (%s)" % marks,
                              (head, *CONFLICT_CODES)).fetchone()[0] for head in heads if head is not None)
    rendition = rendition_sha256 = None
    if state == "extracted":
        raw = c.execute("SELECT evidence FROM stages WHERE correlation_id=? AND ordinal=0",
                        (register_head,)).fetchone()[0]
        evidence = decode(raw.encode()) if raw else {}
        rendition, rendition_sha256 = evidence.get("rendition"), evidence.get("rendition_sha256")
    return {"state": state, "conflicts": conflicts, "rendition": rendition,
            "rendition_sha256": rendition_sha256}


def _receipt(c, correlation):
    """(receipt ID, retained original path, original SHA-256) from a saved receipt request."""
    row = c.execute("SELECT raw FROM requests WHERE correlation_id=?", (correlation,)).fetchone()
    stage = next(s for s in decode_request(row[0])["stages"] if s["operation"] == "catalog-submit")
    records = stage["arguments"]["records"]
    receipt = next(r["id"] for r in records if r["kind"] == "receipt")
    original = next(r["data"] for r in records if r["kind"] == "artifact" and r["data"].get("role") == "original")
    return receipt, original["path"], original["sha256"]


def derive(c, submission_id, suffix, extract_head, register_head):
    """Every ledger-derived column of one document whose receipt request exists.

    ``extract_head`` and ``register_head`` are the newest requests of the document's extraction and
    registration correction chains (``None`` when that request does not exist yet).
    """
    receipt_id, original, sha256 = _receipt(c, "upload-" + submission_id)
    return {"submission_id": submission_id, "suffix": suffix, "receipt_id": receipt_id, "original": original,
            "sha256": sha256, "extract_head": extract_head, "register_head": register_head,
            **measure(c, submission_id, extract_head, register_head)}


def upsert(c, system, path, fmt, derived, stat=None, now=None):
    values = {**derived, "system": system, "path": path, "format": fmt,
              "last_run": time.time() if now is None else now, **dict(zip(STAT, stat or (None,) * len(STAT)))}
    c.execute("INSERT OR REPLACE INTO docs(%s) VALUES (%s)" % (",".join(COLUMNS), ",".join("?" * len(COLUMNS))),
              [values[name] for name in COLUMNS])


def refresh(c, correlation_id, now=None):
    """Recompute the rows that follow ``correlation_id``; called in the transaction that changed it."""
    if not present(c):
        return
    if correlation_id.startswith("upload-"):
        rows = c.execute("SELECT * FROM docs WHERE submission_id=?", (correlation_id[len("upload-"):],)).fetchall()
    else:
        rows = c.execute("SELECT * FROM docs WHERE extract_head=?1 OR register_head=?1", (correlation_id,)).fetchall()
    now = time.time() if now is None else now
    for row in rows:
        state = measure(c, row["submission_id"], row["extract_head"], row["register_head"])
        c.execute("UPDATE docs SET state=?, conflicts=?, rendition=?, rendition_sha256=?, last_run=? "
                  "WHERE system=? AND path=?", (state["state"], state["conflicts"], state["rendition"],
                                                state["rendition_sha256"], now, row["system"], row["path"]))


def repoint(c, parent, child):
    """A correction ``child`` of ``parent`` becomes the head the document's rows follow."""
    if not present(c):
        return
    for column in ("extract_head", "register_head"):
        c.execute(f"UPDATE docs SET {column}=? WHERE {column}=?", (child, parent))
    refresh(c, child)


def load(c, system):
    """Every row of one cohort keyed by inbox-relative path (empty when the table is absent)."""
    if not present(c):
        return {}
    return {row["path"]: dict(row) for row in c.execute("SELECT * FROM docs WHERE system=?", (system,))}


def counts(c):
    """``{state: documents}`` from the table; ``None`` when it is absent."""
    if not present(c):
        return None
    return {state: n for state, n in c.execute("SELECT state, count(*) FROM docs GROUP BY state ORDER BY state")}
