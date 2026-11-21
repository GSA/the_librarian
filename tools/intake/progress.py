"""Read-only, body-free progress view over the private intake stores.

The view reads the intake ledger, catalog, review events and context index with
their existing read-only paths. It never stages, runs, replays or repairs
anything, and it reports each unavailable or corrupt source instead of failing.
``progress_render`` turns the report into text or HTML and ``progress_serve``
serves it over loopback HTTP.
"""
from bisect import insort
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import math
import os
import re
import sqlite3
import stat
import sys
import time

from tools import policy
from tools.file_identity import file_identity
from . import docs, store
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
# One state per document (receipt), the furthest its current requests reached (see ``fold``).
DOC_FUNNEL = ("staged", "received", "extracting", "extracted", "pending-review", "qualified",
              "selected", "rejected", "deferred", "exception", "skipped-by-format",
              "needs-reader", "blocked", "dead-letter", "mismatch", "ambiguous")
# Why a counted request is left out: corrected by another request, a standing review under an older
# policy than another review of the same rendition, a review of a rendition that is no longer the
# document's current one, or any request of a submission whose receipt was corrected.
SUPERSEDED = ("correction", "policy", "rendition", "submission")
# ``standing-<submission>-<rendition sha256[:12]>-<policy sha256[:8]>`` and its ``-c<n>`` corrections.
STANDING_NAME = re.compile(r"standing-(.+)-([0-9a-f]{12})-([0-9a-f]{8})(?:-c[0-9]+)?")
ROLE_OPERATIONS = {"upload-": ("receipt", ("catalog-submit",)),
                   "extract-": ("extract", ("extract-prepare",)),
                   "register-": ("register", ("extract-register",))}
SLOTS = {"receipt": 0, "extract": 1, "register": 2, "review": 3}


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


@contextmanager
def ledger_errors():
    """Map SQLite failures of a ledger read to the reported intake error codes."""
    try:
        yield
    except sqlite3.OperationalError as error:
        raise IntakeError("ledger_unavailable") from error
    except sqlite3.DatabaseError as error:
        raise IntakeError("corrupt_store") from error


def first_identity(c, db):
    """The ledger file identity inside the open read transaction (header fallback outside a lock)."""
    before = file_identity(db, c)
    if before is None:
        c.execute("COMMIT")  # no SQLite lock is held while the header is read directly
        before = header_identity(db)
        c.execute("BEGIN")
    return before


def last_identity(c, db, before):
    """The file identity and attempt rates after the last page."""
    try:
        c.execute("BEGIN")
        after = file_identity(db, c)
        moving = rates(c, time.time())
        c.execute("COMMIT")
        if after is None and before is not None:
            after = header_identity(db)
    except sqlite3.DatabaseError as error:
        raise IntakeError("ledger_unavailable") from error
    return after, moving


def newer(current, value):
    return value if current is None or (value is not None and value > current) else current


def request_state(correlation_id, stages, layout, attempts_of):
    """Each stage's (status, last attempt code when coded), and the oldest attempt start and newest
    attempt time of one request."""
    statuses, first, updated = [], None, None
    for saved, (stage_id, _, _) in zip(stages, layout):
        history = attempts_of.get((correlation_id, stage_id), [])
        for attempt in history:
            updated = newer(newer(updated, attempt["started"]), attempt["ended"])
            first = attempt["started"] if first is None else min(first, attempt["started"])
        code = None
        if saved["status"] in CODED:
            current = [a for a in history if a["cycle"] == saved["cycle"]]
            code = current[-1]["code"] if current else None
        statuses.append((saved["status"], code))
    return tuple(statuses), first, updated


# Documents -----------------------------------------------------------------

