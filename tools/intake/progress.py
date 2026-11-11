"""Read-only, body-free progress view over the private intake stores.

The view reads the intake ledger, catalog, review events and context index with
their existing read-only paths. It never stages, runs, replays or repairs
anything, and it reports each unavailable or corrupt source instead of failing.
The HTTP view binds loopback only unless non-loopback exposure is explicitly
approved by the operator.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import base64
import hashlib
import html
import http.server
import ipaddress
import json
import socket
import sqlite3
import threading

from tools import policy
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

def read_ledger(root, timeout):
    root, home, db = store.locations(root)
    store.private(home, True)
    store.private(db)
    try:
        connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=timeout,
                                     isolation_level=None)
    except sqlite3.Error as error:
        raise IntakeError("ledger_unavailable") from error
    with closing(connection) as c:
        c.row_factory = sqlite3.Row
        try:
            # The read transaction only copies rows: SQLite's own checks and one pass per
            # table. Digest and shape verification runs after it ends, so a large ledger
            # cannot hold a read lock long enough to starve a writer's commit.
            c.execute("BEGIN")
            store.check_identity(c)
            rows = store.snapshot(c)
            c.execute("COMMIT")
        except sqlite3.OperationalError as error:
            raise IntakeError("ledger_unavailable") from error
        except sqlite3.DatabaseError as error:
            raise IntakeError("corrupt_store") from error
        store.verify(rows, root)
        requests = [(row["correlation_id"], row["correction_of"], decode_request(row["raw"]))
                    for row in rows[0]]
        stages = {}
        for correlation_id, saved in rows[1].items():
            for row in saved:
                stages.setdefault(correlation_id, {})[row["ordinal"]] = {
                    key: row[key] for key in ("correlation_id", "stage_id", "ordinal", "status", "cycle")}
        attempts = {key: [{name: row[name] for name in (
            "correlation_id", "stage_id", "cycle", "ordinal", "started", "ended", "outcome",
            "category", "code", "eligible_at")} for row in saved] for key, saved in rows[2].items()}
    return requests, stages, attempts


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


def aggregate(items):
    funnel = dict.fromkeys(FUNNEL, 0)
    matrix = {kind: dict.fromkeys(STATUSES, 0) for kind in KINDS}
    for value in items:
        funnel[value["funnel"]] += 1
        for kind, status in value["kinds"]:
            matrix[kind][status] += 1
    exceptions = sum(any(status in {"blocked", "dead-letter"} for _, status in value["kinds"])
                     for value in items)
    retrying = sum(any(status == "retryable" for _, status in value["kinds"]) for value in items)
    updated = [value["updated"] for value in items if value["updated"] is not None]
    return {"requests": len(items), "funnel": funnel, "stages": matrix,
            "exceptions": exceptions, "retrying": retrying,
            "last_activity": max(updated) if updated else None}


def public(value):
    return {key: data for key, data in value.items() if key != "kinds"}


# Other sources ------------------------------------------------------------

def read_catalog(root):
    from tools.catalog import catalog_store as catalog, contracts
    try:
        home, db, _ = catalog.locations(root)
        catalog.require(db.exists(), "Catalog missing; initialize the instance")
        catalog.require(home.is_dir() and home.stat().st_mode & 0o777 == 0o700,
                        "Catalog home missing or not private")
        with closing(catalog.connect(db)) as connection:
            connection.execute("BEGIN")
            # Full inspection, not validated_state(): its cache would write files.
            state = catalog.inspect(connection, detail=False)[0]
            catalog.current_view(connection)
            kinds = dict(connection.execute("SELECT kind,count(*) FROM catalog_current GROUP BY kind"))
            coverage = dict(connection.execute(
                "SELECT json_extract(payload,'$.data.coverage'),count(*) FROM catalog_current "
                "WHERE kind='artifact' GROUP BY 1"))
            extraction = dict(connection.execute(
                "SELECT json_extract(payload,'$.data.extraction'),count(*) FROM catalog_current "
                "WHERE kind='artifact' GROUP BY 1"))
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
        requests, saved, attempts = read_ledger(root, timeout)
        items = [item(c, prior, request, saved[c], attempts) for c, prior, request in requests]
        ledger = {"available": True}
    except (IntakeError, OSError, KeyError) as error:
        items, ledger = [], failure(error)
    items.sort(key=lambda v: (-(v["updated"] or 0), v["correlation_id"]))
    attention = [v for v in items if v["state"] in {"blocked", "dead-letter", "retryable"}]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "body_free": True, "read_only": True,
        "ledger": ledger, "overall": aggregate(items),
        "runs": {name: {"prefix": prefix,
                        **aggregate([v for v in items if v["correlation_id"].startswith(prefix)])}
                 for name, prefix in sorted((runs or {}).items())},
        "recent": [public(v) for v in items[:limit]],
        "attention": [public(v) for v in attention[:limit]],
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
    index = report["index"]
    if index.get("available"):
        lines.append(f"index: {'fresh' if index['fresh'] else 'STALE'}; documents="
                     f"{index['indexed']['documents']} sections={index['indexed']['sections']}; "
                     f"selected documents={index['selected']['documents']} "
                     f"renditions={index['selected']['renditions']}")
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
             f"refreshes every {int(refresh)} s. Last ledger activity {time_tag(when(overall['last_activity']))}.</p>"]
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
    parts.append("<h2>Needs attention</h2>" + table(headers, item_rows(report["attention"])))
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


def make_server(root, *, host="127.0.0.1", port=8765, refresh=5, limit=50, runs=None,
                allow_non_loopback=False):
    address = check_bind(host, allow_non_loopback)
    reading = threading.Lock()

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
            with reading:
                report = snapshot(root, limit=limit, runs=runs)
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
