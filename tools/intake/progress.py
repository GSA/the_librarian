"""Read-only, body-free progress view over the private intake stores.

The view reads the intake ledger, catalog, review events and context index with
their existing read-only paths. It never stages, runs, replays or repairs
anything, and it reports each unavailable or corrupt source instead of failing.
``progress_render`` turns the report into text or HTML and ``progress_serve``
serves it over loopback HTTP.
"""
from bisect import insort
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import os
import sqlite3
import stat
import time

from tools import policy
from tools.file_identity import file_identity
from . import store
from .contracts import IntakeError, decode_request

FUNNEL = ("staged", "received", "extracting", "extracted", "pending-review", "reviewed",
          "qualified", "selected", "rejected", "deferred", "exception", "skipped-by-format",
          "needs-reader", "blocked", "dead-letter", "published")
STATUSES = ("staged", "running", "retryable", "blocked", "dead-letter", "succeeded",
            "return-pending")
KINDS = ("receipt", "extraction", "review", "publication", "return")
DONE = {"catalog-submit": "received", "docling-prepare": "extracting",
        "docling-verify": "extracting", "docling-register": "extracted",
        "review-append": "reviewed", "standing-review": "qualified",
        "standing-select": "selected", "index-build": "published",
        "standing-publish": "published"}
ACTIVE = {"receipt": "staged", "extraction": "extracting", "review": "pending-review"}
STANDING_OUTCOMES = ("pending-review", "rejected", "deferred", "exception")
FORMAT_CODES = {"unsupported-type", "type-mismatch", "unsupported-format", "skipped-by-format"}
READER_PREFIX = "needs-reader"


def failure(error):
    """Report an error class and a short message; never document text."""
    code = getattr(error, "code", None)
    if isinstance(error, IntakeError) and isinstance(code, str):
        return {"available": False, "error": code}
    message = str(error).splitlines()[0][:200] if str(error) else ""
    return {"available": False, "error": type(error).__name__, "detail": message}


# Ledger -----------------------------------------------------------------

PAGE = 5000  # requests per short read transaction
WINDOWS = (60, 300, 900)  # seconds for the "is it moving" attempt rates
CODED = {"blocked", "dead-letter", "retryable"}  # statuses whose last attempt code is reported
ATTENTION = CODED

# Verification state reused across renders in this process. ``_memo`` holds SHA-256 digests of
# request and artifact bytes already proven (store.PageMemo); ``_cached`` holds the one finished
# ledger summary, valid only for the exact file identity it was computed from (see "Intake ledger" in docs/design/current-state.md).
_memo = store.PageMemo()
_cached = {}


def forget():
    """Drop everything kept between renders (tests and long-lived callers)."""
    _memo.clear()
    _cached.clear()


class Best:
    """The ``limit`` smallest keys offered, each with a payload built only when it is kept."""

    def __init__(self, limit):
        self.limit, self.entries = limit, []

    def offer(self, key, make):
        if len(self.entries) < self.limit or key < self.entries[-1][0]:
            insort(self.entries, (key, make()), key=lambda entry: entry[0])
            del self.entries[self.limit:]


def rates(c, now):
    """Attempts that finished within each window, by outcome (the run is moving, or it is not)."""
    rows = c.execute("SELECT outcome, " + ", ".join("coalesce(sum(ended>=?),0)" for _ in WINDOWS)
                     + " FROM attempts WHERE ended>=? AND outcome IS NOT NULL GROUP BY outcome",
                     [now - w for w in WINDOWS] + [now - max(WINDOWS)]).fetchall()
    return {"windows_seconds": list(WINDOWS), "finished": {
        outcome: dict(zip(map(str, WINDOWS), counts)) for outcome, *counts in sorted(rows, key=lambda row: row[0])}}