class LedgerIndex:
    """What the page scan needs to know about the whole ledger before it starts, from names only.

    ``uploads`` maps each receipt request's submission ID to True when it is a chain head (a
    document). ``stale`` maps standing review heads that another review supersedes to the reason.
    ``policy_rank`` orders standing policies by when they were first attempted (never attempted:
    newest). ``docs`` is None without the derived ``docs`` table; otherwise it maps each row's
    submission ID to the name tails of its extraction and registration heads (the part after
    ``extract-<submission>`` / ``register-<submission>``) and its current rendition's 12-digit
    prefix, and ``special`` holds the rows whose chain is not trusted (``mismatch``, ``ambiguous``).
    Strings are interned so the per-document maps share one copy of each submission ID.
    """

    def __init__(self):
        self.uploads, self.stale, self.first_use, self.ranks = {}, {}, {}, {}
        self.docs, self.special, self.foreign = None, {}, {}

    def policy_rank(self, p8):
        rank = self.ranks.get(p8)
        if rank is None:
            rank = self.ranks[p8] = (self.first_use.get(p8, math.inf), sys.intern(p8))
        return rank

    def submission(self, rest):
        """The longest known submission ID that ``rest`` starts with, ending at a ``-`` or at its end."""
        at = len(rest)
        while at > 0:
            if rest[:at] in self.uploads:
                return sys.intern(rest[:at])
            at = rest.rfind("-", 0, at)
        return None

    def is_docs_head(self, role, sid, correlation_id):
        tail = self.docs[sid][0 if role == "extract" else 1]
        return tail is not None and (correlation_id == f"{role}-{sid}{tail}"
                                     or self.foreign.get(correlation_id) == (role, sid))

    def supersede(self, sid, r12, members):
        """Mark the reviews of one rendition that a newer policy or a newer rendition supersedes."""
        if self.docs is not None and sid in self.docs and self.docs[sid][2] != r12:
            self.stale.update((correlation_id, "rendition") for _, correlation_id in members)
            return
        top = max(self.policy_rank(p8) for p8, _ in members)
        self.stale.update((correlation_id, "policy") for p8, correlation_id in members
                          if self.policy_rank(p8) < top)


def prefixed(c, table, prefix, columns="correlation_id", tail=""):
    """Rows of ``table`` whose correlation ID starts with ``prefix`` (a primary-key range, no LIKE)."""
    return c.execute(f"SELECT {columns} FROM {table} WHERE correlation_id>=? AND correlation_id<?{tail}",
                     (prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)))


def tail_of(prefix, sid, head):
    if head is None:
        return None
    return sys.intern(head[len(prefix) + len(sid):]) if head.startswith(prefix + sid) else head


def ledger_index(c, corrected):
    """Read the names, first standing attempt times and ``docs`` rows the scan needs.

    Each query is its own short read transaction; nothing is decoded and no document text is read.
    Standing review names are read in key order, so the heads of one rendition arrive together.
    """
    index = LedgerIndex()
    with ledger_errors():
        c.execute("BEGIN")
        for correlation_id, started in prefixed(c, "attempts", "standing-", "correlation_id, min(started)",
                                                " GROUP BY correlation_id"):
            name = STANDING_NAME.fullmatch(correlation_id)
            if name is not None and started is not None:
                index.first_use[name[3]] = min(started, index.first_use.get(name[3], started))
        c.execute("COMMIT")
        c.execute("BEGIN")
        for (correlation_id,) in prefixed(c, "requests", "upload-"):
            index.uploads[sys.intern(correlation_id[len("upload-"):])] = correlation_id not in corrected
        if docs.present(c):
            index.docs = {}
            for sid, state, sha, extract_head, register_head in c.execute(
                    "SELECT submission_id, state, rendition_sha256, extract_head, register_head FROM docs"):
                sid = sys.intern(sid)
                index.docs[sid] = (tail_of("extract-", sid, extract_head), tail_of("register-", sid, register_head),
                                   sha[:12] if state == "extracted" and sha else None)
                # A correction may carry any name; the row names the head explicitly.
                for role, head in (("extract", extract_head), ("register", register_head)):
                    if head is not None and not head.startswith(f"{role}-{sid}"):
                        index.foreign[head] = (role, sid)
                if state in {"mismatch", "ambiguous"}:
                    index.special[sid] = state
        c.execute("COMMIT")
        c.execute("BEGIN")
        group, members = None, []
        for (correlation_id,) in prefixed(c, "requests", "standing-", tail=" ORDER BY correlation_id"):
            name = STANDING_NAME.fullmatch(correlation_id)
            if name is None or correlation_id in corrected:
                continue
            if (name[1], name[2]) != group:
                if members:
                    index.supersede(*group, members)
                group, members = (name[1], name[2]), []
            members.append((name[3], correlation_id))
        if members:
            index.supersede(*group, members)
        c.execute("COMMIT")
    return index


