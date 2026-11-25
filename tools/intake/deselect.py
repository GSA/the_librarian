"""Remove selections from the shared selection manifest, with evidence (Kata bvvc).

``cohort deselect`` reports (dry run, the default) or with ``--apply`` removes manifest selections:

* **by receipt** (``--receipt D123``, ``--receipts-file``: IDs one per line, or the report of an
  earlier dry run);
* **by exclusion rule** (``--rule ID`` or ``--all-rules`` from the instance's exclusion file, see
  ``exclusions``): the selections whose inbox path, read from the intake ledger's ``docs`` rows, a
  rule matches;
* **byte-identical duplicates** (``--dedupe``): selections of different receipts whose original has
  the same SHA-256 form a group; every group is reported, and ``--dedupe`` keeps the copy with the
  best path class (``path_rank``: plain folder, archive member, build/cache, VCS internals), then the
  lowest receipt, among those no other selector removes and deselects the rest.

Receipts, retained originals, renditions and extractions are never touched: only the manifest entry
goes, so ``publish`` stops serving it. The removal is durable in three places, written in this order
so that a crash anywhere is finished by running the same command again:

1. ``<intake home>/deselections.jsonl`` (private, append-only, fsynced): a ``begin`` record naming
   every entry and why, then, once the manifest is rewritten, a ``done`` record;
2. one review event per removed entry that supersedes the entry's live review (outcome
   ``dispositioned``, event ID ``rev-deselect-<hash>``). This is what makes the removal stick: a
   later ``qualify`` sees the saved standing review superseded and does not select it again, and a
   later policy version cannot silently re-qualify it (the new review is blocked as conflicting);
3. the atomic, validated rewrite of the manifest under the ledger writer lock, after recovering an
   interrupted append.

A manifest that still holds entries a ``begin`` record names (a crash after step 1 or 2) has them
removed by the next ``--apply``, whatever selectors it is given. A rerun with nothing left to remove
changes nothing.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from tools.catalog import catalog_store as catalog
from tools.context_index import context_index as index
from tools.durable import fsync_directory
from tools.review_events import event_history, event_reader
from tools.review_events import event_store as events
from . import docs, selection_manifest, standing_publication, store
from .contracts import IntakeError, require

LOG = "deselections.jsonl"
SCHEMA = "librarian-deselect/1"
REPORT_SCHEMA = "librarian-deselect-report/1"
BATCH = 200  # review events per append (the review-event contract allows 256)
RETRIES = 5
LOG_LIMIT = 256 * 1024 * 1024
RECEIPT = re.compile(r"D0*[1-9][0-9]*")
ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}")


VCS_DIRECTORY = re.compile(r"(?:^|/)(?:\.svn|\.git|\.hg|CVS)(?:/|$)", re.IGNORECASE)
BUILD_DIRECTORY = re.compile(r"(?:^|/)(?:build|dist|target|node_modules|\.cache|\.m2|__pycache__)(?:/|$)",
                             re.IGNORECASE)


def path_rank(path):
    """How poor an inbox path is as the one search results cite for a document (lower is better).

    0 a plain document folder (and a receipt with no known path); 1 an archive member (``A.zip!/m``) or
    an operator's ``_extracted`` folder; 2 build or cache output; 3 version-control internals. A path
    takes its worst class, so a member inside ``.svn`` is 3 whatever archive holds it.
    """
    if not path:
        return 0
    if VCS_DIRECTORY.search(path):
        return 3
    if BUILD_DIRECTORY.search(path):
        return 2
    return 1 if "!/" in path or "/_extracted/" in "/" + path else 0


def _number(receipt):
    return int(receipt[1:])


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_receipts(path):
    """Receipt IDs from a file of IDs (one per line, ``#`` comments) or a ``--report`` of a dry run."""
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 64 * 1024 * 1024:
            raise IntakeError("invalid_receipts")
        text = raw.decode("utf-8")
        if text.lstrip().startswith("{"):
            value = json.loads(text)
            require(isinstance(value, dict) and value.get("schema") == REPORT_SCHEMA
                    and isinstance(value.get("entries"), list), "invalid_receipts")
            found = [e.get("receipt_id") for e in value["entries"] if isinstance(e, dict)]
        else:
            found = [line.split("#", 1)[0].strip() for line in text.splitlines()]
            found = [f for f in found if f]
    except (OSError, UnicodeError, ValueError) as error:
        raise IntakeError("invalid_receipts") from error
    require(all(isinstance(f, str) and RECEIPT.fullmatch(f) for f in found), "invalid_receipts")
    return found


# -- the append-only log --------------------------------------------------------------------------

def _log_path(root):
    _, home, _ = store.locations(root)
    return store.safe(home / LOG)


def _records(path):
    """Every saved record; a torn last line (a crash while appending) is ignored."""
    if not os.path.lexists(path):
        return []
    with open(path, "rb") as stream:
        raw = stream.read(LOG_LIMIT + 1)
    require(len(raw) <= LOG_LIMIT, "deselect_log_invalid")
    lines, records = raw.split(b"\n"), []
    for number, line in enumerate(lines):
        if not line:
            continue
        try:
            records.append(json.loads(line.decode("utf-8")))
        except (UnicodeError, ValueError) as error:
            if number != len(lines) - 1:
                raise IntakeError("deselect_log_invalid") from error
    return records


def _repair_tail(descriptor):
    """End a complete last record that lacks only its newline; cut a torn one back to the last complete line."""
    size = os.fstat(descriptor).st_size
    if not size or os.pread(descriptor, 1, size - 1) == b"\n":
        return
    start = size
    while start > 0:
        start = max(0, start - 65536)
        block = os.pread(descriptor, size - start, start)
        cut = block.rfind(b"\n")
        if cut >= 0:
            start += cut + 1
            break
    tail = os.pread(descriptor, size - start, start)
    try:
        json.loads(tail.decode("utf-8"))
    except (UnicodeError, ValueError):
        os.ftruncate(descriptor, start)
    else:
        os.write(descriptor, b"\n")
    os.fsync(descriptor)


def _append(path, record):
    created = not os.path.lexists(path)
    descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _repair_tail(descriptor)
        os.write(descriptor, (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if created:
        fsync_directory(path.parent)


def _open_plans(records, manifest):
    done = {r["begin_id"] for r in records if r.get("kind") == "done" and r.get("manifest") == manifest}
    return [r for r in records if r.get("kind") == "begin" and r.get("manifest") == manifest
            and r["id"] not in done]


# -- planning -------------------------------------------------------------------------------------

def _manifest(root, name):
    path = index.no_symlinks(Path(root) / name)
    raw_bytes = path.read_bytes()
    index.load_manifest(path, root)
    return path, json.loads(raw_bytes.decode("utf-8"), object_pairs_hook=index.strict_object), \
        hashlib.sha256(raw_bytes).hexdigest()


def _rows(c, receipts):
    """``{receipt ID: (system, path, original sha256)}`` of the ledger's ``docs`` rows."""
    found = {}
    if not docs.present(c):
        return found
    wanted = sorted(receipts)
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        for row in c.execute("SELECT system, path, sha256, receipt_id FROM docs WHERE receipt_id IN (%s) "
                             "ORDER BY system, path" % ",".join("?" * len(chunk)), chunk):
            found.setdefault(row["receipt_id"], (row["system"], row["path"], row["sha256"]))
    return found


def _originals(root, rows, receipts):
    """``{receipt ID: original SHA-256}``: from the current ``docs`` row when there is one, else from the catalog.

    A receipt with no row (the optional ``docs`` table is absent, or a correction moved the row to a
    newer receipt) still has its original's hash in the authoritative catalog, so duplicates are
    found without the table. A receipt the catalog cannot show is left out.
    """
    found = {receipt: row[2] for receipt, row in rows.items() if row[2]}
    missing = sorted(set(receipts) - found.keys())
    for start in range(0, len(missing), 2000):
        chunk = missing[start:start + 2000]
        try:
            held = catalog.receipt_metadata(root, chunk)["registrations"]
        except (catalog.CatalogError, OSError, ValueError) as error:
            raise IntakeError("deselect_catalog_unavailable") from error
        for receipt, registration in held.items():
            sha256 = registration["artifact"]["data"].get("sha256")
            if sha256:
                found[receipt] = sha256
    return found


def build_plan(root, c, selected, *, receipts=(), rules=None, rule_ids=(), system=None, dedupe=False):
    """What the selectors remove from the manifest entries ``selected``, and every duplicate group.

    Returns ``{"entries": [...], "groups": [...], "unmatched": [...], "docs": n}``; each removed entry
    is ``{path, sha256, receipt_id, system, inbox_path, reason}`` (``system`` and ``inbox_path`` are
    ``None`` for a receipt the ledger has no row for).
    """
    rows = _rows(c, {e["receipt_id"] for e in selected})
    originals = _originals(root, rows, {e["receipt_id"] for e in selected})
    wanted = set(receipts)
    removed = {}
    for entry in selected:
        row = rows.get(entry["receipt_id"])
        reason = None
        if entry["receipt_id"] in wanted:
            reason = "receipt"
        elif rules is not None and row is not None and (system is None or row[0] == system):
            rule = rules.match(row[1])
            if rule is not None and (not rule_ids or rule in rule_ids):
                reason = "rule:" + rule
        if reason is not None:
            removed[entry["path"]] = reason
    by_original = defaultdict(list)
    for entry in selected:
        if entry["receipt_id"] in originals:
            by_original[originals[entry["receipt_id"]]].append(entry)
    groups = []
    for original, members in sorted(by_original.items()):
        if len({m["receipt_id"] for m in members}) < 2:
            continue
        members.sort(key=lambda m: (path_rank(rows[m["receipt_id"]][1] if m["receipt_id"] in rows else None),
                                    _number(m["receipt_id"]), m["path"]))
        left = [m for m in members if m["path"] not in removed]
        keep = left[0]["receipt_id"] if left else None
        if dedupe and keep is not None:
            for member in left:
                if member["receipt_id"] != keep:
                    removed[member["path"]] = "duplicate-of:" + keep
        ordered = sorted(members, key=lambda m: (_number(m["receipt_id"]), m["path"]))
        groups.append({"sha256": original, "keep": keep, "receipts": [m["receipt_id"] for m in ordered],
                       "removed": [m["receipt_id"] for m in ordered if m["path"] in removed]})
    entries = []
    for entry in selected:
        if entry["path"] in removed:
            row = rows.get(entry["receipt_id"])
            entries.append({"path": entry["path"], "sha256": entry["sha256"], "receipt_id": entry["receipt_id"],
                            "system": row and row[0], "inbox_path": row and row[1],
                            "reason": removed[entry["path"]],
                            "review_event_ids": list(entry.get("review_event_ids") or [])})
    listed = {e["receipt_id"] for e in selected}
    return {"entries": entries, "groups": groups, "unmatched": sorted(wanted - listed, key=_number),
            "docs": len(rows)}


def _counts(selected, plan):
    reasons, by_rule = defaultdict(int), defaultdict(int)
    for entry in plan["entries"]:
        kind, _, detail = entry["reason"].partition(":")
        reasons[kind] += 1
        if kind == "rule":
            by_rule[detail] += 1
    groups = plan["groups"]
    return {"selections": len(selected), "deselect": len(plan["entries"]),
            "remaining": len(selected) - len(plan["entries"]),
            "by_reason": dict(sorted(reasons.items())), "by_rule": dict(sorted(by_rule.items())),
            "unmapped": sum(e["inbox_path"] is None for e in plan["entries"]),
            "unmatched_receipts": len(plan["unmatched"]),
            "duplicates": {"groups": len(groups), "extra_selections": sum(len(g["receipts"]) - 1 for g in groups),
                           "deselected": sum(len(g["removed"]) for g in groups)}}


# -- evidence -------------------------------------------------------------------------------------

def _basis(entry):
    reason = entry["reason"]
    if reason.startswith("rule:"):
        return "its inbox path matches exclusion rule " + reason[5:]
    if reason.startswith("duplicate-of:"):
        return ("its original is byte-identical to the one of receipt " + reason[len("duplicate-of:"):]
                + ", which stays selected")
    return "the operator named its receipt"


def _head(snapshot, event_id):
    seen = {event_id}
    while (successor := snapshot.superseded_by(event_id)) is not None and successor not in seen:
        event_id = successor
        seen.add(event_id)
    return event_id


def _event(prior, actor, basis, reason, now):
    target = prior["event_id"]
    recorded = max(now, datetime.fromisoformat(prior["recorded_at"]).replace(microsecond=0)
                   + timedelta(seconds=1))
    said = f"Deselected from the selection manifest: {basis}."
    if reason:
        said += " Operator note: " + reason
    unknown = prior["source"]["source_id"] is None or prior["source"]["checksum"]["sha256"] is None
    uncertain = ["The superseded review recorded no source or checksum identity."] if unknown else []
    return {
        "event_id": "rev-deselect-" + hashlib.sha256(target.encode()).hexdigest()[:24],
        "source": prior["source"], "scope": prior["scope"], "outcome": "dispositioned",
        "counts": {"total": 1, "reviewed": 0, "pending": 0, "unreadable": 0, "dispositioned": 1},
        "reason": said + " This is a selection decision, not a judgement of the content.",
        "reviewer": actor, "reviewed_on": recorded.date().isoformat(), "recorded_at": recorded.isoformat(),
        "evidence": [{"kind": "agent_statement", "pointer": None, "quote": said}],
        "uncertainties": uncertain, "handling": [events.contract.HANDLING],
        "provenance": {"origin": "agent_review", "record": None, "mapping_version": None,
                       "unmapped_fields": []},
        "relationships": [], "supersedes": [target], "correction_reason": said}


def _supersessions(root, entries, actor, reason):
    """The review events that supersede the live reviews the removed entries cite (none already made)."""
    snapshot = event_reader.read_snapshot(root)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    made, seen = [], set()
    for entry in entries:
        for cited in entry["review_event_ids"]:
            if snapshot.event(cited) is None:
                continue
            head = _head(snapshot, cited)
            if head in seen or head.startswith("rev-deselect-"):
                continue
            seen.add(head)
            made.append(_event(snapshot.event(head), actor, _basis(entry), reason, now))
    return made


def _append_events(root, made, actor):
    """Append ``made`` in batches; each batch retries on a stale read, and an earlier committed one is idempotent."""
    for start in range(0, len(made), BATCH):
        batch = made[start:start + BATCH]
        request = "req-deselect-" + _digest([e["event_id"] for e in batch])[:40]
        for attempt in range(RETRIES):
            state = event_reader.read_snapshot(root).state
            try:
                events.append(root, event_history.bundle(batch, root), actor, request,
                              state["revision"], state["state_token"])
                break
            except events.contract.ReviewError as error:
                if attempt == RETRIES - 1 or "Stale revision" not in str(error):
                    raise IntakeError("deselect_review_events_failed") from error


# -- the command ----------------------------------------------------------------------------------

def _check(root, manifest, actor, apply):
    root = Path(root).resolve()
    require(isinstance(manifest, str) and manifest.endswith(".json"), "invalid_manifest")
    try:
        index.relative_name(manifest)
        manifest.encode("ascii")
        index.no_symlinks(root / manifest)
    except (index.IndexError_, UnicodeError) as error:
        raise IntakeError("unsafe_path") from error
    require(not apply or (isinstance(actor, str) and ACTOR.fullmatch(actor)), "actor_required")
    return root


def _recover(root, manifest):
    try:
        if os.path.lexists(selection_manifest.intent_path(root, manifest)):
            with store.locked(root):
                selection_manifest.recover(root, manifest)
    except (index.IndexError_, OSError, ValueError) as error:
        raise IntakeError("preflight_invalid") from error


def _report(manifest, digest, plan, selected, apply):
    return {"schema": REPORT_SCHEMA, "apply": apply, "manifest": manifest, "manifest_sha256": digest,
            "counts": _counts(selected, plan), "entries": plan["entries"], "groups": plan["groups"],
            "unmatched": plan["unmatched"]}


def _finish(plan, begun, selected):
    """Add to ``plan`` the entries of earlier, unfinished runs that are still selected."""
    present = {(e["receipt_id"], e["sha256"]): e for e in selected}
    planned = {(e["receipt_id"], e["sha256"]) for e in plan["entries"]}
    for record in begun:
        for receipt, sha256, why in record["entries"]:
            if (receipt, sha256) in present and (receipt, sha256) not in planned:
                planned.add((receipt, sha256))
                entry = present[(receipt, sha256)]
                plan["entries"].append({
                    "path": entry["path"], "sha256": sha256, "receipt_id": receipt, "system": None,
                    "inbox_path": None, "reason": why,
                    "review_event_ids": list(entry.get("review_event_ids") or [])})


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def deselect(root, manifest, *, actor=None, receipts=(), rules=None, rule_ids=(), system=None,
             dedupe=False, reason=None, apply=False):
    """Report, and with ``apply`` remove, the manifest selections the selectors name. See the module doc.

    Returns the report: body-free ``counts`` plus, for ``--report``, every entry (paths and receipts)
    and duplicate group. Without a selector only the duplicate groups are reported.
    """
    root = _check(root, manifest, actor, apply)
    require(reason is None or (isinstance(reason, str) and 0 < len(reason) <= 200 and reason.isascii()
                               and reason.isprintable()), "invalid_reason")
    require(all(RECEIPT.fullmatch(receipt) for receipt in receipts), "invalid_receipts")
    require(not rule_ids or rules is not None, "exclusions_missing")
    require(set(rule_ids) <= set(rules.ids if rules is not None else ()), "unknown_rule")
    _recover(root, manifest)
    selectors = dict(receipts=tuple(receipts), rules=rules, rule_ids=tuple(rule_ids), system=system, dedupe=dedupe)
    if not apply:
        _, raw, digest = _manifest(root, manifest)
        selected = raw.get("renditions", [])
        with store.locked(root) as c:
            plan = build_plan(root, c, selected, **selectors)
        return _report(manifest, digest, plan, selected, False)
    with store.locked(root) as c:
        selection_manifest.recover(root, manifest)
        path, raw, digest = _manifest(root, manifest)
        selected = raw.get("renditions", [])
        log = _log_path(root)
        begun = _open_plans(_records(log), manifest)
        plan = build_plan(root, c, selected, **selectors)
        _finish(plan, begun, selected)
        report = _report(manifest, digest, plan, selected, True)
        removing = {e["path"] for e in plan["entries"]}
        report["review_events"] = 0
        if removing:
            triples = sorted([e["receipt_id"], e["sha256"], e["reason"]] for e in plan["entries"])
            plan_sha = _digest([manifest, triples])
            if not any(r["plan_sha256"] == plan_sha for r in begun):
                begin = {"schema": SCHEMA, "kind": "begin", "id": uuid.uuid4().hex, "plan_sha256": plan_sha,
                         "manifest": manifest, "actor": actor, "at": _now(), "reason": reason,
                         "manifest_sha256_before": digest, "counts": report["counts"], "entries": triples}
                _append(log, begin)
                begun.append(begin)
            # Evidence first, so a crash before the rewrite leaves the entries visibly superseded
            # (publication refuses them) and never selected again by a later qualify.
            made = _supersessions(root, plan["entries"], actor, reason)
            _append_events(root, made, actor)
            selection_manifest.forget(path)
            raw["renditions"] = [e for e in selected if e["path"] not in removing]
            data = (json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
            standing_publication._replace_validated(root, path, data)
            selection_manifest.forget(path)
            digest = hashlib.sha256(data).hexdigest()
            report["review_events"] = len(made)
        for record in begun:
            _append(log, {"schema": SCHEMA, "kind": "done", "begin_id": record["id"],
                          "plan_sha256": record["plan_sha256"],
                          "manifest": manifest, "at": _now(), "manifest_sha256_after": digest,
                          "removed": len(removing), "review_events": report["review_events"]})
    report["manifest_sha256_after"] = digest
    return report