def header_identity(db):
    """``file_identity`` for a SQLite build without ``sqlite_dbpage``: the same stat fields and the
    change counter from the file's own header. Opens a descriptor on the database, so call it only
    while this process holds no SQLite lock on it (closing any descriptor drops fcntl locks)."""
    if os.path.lexists(f"{db}-journal") or os.path.lexists(f"{db}-wal"):
        return None
    with open(db, "rb") as stream:
        header = stream.read(100)
    info = os.stat(db, follow_symlinks=False)
    if len(header) < 100 or header[18:20] != b"\x01\x01" or not stat.S_ISREG(info.st_mode):
        return None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, header[24:28].hex()]


def open_ledger(root, timeout):
    root, home, db = store.locations(root)
    store.private(home, True)
    store.private(db)
    try:
        connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=timeout,
                                     isolation_level=None)
    except sqlite3.Error as error:
        raise IntakeError("ledger_unavailable") from error
    connection.row_factory = sqlite3.Row
    return root, db, connection


def stage_signature(layout, statuses):
    """Funnel state, current stage position and its status for one request shape and progress."""
    steps = [({"kind": kind, "operation": operation}, {"status": status}, {"code": code})
             for (_, kind, operation), (status, code) in zip(layout, statuses)]
    funnel = classify(steps)
    current = next((n for n, (status, _) in enumerate(statuses)
                    if status not in {"succeeded", "return-pending"}), len(statuses) - 1)
    return funnel, statuses[current][0], statuses[current][1]


def read_ledger(root, timeout=2.0, limit=50, runs=None):
    """Verify the whole ledger a page at a time and reduce it to counts and the few items shown.

    Every page is read in its own short read transaction and verified after it ends (so, as
    described under "Intake ledger" in docs/design/current-state.md, no read lock is held across digest or shape work). Nothing but counters and
    the ``limit`` newest items per list is kept, so memory follows the page, not the ledger.
    """
    started = time.perf_counter()
    root, db, c = open_ledger(root, timeout)
    with closing(c):
        try:
            c.execute("BEGIN")
            before = file_identity(db, c)
            if before is None:
                c.execute("COMMIT")  # no SQLite lock is held while the header is read directly
                before = header_identity(db)
                c.execute("BEGIN")
            key = (str(root), before, limit, tuple(sorted((runs or {}).items())),
                   datetime.now(timezone.utc).date().isoformat())
            if before is not None and _cached.get("key") == key:
                summary = _cached["summary"]
                moving = rates(c, time.time())
                c.execute("COMMIT")
                return {**summary, "rates": moving, "cached": True,
                        "seconds": round(time.perf_counter() - started, 3)}
            store.check_identity(c)
            c.execute("COMMIT")
        except sqlite3.OperationalError as error:
            raise IntakeError("ledger_unavailable") from error
        except sqlite3.DatabaseError as error:
            raise IntakeError("corrupt_store") from error
        counts, per_run = Counter(), {name: Counter() for name in runs or {}}
        latest = {name: None for name in per_run}
        newest = None
        recent, groups = Best(limit), {}
        info, after = {}, None
        while True:
            try:
                c.execute("BEGIN")
                rows = store.snapshot(c, after, PAGE)
                c.execute("COMMIT")
            except sqlite3.OperationalError as error:
                raise IntakeError("ledger_unavailable") from error
            except sqlite3.DatabaseError as error:
                raise IntakeError("corrupt_store") from error
            requests, stages_of, attempts_of, _ = rows
            if not requests:
                break
            layouts = store.verify_page(rows, root, _memo)
            for row, layout in zip(requests, layouts):
                correlation_id = row["correlation_id"]
                statuses, updated = [], None
                for saved, (stage_id, _, _) in zip(stages_of[correlation_id], layout):
                    history = attempts_of.get((correlation_id, stage_id), [])
                    for attempt in history:
                        for field in ("started", "ended"):
                            if attempt[field] is not None and (updated is None or attempt[field] > updated):
                                updated = attempt[field]
                    code = None
                    if saved["status"] in CODED:
                        current = [a for a in history if a["cycle"] == saved["cycle"]]
                        code = current[-1]["code"] if current else None
                    statuses.append((saved["status"], code))
                signature = (layout, tuple(statuses))
                if signature not in info:
                    info[signature] = stage_signature(layout, signature[1])
                _, state, code = info[signature]
                funnel = info[signature][0]
                counts[signature] += 1
                if updated is not None and (newest is None or updated > newest):
                    newest = updated
                for name, prefix in (runs or {}).items():
                    if correlation_id.startswith(prefix):
                        per_run[name][signature] += 1
                        if updated is not None and (latest[name] is None or updated > latest[name]):
                            latest[name] = updated
                order = (-(updated or 0), correlation_id)

                def build(row=row, correlation_id=correlation_id, stages_of=stages_of, attempts_of=attempts_of):
                    return candidate(row, stages_of[correlation_id], attempts_of)
                recent.offer(order, build)
                if state in ATTENTION:
                    group = groups.setdefault((state, funnel, code), [0, Best(limit)])
                    group[0] += 1
                    group[1].offer(order, build)
            after = requests[-1]["correlation_id"]
        try:
            c.execute("BEGIN")
            after_identity = file_identity(db, c)
            moving = rates(c, time.time())
            c.execute("COMMIT")
            if after_identity is None and before is not None:
                after_identity = header_identity(db)
        except sqlite3.DatabaseError as error:
            raise IntakeError("ledger_unavailable") from error
    summary = {
        "overall": aggregate(counts, newest),
        "runs": {name: aggregate(per_run[name], latest[name]) for name in per_run},
        "recent": [public(make_item(c_)) for _, c_ in recent.entries],
        "attention_groups": [
            {"state": state, "funnel": funnel, "code": code, "count": count}
            for (state, funnel, code), (count, _) in sorted(
                groups.items(), key=lambda g: (-g[1][0], tuple(str(x) for x in g[0])))],
        "attention_total": sum(count for count, _ in groups.values()),
    }
    summary["attention"] = [public(make_item(c_)) for c_ in spread(groups, limit)]
    # Stored only when the file was exactly the same before the first and after the last page: no
    # commit landed between pages, so the summary describes one verified file state.
    if before is not None and after_identity == before:
        _cached.clear()
        _cached.update(key=key, summary=summary)
    return {**summary, "rates": moving, "cached": False,
            "seconds": round(time.perf_counter() - started, 3)}


