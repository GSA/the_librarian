"""Read-only, body-free progress view over the private intake stores.

The view reads the intake ledger, catalog, review events and context index with
their existing read-only paths. It never stages, runs, replays or repairs
anything, and it reports each unavailable or corrupt source instead of failing.
The HTTP view binds loopback only unless non-loopback exposure is explicitly
approved by the operator.
"""
from bisect import insort
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import base64
import hashlib
import html
import http.server
import ipaddress
import json
import os
import socket
import sqlite3
import stat
import threading
import time

from tools import policy
from tools.file_identity import file_identity
from . import store
from .contracts import IntakeError, decode_request

FUNNEL = ("staged", "received", "extracting", "extracted", "pending-review", "reviewed",
          "qualified", "selected", "rejected", "deferred", "exception", "skipped-by-format",
          "needs-reader", "blocked", "dead-letter", "published")
EXCEPTIONAL = ("rejected", "deferred", "exception", "skipped-by-format", "needs-reader",
               "blocked", "dead-letter")
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


class BindRefused(IntakeError):
    def __init__(self):
        super().__init__("non_loopback_bind")


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
# ledger summary, valid only for the exact file identity it was computed from (decision 0014/0017).
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
        outcome: dict(zip(map(str, WINDOWS), counts)) for outcome, *counts in sorted(rows)}}


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

    Every page is read in its own short read transaction and verified after it ends (so, as in
    decision 0014, no read lock is held across digest or shape work). Nothing but counters and
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
        counts, overall, per_run = Counter(), Counter(), {name: Counter() for name in runs or {}}
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

                def build(row=row, layout=layout, correlation_id=correlation_id):
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

    A read-validation receipt that covers exactly this file (decision 0017) vouches for a state
    that a writer or reader already validated in full today, and this process's own earlier full
    inspection of the identical binding does the same; otherwise the whole history is inspected.
    A receipt that cannot be read is ignored, never trusted.
    """
    try:
        binding = catalog._binding(connection)
        marker = json.dumps([binding, catalog._today()], sort_keys=True)
        trusted = catalog._trusted_state(connection, home, binding)
    except (catalog.CatalogError, OSError, ValueError):
        binding = marker = trusted = None
    if trusted is not None:
        return trusted[0]
    if marker is not None and _catalog_seen.get("marker") == marker \
            and _catalog_seen["head"] == catalog._head_state(connection):
        return _catalog_seen["state"]
    state = catalog.inspect(connection, detail=False)[0]
    if marker is not None and binding["version"] == 2:
        _catalog_seen.clear()
        _catalog_seen.update(marker=marker, head=catalog._head_state(connection), state=state)
    return state


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


# Rendering -------------------------------------------------------------------

def when(value):
    if value is None:
        return "-"
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


LOCAL_TIME_SCRIPT = (
    "for(const t of document.querySelectorAll('time[datetime]')){"
    "const d=new Date(t.getAttribute('datetime'));"
    "if(!isNaN(d)){t.title=t.textContent;t.textContent=d.toLocaleString(undefined,"
    "{dateStyle:'medium',timeStyle:'medium'})}}")
SCRIPT_SRC = "'sha256-" + base64.b64encode(
    hashlib.sha256(LOCAL_TIME_SCRIPT.encode()).digest()).decode() + "'"


def time_tag(value):
    if value is None or value == "-":
        return "-"
    return f'<time datetime="{html.escape(str(value), quote=True)}">{html.escape(str(value))}</time>'


def source_line(name, value):
    if value.get("available"):
        return name + ": available"
    return f"{name} unavailable: {value.get('error')}" + (
        f" ({value['detail']})" if value.get("detail") else "")


def render_text(report):
    lines = [f"Intake progress at {report['generated_at']} (read-only, body-free)"]
    for name in ("ledger", "catalog", "review_events", "index"):
        lines.append("  " + source_line(name.replace("_", " "), report[name]))
    groups = [("overall", report["overall"])] + [("run " + k, v) for k, v in report["runs"].items()]
    for label, data in groups:
        counts = ", ".join(f"{k}={v}" for k, v in data["funnel"].items() if v) or "none"
        lines.append(f"{label}: {data['requests']} requests, {data['exceptions']} exceptions, "
                     f"{data['retrying']} retrying; {counts}")
    moving = report.get("rates")
    if moving:
        lines.append("finished attempts (last " + "/".join(f"{w // 60}" for w in moving["windows_seconds"])
                     + " min): " + (", ".join(
                         f"{outcome} " + "/".join(str(counts[str(w)]) for w in moving["windows_seconds"])
                         for outcome, counts in moving["finished"].items()) or "none"))
    index = report["index"]
    if index.get("available"):
        lines.append(f"index: {'fresh' if index['fresh'] else 'STALE'}; documents="
                     f"{index['indexed']['documents']} sections={index['indexed']['sections']}; "
                     f"selected documents={index['selected']['documents']} "
                     f"renditions={index['selected']['renditions']}")
    for group in report["attention_groups"]:
        lines.append(f"  group: {group['count']} x {group['state']} {group['funnel']} {group['code'] or '-'}")
    for value in report["attention"]:
        attempt = value["last_attempt"] or {}
        lines.append(f"  ! {value['correlation_id']} {value['stage']} {value['state']} "
                     f"{value['funnel']} {attempt.get('category') or '-'} {attempt.get('code') or '-'}")
    return "\n".join(lines) + "\n"


STYLE = """
body{font:15px/1.4 system-ui,sans-serif;margin:1.5rem;color:#1b1b1b;background:#fafafa}
h1{font-size:1.4rem;margin:0}h2{font-size:1.1rem;margin:1.4rem 0 .4rem}
.muted{color:#555}.cards{display:flex;flex-wrap:wrap;gap:.5rem}
.card{background:#fff;border:1px solid #ccc;border-radius:6px;padding:.5rem .8rem;min-width:7.5rem}
.card b{display:block;font-size:1.5rem}.bad{border-color:#b50909;background:#fdecec}
.ok{border-color:#2e8540;background:#eef8f0}.warn{border-color:#c05600;background:#fff4e5}
table{border-collapse:collapse;background:#fff;font-size:.9rem}
td,th{border:1px solid #ccc;padding:.25rem .5rem;text-align:left}th{background:#eee}
"""


class Raw:
    def __init__(self, html_text):
        self.html = html_text


def esc(value):
    return html.escape("-" if value is None else str(value), quote=True)


def cards(funnel):
    out = []
    for name in FUNNEL:
        tone = "bad" if name in EXCEPTIONAL and funnel[name] else "ok" if name == "published" else ""
        out.append(f'<div class="card {tone}"><b>{funnel[name]}</b>{esc(name)}</div>')
    return '<div class="cards">' + "".join(out) + "</div>"


def table(headers, rows):
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(
        f"<td>{c.html}</td>" if isinstance(c, Raw) else f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><tr>{head}</tr>{body or '<tr><td colspan=99>none</td></tr>'}</table>"


def item_rows(values):
    return [(v["correlation_id"], v["stage"], v["operation"], v["state"], v["funnel"],
             (v["last_attempt"] or {}).get("outcome"), (v["last_attempt"] or {}).get("category"),
             (v["last_attempt"] or {}).get("code"), Raw(time_tag(when(v["updated"])))) for v in values]


def render_html(report, *, refresh=5):
    overall = report["overall"]
    parts = [f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
             f'<meta http-equiv="refresh" content="{int(refresh)}">'
             f"<title>Intake progress</title><style>{STYLE}</style></head><body>",
             "<h1>Intake progress</h1>",
             f'<p class="muted">Read-only and body-free. Updated {time_tag(report["generated_at"])}; '
             f"refreshes every {int(refresh)} s. Ledger read {report['render']['ledger_seconds']} s"
             f"{' (unchanged since last read)' if report['render']['ledger_cached'] else ''}. Last ledger activity {time_tag(when(overall['last_activity']))}.</p>"]
    sources = []
    for name in ("ledger", "catalog", "review_events", "index"):
        value = report[name]
        tone = "ok" if value.get("available") else "bad"
        sources.append(f'<div class="card {tone}">{esc(source_line(name.replace("_", " "), value))}</div>')
    parts.append('<h2>Sources</h2><div class="cards">' + "".join(sources) + "</div>")
    index = report["index"]
    if index.get("available"):
        tone = "ok" if index["fresh"] else "warn"
        parts.append(f'<h2>Index</h2><div class="cards"><div class="card {tone}"><b>'
                     f'{"fresh" if index["fresh"] else "stale"}</b>freshness</div>'
                     f'<div class="card"><b>{index["indexed"]["documents"]}</b>indexed documents</div>'
                     f'<div class="card"><b>{index["indexed"]["sections"]}</b>indexed sections</div>'
                     f'<div class="card"><b>{index["selected"]["documents"]}</b>selected documents</div>'
                     f'<div class="card"><b>{index["selected"]["renditions"]}</b>selected renditions</div></div>')
        if index["reasons"]:
            parts.append("<p>Stale: " + esc("; ".join(index["reasons"])) + "</p>")
    parts.append(f"<h2>Overall: {overall['requests']} requests, {overall['exceptions']} exceptions, "
                 f"{overall['retrying']} retrying</h2>" + cards(overall["funnel"]))
    for name, data in report["runs"].items():
        parts.append(f"<h2>Run {esc(name)} ({esc(data['prefix'])}*): {data['requests']} requests, "
                     f"{data['exceptions']} exceptions</h2>" + cards(data["funnel"]))
    parts.append("<h2>Stages by kind and state</h2>" + table(
        ("kind", *STATUSES), [(k, *overall["stages"][k].values()) for k in KINDS]))
    catalog = report["catalog"]
    if catalog.get("available"):
        parts.append("<h2>Catalog</h2>" + table(
            ("receipts", "artifacts", *("coverage " + k for k in catalog["coverage"]),
             *("extraction " + k for k in catalog["extraction"])),
            [(catalog["receipts"], catalog["artifacts"], *catalog["coverage"].values(),
              *catalog["extraction"].values())]))
    reviews = report["review_events"]
    if reviews.get("available"):
        parts.append("<h2>Review events</h2>" + table(
            ("current events", *reviews["outcomes"]), [(reviews["current"], *reviews["outcomes"].values())]))
    headers = ("correlation", "stage", "operation", "state", "funnel", "last outcome",
               "category", "code", "updated")
    moving = report.get("rates")
    if moving:
        windows = moving["windows_seconds"]
        parts.append("<h2>Attempts finished (is it moving?)</h2>" + table(
            ("outcome", *(f"last {w // 60} min" for w in windows)),
            [(outcome, *(counts[str(w)] for w in windows)) for outcome, counts in moving["finished"].items()]))
    if report["attention_groups"]:
        parts.append("<h2>Needs attention by cause</h2>" + table(
            ("count", "state", "funnel", "code"),
            [(g["count"], g["state"], g["funnel"], g["code"]) for g in report["attention_groups"]]))
    parts.append(f"<h2>Needs attention (showing {len(report['attention'])} of {report['attention_total']})</h2>"
                 + table(headers, item_rows(report["attention"])))
    parts.append("<h2>Recent items</h2>" + table(headers, item_rows(report["recent"])))
    parts.append(f"<script>{LOCAL_TIME_SCRIPT}</script></body></html>")
    return "".join(parts)


# Loopback server ---------------------------------------------------------------

def check_bind(host, allow_non_loopback=False):
    """Return the address to bind; refuse anything not loopback without approval."""
    if host == "localhost":
        return "127.0.0.1"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        return str(ipaddress.ip_address(host))
    if allow_non_loopback and host:
        return host
    raise BindRefused()


class Latest:
    """At most one render at a time, shared by every request that arrives meanwhile.

    A request that finds a render running waits for it and uses its result, and a result younger
    than ``reuse`` seconds is served again, so any number of open tabs cost one render per
    ``reuse`` seconds plus one render's time at most, never one render per tab.
    """

    def __init__(self, render, reuse=1.0, clock=time.monotonic):
        self.render, self.reuse, self.clock = render, reuse, clock
        self.lock, self.result, self.finished, self.running = threading.Condition(), None, None, False

    def get(self):
        with self.lock:
            while True:
                if self.result is not None and not self.running \
                        and self.clock() - self.finished <= self.reuse:
                    return self.result
                if not self.running:
                    break
                joined = self.finished
                self.lock.wait()
                if self.result is not None and self.finished != joined:
                    return self.result
            self.running = True
        try:
            result = self.render()
        except BaseException:
            with self.lock:
                self.running = False
                self.lock.notify_all()
            raise
        with self.lock:
            self.result, self.finished, self.running = result, self.clock(), False
            self.lock.notify_all()
        return result


def make_server(root, *, host="127.0.0.1", port=8765, refresh=5, limit=50, runs=None,
                allow_non_loopback=False, reuse=1.0):
    address = check_bind(host, allow_non_loopback)
    latest = Latest(lambda: snapshot(root, limit=limit, runs=runs), reuse)

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "librarian-progress"
        sys_version = ""

        def send(self, status, kind, body):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy",
                             f"default-src 'none'; style-src 'unsafe-inline'; script-src {SCRIPT_SRC}; "
                             "frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def trusted_host(self):
            if allow_non_loopback:
                return True
            name = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return name in {"127.0.0.1", "localhost", "::1"}

        def do_GET(self):
            if not self.trusted_host():
                return self.send(421, "text/plain; charset=utf-8", "Misdirected request\n")
            path = self.path.split("?", 1)[0]
            if path not in {"/", "/progress.json"}:
                return self.send(404, "text/plain; charset=utf-8", "Not found\n")
            report = latest.get()
            if path == "/":
                return self.send(200, "text/html; charset=utf-8", render_html(report, refresh=refresh))
            return self.send(200, "application/json", json.dumps(report, sort_keys=True))

        do_HEAD = do_GET

        def refuse(self):
            self.send(405, "text/plain; charset=utf-8", "Read-only view\n")

        do_POST = do_PUT = do_PATCH = do_DELETE = refuse

        def log_message(self, *args):
            return

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET6 if ":" in address else socket.AF_INET

    return Server((address, port), Handler)


def serve(root, **options):
    server = make_server(root, **options)
    host, port = server.server_address[:2]
    shown = f"[{host}]" if ":" in host else host
    print(json.dumps({"serving": f"http://{shown}:{port}/", "read_only": True}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"stopped": True}
