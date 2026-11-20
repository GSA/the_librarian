"""Text and HTML rendering of a body-free progress report (see ``progress.snapshot``)."""
from datetime import datetime, timezone
import base64
import hashlib
import html

from .progress import DOC_FUNNEL, FUNNEL, KINDS, STATUSES

EXCEPTIONAL = ("rejected", "deferred", "exception", "skipped-by-format", "needs-reader",
               "blocked", "dead-letter", "mismatch", "ambiguous")


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


def documents_source(documents):
    return "the docs table" if documents["source"] == "docs" else "request names (no docs table)"


def reasons(data):
    found = ", ".join(f"{k}={v}" for k, v in data.get("superseded_by", {}).items() if v)
    return f" ({found})" if found else ""


def render_text(report):
    lines = [f"Intake progress at {report['generated_at']} (read-only, body-free)"]
    for name in ("ledger", "catalog", "review_events", "index"):
        lines.append("  " + source_line(name.replace("_", " "), report[name]))
    documents = report["documents"]
    if documents.get("available"):
        counts = ", ".join(f"{k}={v}" for k, v in documents["funnel"].items() if v) or "none"
        lines.append(f"documents: {documents['documents']} (from {documents_source(documents)}), "
                     f"{documents['attention_total']} need attention; {counts}")
        for group in documents["attention_groups"]:
            lines.append(f"  group: {group['count']} x {group['state']} {group['funnel']} {group['code'] or '-'}")
        publications = report["publications"]
        counts = ", ".join(f"{k}={v}" for k, v in publications["funnel"].items() if v) or "none"
        lines.append(f"publications: {publications['requests']} requests, last published "
                     f"{when(publications['last_published'])}; {counts}")
    groups = [("overall", report["overall"])] + [("run " + k, v) for k, v in report["runs"].items()]
    for label, data in groups:
        counts = ", ".join(f"{k}={v}" for k, v in data["funnel"].items() if v) or "none"
        lines.append(f"{label}: {data['requests']} requests, {data['exceptions']} exceptions, "
                     f"{data['retrying']} retrying, {data['superseded']} superseded{reasons(data)}; {counts}")
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
        lines.append(f"  request group: {group['count']} x {group['state']} {group['funnel']} "
                     f"{group['code'] or '-'}")
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


def cards(funnel, names=FUNNEL):
    out = []
    for name in names:
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
    documents = report["documents"]
    if documents.get("available"):
        parts.append(f"<h2>Documents: {documents['documents']} (from {esc(documents_source(documents))}), "
                     f"{documents['attention_total']} need attention</h2>" + cards(documents["funnel"], DOC_FUNNEL))
        if documents["attention_groups"]:
            parts.append("<h2>Documents needing attention by cause</h2>" + table(
                ("count", "state", "funnel", "code"),
                [(g["count"], g["state"], g["funnel"], g["code"]) for g in documents["attention_groups"]]))
        publications = report["publications"]
        parts.append(f"<h2>Publications: {publications['requests']} requests, last published "
                     f"{time_tag(when(publications['last_published']))}</h2>" + table(
                         tuple(publications["funnel"]) or ("requests",),
                         [tuple(publications["funnel"].values()) or (0,)]))
    parts.append(f"<h2>Requests: {overall['requests']} requests, {overall['exceptions']} exceptions, "
                 f"{overall['retrying']} retrying ({overall['superseded']} superseded not counted"
                 f"{esc(reasons(overall))})</h2>" + cards(overall["funnel"]))
    for name, data in report["runs"].items():
        parts.append(f"<h2>Run {esc(name)} ({esc(data['prefix'])}*): {data['requests']} requests, "
                     f"{data['exceptions']} exceptions ({data['superseded']} superseded not counted)</h2>"
                     + cards(data["funnel"]))
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
        parts.append("<h2>Requests needing attention by cause</h2>" + table(
            ("count", "state", "funnel", "code"),
            [(g["count"], g["state"], g["funnel"], g["code"]) for g in report["attention_groups"]]))
    parts.append(f"<h2>Requests needing attention (showing {len(report['attention'])} of "
                 f"{report['attention_total']})</h2>"
                 + table(headers, item_rows(report["attention"])))
    parts.append("<h2>Recent items</h2>" + table(headers, item_rows(report["recent"])))
    parts.append(f"<script>{LOCAL_TIME_SCRIPT}</script></body></html>")
    return "".join(parts)