def candidate(row, stages, attempts_of):
    """The rows one displayed request needs, copied so the page they came from can be freed."""
    saved = {s["ordinal"]: {k: s[k] for k in ("correlation_id", "stage_id", "ordinal", "status", "cycle")}
             for s in stages}
    history = {(s["correlation_id"], s["stage_id"]): [
        {n: a[n] for n in ("correlation_id", "stage_id", "cycle", "ordinal", "started", "ended",
                           "outcome", "category", "code", "eligible_at")}
        for a in attempts_of.get((s["correlation_id"], s["stage_id"]), [])] for s in stages}
    return row["correlation_id"], row["correction_of"], row["raw"], saved, history


def make_item(entry):
    correlation_id, correction_of, raw, saved, history = entry
    return item(correlation_id, correction_of, decode_request(raw), saved, history)


def spread(groups, limit):
    """Up to ``limit`` attention entries taken round-robin from each (state, funnel, code) group, so
    one repeated code cannot use the whole list; newest first."""
    ranked = [group[1].entries for _, group in sorted(groups.items(), key=lambda g: (-g[1][0], str(g[0])))]
    chosen = []
    for depth in range(limit):
        for entries in ranked:
            if depth < len(entries) and len(chosen) < limit:
                chosen.append(entries[depth])
    chosen.sort(key=lambda entry: entry[0])
    return [payload for _, payload in chosen]