def role_of(correlation_id, layout, index):
    """``(role, submission ID)`` of one request: a document's receipt, extraction, registration or
    standing review, a publication, or ``(None, None)`` for anything else."""
    operations = tuple(operation for _, _, operation in layout)
    foreign = index.foreign.get(correlation_id)
    if foreign is not None and operations == ROLE_OPERATIONS[foreign[0] + "-"][1]:
        return foreign
    for prefix, (role, expected) in ROLE_OPERATIONS.items():
        if correlation_id.startswith(prefix) and operations == expected:
            rest = correlation_id[len(prefix):]
            return role, sys.intern(rest) if role == "receipt" else index.submission(rest)
    if operations == ("standing-review", "standing-select"):
        name = STANDING_NAME.fullmatch(correlation_id)
        if name is not None:
            return "review", sys.intern(name[1])
    if all(kind == "publication" for _, kind, _ in layout) and "standing-select" not in operations:
        return "publication", None
    return None, None


def offer_document(documents, index, role, sid, correlation_id, found, first):
    """Keep the request that decides one slot of a document: with a ``docs`` row its own heads,
    otherwise the newest (standing reviews: newest policy first)."""
    entry = documents[sid]
    slot = SLOTS[role]
    if role in {"extract", "register"} and index.docs is not None and sid in index.docs:
        if index.is_docs_head(role, sid, correlation_id):
            entry[slot] = (None, found)
        return
    started = math.inf if first is None else first
    key = (*index.policy_rank(STANDING_NAME.fullmatch(correlation_id)[3]), started) \
        if role == "review" else (started,)
    if entry[slot] is None or entry[slot][0] is None or key >= entry[slot][0]:
        entry[slot] = (key, found)


def scan(c, root, limit, runs, corrected, index):
    """Verify every page and fold it into signature counts, newest times, the entries kept to show
    and one small entry per document.

    Superseded requests (``corrected`` and ``index.stale``, and every request of a corrected
    submission) are verified with their page but only counted as superseded."""
    counts, per_run = Counter(), {name: Counter() for name in runs}
    superseded, superseded_runs = Counter(), {name: Counter() for name in runs}
    latest = dict.fromkeys(per_run)
    newest, recent, groups, info, after = None, Best(limit), {}, {}, None
    # One entry per document, allocated before the pages so the long-lived entries sit together.
    documents = {sid: [None, None, None, None] for sid, head in index.uploads.items() if head}
    publications, roles = Counter(), Counter()
    published = None
    while True:
        with ledger_errors():
            c.execute("BEGIN")
            rows = store.snapshot(c, after, PAGE)
            c.execute("COMMIT")
        requests, stages_of, attempts_of, _ = rows
        if not requests:
            break
        layouts = store.verify_page(rows, root, _memo)
        for row, layout in zip(requests, layouts):
            correlation_id = row["correlation_id"]
            role, sid = role_of(correlation_id, layout, index)
            reason = "correction" if correlation_id in corrected else None
            if reason is None and role == "review":
                reason = index.stale.get(correlation_id)
            if reason is None and sid is not None and index.uploads.get(sid) is False:
                reason = "submission"
            if reason is not None:
                superseded[reason] += 1
                for name, prefix in runs.items():
                    superseded_runs[name][reason] += correlation_id.startswith(prefix)
                continue
            statuses, first, updated = request_state(correlation_id, stages_of[correlation_id], layout,
                                                     attempts_of)
            signature = (layout, statuses)
            if signature not in info:
                info[signature] = stage_signature(layout, statuses)
            funnel, state, code = found = info[signature]
            counts[signature] += 1
            newest = newer(newest, updated)
            for name, prefix in runs.items():
                if correlation_id.startswith(prefix):
                    per_run[name][signature] += 1
                    latest[name] = newer(latest[name], updated)
            if role == "publication":
                publications[funnel] += 1
                if all(status == "succeeded" for status, _ in statuses):
                    published = newer(published, updated)
            elif role is not None and index.uploads.get(sid):
                offer_document(documents, index, role, sid, correlation_id,
                               (found, updated) if role == "review" else found, first)
            roles["unattributed" if role not in {None, "publication"} and not index.uploads.get(sid)
                  else role or "other"] += 1
            order = (-(updated or 0), correlation_id)

            def build(row=row, stages=stages_of[correlation_id], attempts_of=attempts_of):
                return candidate(row, stages, attempts_of)
            recent.offer(order, build)
            if state in CODED:
                group = groups.setdefault((state, funnel, code), [0, Best(limit)])
                group[0] += 1
                group[1].offer(order, build)
        after = requests[-1]["correlation_id"]
    return {"counts": counts, "per_run": per_run, "latest": latest, "newest": newest, "recent": recent,
            "groups": groups, "superseded": superseded, "superseded_runs": superseded_runs,
            "documents": documents, "publications": publications, "published": published, "roles": roles}