def classify(steps):
    """Map one request's ordered (stage, saved, last attempt) steps to a funnel state."""
    reached, previous = "staged", None
    for stage, saved, last in steps:
        status = saved["status"]
        if previous is not None and previous["kind"] == "extraction":
            reached = "extracting" if stage["kind"] == "extraction" else "extracted"
        if status in {"succeeded", "return-pending"}:
            reached = DONE.get(stage["operation"], reached)
            previous = stage
            continue
        if status in {"blocked", "dead-letter"}:
            code = (last or {}).get("code") or ""
            for outcome in STANDING_OUTCOMES:
                if code.startswith("standing-" + outcome + "-"):
                    return outcome
            if code in FORMAT_CODES:
                return "skipped-by-format"
            if code.startswith(READER_PREFIX):
                return "needs-reader"
            return status
        if status in {"running", "retryable"} and stage["kind"] in ACTIVE:
            return ACTIVE[stage["kind"]]
        return reached
    if previous is not None and previous["kind"] == "extraction":
        return "extracted"
    return reached


def item(correlation, correction_of, request, saved, attempts):
    steps = []
    for ordinal, stage in enumerate(request["stages"]):
        row = saved[ordinal]
        history = [a for a in attempts.get((correlation, stage["id"]), []) if a["cycle"] == row["cycle"]]
        steps.append((stage, row, history[-1] if history else None))
    current = next((step for step in steps if step[1]["status"] not in {"succeeded", "return-pending"}),
                   steps[-1])
    stage, row, last = current
    times = [a[key] for history in (attempts.get((correlation, s["id"]), []) for s, _, _ in steps)
             for a in history for key in ("started", "ended") if a[key] is not None]
    if last is None:
        last = next((step[2] for step in reversed(steps) if step[2] is not None), None)
    return {
        "correlation_id": correlation, "correction_of": correction_of,
        "stage": stage["id"], "kind": stage["kind"], "operation": stage["operation"],
        "state": row["status"], "funnel": classify(steps),
        "stages": dict(Counter(step[1]["status"] for step in steps)),
        "kinds": [(s["kind"], r["status"]) for s, r, _ in steps],
        "updated": max(times) if times else None,
        "last_attempt": None if last is None else {
            "outcome": last["outcome"], "category": last["category"], "code": last["code"],
            "attempt": last["ordinal"], "started": last["started"], "ended": last["ended"],
            "eligible_at": last["eligible_at"]},
    }


def aggregate(counts, last_activity):
    """Counts for a ``Counter`` of (layout, ((status, code), ...)) request signatures."""
    funnel = dict.fromkeys(FUNNEL, 0)
    matrix = {kind: dict.fromkeys(STATUSES, 0) for kind in KINDS}
    exceptions = retrying = 0
    for (layout, statuses), number in counts.items():
        funnel[stage_signature(layout, statuses)[0]] += number
        for (_, kind, _), (status, _) in zip(layout, statuses):
            matrix[kind][status] += number
        exceptions += number * any(status in {"blocked", "dead-letter"} for status, _ in statuses)
        retrying += number * any(status == "retryable" for status, _ in statuses)
    return {"requests": sum(counts.values()), "funnel": funnel, "stages": matrix,
            "exceptions": exceptions, "retrying": retrying, "last_activity": last_activity}


def public(value):
    return {key: data for key, data in value.items() if key != "kinds"}


# Other sources ------------------------------------------------------------

_catalog_seen = {}  # canonical binding -> state of a full inspection this process has done today


def catalog_state(catalog, connection, home):
    """The catalog's validated state without writing anything (no receipt is ever written here).

    A read-validation receipt that covers exactly this file ("What a catalog write validates" in docs/design/current-state.md) vouches for a state
    that a writer or reader already validated in full today, and this process's own earlier full
    inspection of the identical binding does the same; otherwise the whole history is inspected.
    A receipt that cannot be read is ignored, never trusted.
    """
    return catalog.observed_state(connection, home, _catalog_seen)