# Each role's request funnel as a document state: a staged extraction is still extracting and a
# staged standing review is still pending review.
AS_DOCUMENT = {"extract": {"staged": "extracting"}, "register": {"staged": "extracting"},
               "review": {"staged": "pending-review"}}


def fold(entry, special):
    """One document's ``(funnel state, deciding (funnel, status, code) or None)``.

    The receipt decides until it succeeds, then the extraction and the registration, then the
    current standing review. A selected document stays ``selected``: publication requests do not
    name the documents they publish, so the publications and index sections report publication."""
    receipt, extract, register, review = entry
    if special is not None:
        return special, None
    if receipt is None:
        return "staged", None
    if receipt[1][0] != "received":
        return receipt[1][0], receipt[1]
    if extract is None:
        return "received", None
    for role, slot in (("extract", extract), ("register", register)):
        if slot is None:
            return "extracting", None
        funnel = AS_DOCUMENT[role].get(slot[1][0], slot[1][0])
        if funnel != "extracted":
            return funnel, slot[1]
    if review is None:
        return "extracted", None
    found = review[1][0]
    return AS_DOCUMENT["review"].get(found[0], found[0]), found


def summarize_documents(tally, index):
    funnel, groups = dict.fromkeys(DOC_FUNNEL, 0), Counter()
    documents = tally["documents"]
    for sid, head in index.uploads.items():
        if not head:
            continue
        state, found = fold(documents[sid], index.special.get(sid))
        funnel[state] += 1
        if found is None and state in {"mismatch", "ambiguous"}:
            groups[(state, state, None)] += 1
        elif found is not None and found[1] in CODED:
            groups[(found[1], state, found[2])] += 1
    roles = tally["roles"]
    return {
        "available": True, "source": "requests" if index.docs is None else "docs",
        "documents": sum(funnel.values()), "funnel": funnel,
        "attention_groups": [{"state": state, "funnel": f, "code": code, "count": count}
                             for (state, f, code), count in sorted(
                                 groups.items(), key=lambda g: (-g[1], tuple(str(x) for x in g[0])))],
        "attention_total": sum(groups.values()),
        "requests": {"documents": sum(roles[r] for r in SLOTS), "unattributed": roles["unattributed"],
                     "publication": roles["publication"], "other": roles["other"]},
    }


def summarize_publications(tally):
    found = tally["publications"]
    return {"requests": sum(found.values()), "funnel": {k: v for k, v in sorted(found.items())},
            "last_published": tally["published"]}


def summarize(tally, index, limit):
    groups = tally["groups"]
    return {
        "overall": aggregate(tally["counts"], tally["newest"], tally["superseded"]),
        "runs": {name: aggregate(tally["per_run"][name], tally["latest"][name], tally["superseded_runs"][name])
                 for name in tally["per_run"]},
        "documents": summarize_documents(tally, index),
        "publications": summarize_publications(tally),
        "recent": [public(make_item(entry)) for _, entry in tally["recent"].entries],
        "attention_groups": [
            {"state": state, "funnel": funnel, "code": code, "count": count}
            for (state, funnel, code), (count, _) in sorted(
                groups.items(), key=lambda g: (-g[1][0], tuple(str(x) for x in g[0])))],
        "attention_total": sum(count for count, _ in groups.values()),
        "attention": [public(make_item(entry)) for entry in spread(groups, limit)],
    }