def read_catalog(root):
    from tools.catalog import catalog_store as catalog, contracts
    try:
        home, db, _ = catalog.locations(root)
        catalog.require(db.exists(), "Catalog missing; initialize the instance")
        catalog.require(home.is_dir() and home.stat().st_mode & 0o777 == 0o700,
                        "Catalog home missing or not private")
        with closing(catalog.connect(db)) as connection:
            connection.execute("BEGIN")
            state = catalog_state(catalog, connection, home)
            catalog.current_view(connection)
            # One scan of the current records for all three breakdowns.
            kinds, coverage, extraction = Counter(), Counter(), Counter()
            for kind, cover, extract, number in connection.execute(
                    "SELECT kind, json_extract(payload,'$.data.coverage'), "
                    "json_extract(payload,'$.data.extraction'), count(*) FROM catalog_current "
                    "GROUP BY 1,2,3"):
                kinds[kind] += number
                if kind == "artifact":
                    coverage[cover] += number
                    extraction[extract] += number
    except (catalog.CatalogError, OSError, ValueError, sqlite3.Error) as error:
        return failure(error)
    return {"available": True, "revision": state["revision"], "records": state["records"],
            "receipts": kinds.get("receipt", 0), "sources": kinds.get("source", 0),
            "artifacts": kinds.get("artifact", 0),
            "coverage": {name: coverage.get(name, 0) for name in sorted(contracts.COVERAGES)},
            "extraction": {name: extraction.get(name, 0) for name in sorted(contracts.EXTRACTIONS)}}


def read_review_events(root):
    from tools.review_events import event_reader, event_store
    try:
        _, db, _ = event_store.locations(root)
        if not db.exists():
            return {"available": False, "error": "ReviewError", "detail": "Review-event store missing"}
        snapshot = event_reader.read_snapshot(root)
    except (event_store.contract.ReviewError, OSError, ValueError) as error:
        return failure(error)
    current = [event for event in snapshot.events if event["event_id"] not in snapshot.successors]
    return {"available": True, "revision": snapshot.state["revision"], "events": len(snapshot.events),
            "current": len(current), "outcomes": dict(sorted(Counter(e["outcome"] for e in current).items()))}


def read_index(root):
    from tools.context_index import context_index as index
    try:
        root = index.no_symlinks(root)
        manifest = policy.selection_path(root, None)
        config, checksum = index.load_manifest(manifest, root)
        selected = {"documents": len(config["documents"]),
                    "renditions": len(config.get("renditions", []))}
        db = index.validate_db_path(root, root / policy.validate_instance_roots(root)["context_index_db"])
        if db.is_file() and (db.stat().st_mode & 0o777 != 0o600
                             or db.parent.stat().st_mode & 0o777 != 0o700):
            return {"available": False, "error": "not_private", "fresh": False}
        with closing(index.readonly(db)) as connection:
            reasons = index.freshness(connection, root, manifest, db, config, checksum)
            counts = index.count_index(connection)
    except (index.IndexError_, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        return {**failure(error), "fresh": False}
    return {"available": True, "fresh": not reasons, "reasons": reasons[:10],
            "stale_reasons": len(reasons), "selected": selected, "indexed": counts}


# Snapshot ------------------------------------------------------------------

def snapshot(root, *, limit=50, runs=None, timeout=2.0):
    """One body-free progress report; each source fails independently."""
    try:
        ledger = read_ledger(root, timeout, limit, runs)
        view, ledger_state = ledger, {"available": True}
    except (IntakeError, OSError, KeyError) as error:
        view, ledger_state = None, failure(error)
    if view is None:
        empty = aggregate(Counter(), None)
        view = {"overall": empty, "runs": {name: aggregate(Counter(), None) for name in runs or {}},
                "recent": [], "attention": [], "attention_groups": [], "attention_total": 0,
                "rates": None, "cached": False, "seconds": None}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "body_free": True, "read_only": True,
        "ledger": ledger_state, "overall": view["overall"],
        "runs": {name: {"prefix": prefix, **view["runs"][name]}
                 for name, prefix in sorted((runs or {}).items())},
        "recent": view["recent"], "attention": view["attention"],
        "attention_groups": view["attention_groups"], "attention_total": view["attention_total"],
        "rates": view["rates"],
        "render": {"ledger_seconds": view["seconds"], "ledger_cached": view["cached"]},
        "catalog": read_catalog(root), "review_events": read_review_events(root),
        "index": read_index(root),
    }