def read_ledger(root, timeout=2.0, limit=50, runs=None):
    """Verify the whole ledger a page at a time and reduce it to counts and the few items shown.

    Every page is read in its own short read transaction and verified after it ends (so, as
    described under "Intake ledger" in docs/design/current-state.md, no read lock is held across
    digest or shape work). Only counters, the ``limit`` newest items per list and a few hundred
    bytes per document are kept, so memory follows the page and the document count, not the ledger.
    """
    started = time.perf_counter()
    runs = runs or {}
    root, db, c = open_ledger(root, timeout)
    with closing(c):
        with ledger_errors():
            c.execute("BEGIN")
            before = first_identity(c, db)
            key = (str(root), before, limit, tuple(sorted(runs.items())),
                   datetime.now(timezone.utc).date().isoformat())
            if before is not None and _cached.get("key") == key:
                moving = rates(c, time.time())
                c.execute("COMMIT")
                return {**_cached["summary"], "rates": moving, "cached": True,
                        "seconds": round(time.perf_counter() - started, 3)}
            store.check_identity(c)
            # Requests some correction points at are not chain heads; read once, not per row.
            corrected = {parent for (parent,) in c.execute(
                "SELECT DISTINCT correction_of FROM requests WHERE correction_of IS NOT NULL")}
            c.execute("COMMIT")
        index = ledger_index(c, corrected)
        tally = scan(c, root, limit, runs, corrected, index)
        after_identity, moving = last_identity(c, db, before)
    summary = summarize(tally, index, limit)
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


def aggregate(counts, last_activity, superseded=None):
    """Counts for a ``Counter`` of (layout, ((status, code), ...)) request signatures of chain heads;
    ``superseded`` counts the requests left out of them by reason (``SUPERSEDED``)."""
    superseded = superseded or Counter()
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
            "exceptions": exceptions, "retrying": retrying, "superseded": sum(superseded.values()),
            "superseded_by": {reason: superseded[reason] for reason in SUPERSEDED},
            "last_activity": last_activity}


def public(value):
    return {key: data for key, data in value.items() if key != "kinds"}


# Other sources ------------------------------------------------------------

_catalog_seen = {}  # canonical binding -> state of a full inspection this process has done today
# Larger catalogs are never inspected by a render: a full pass would hold the read lock for its
# whole O(history) replay (about 21 s at 800 MB) and block writers and the identity probe (Kata 8as8).
INSPECT_LIMIT = 32 * 1024 * 1024


def read_catalog(root):
    from tools.catalog import catalog_store as catalog, contracts
    try:
        home, db, _ = catalog.locations(root)
        catalog.require(db.exists(), "Catalog missing; initialize the instance")
        catalog.require(home.is_dir() and home.stat().st_mode & 0o777 == 0o700,
                        "Catalog home missing or not private")
        with closing(catalog.connect(db)) as connection:
            connection.execute("BEGIN")
            state = catalog.observed_state(connection, home, _catalog_seen, inspect_limit=INSPECT_LIMIT)
            if state is None:
                return {"available": False, "error": "unvalidated",
                        "detail": "No read-validation receipt covers this catalog file; "
                                  "run python -m tools.catalog.cli --root . status"}
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
            reasons = index.freshness(connection, root, manifest, db, config, checksum, reuse=True)
            counts = index.count_index(connection)
            integrity = index.integrity_flags.load(connection).summary()
    except (index.IndexError_, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        return {**failure(error), "fresh": False}
    return {"available": True, "fresh": not reasons, "reasons": reasons[:10],
            "stale_reasons": len(reasons), "selected": selected, "indexed": counts,
            "integrity": integrity}


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
                "documents": {"available": False, "documents": 0, "funnel": dict.fromkeys(DOC_FUNNEL, 0),
                              "attention_groups": [], "attention_total": 0},
                "publications": {"requests": 0, "funnel": {}, "last_published": None},
                "recent": [], "attention": [], "attention_groups": [], "attention_total": 0,
                "rates": None, "cached": False, "seconds": None}
    index = read_index(root)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "body_free": True, "read_only": True,
        "ledger": ledger_state, "documents": view["documents"], "publications": view["publications"],
        "overall": view["overall"],
        "runs": {name: {"prefix": prefix, **view["runs"][name]}
                 for name, prefix in sorted((runs or {}).items())},
        "recent": view["recent"], "attention": view["attention"],
        "attention_groups": view["attention_groups"], "attention_total": view["attention_total"],
        "rates": view["rates"],
        "render": {"ledger_seconds": view["seconds"], "ledger_cached": view["cached"]},
        # The index freshness check validates the catalog and leaves a receipt, so it runs first.
        "index": index, "catalog": read_catalog(root), "review_events": read_review_events(root),
    }
