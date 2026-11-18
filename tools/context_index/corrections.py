"""Claim corrections at read time: where each recorded correction sits in the current index, whether its
claim drifted, and how it marks the pieces and search hits that carry the claim.

Corrections are applied when text is read, never built into the index, so recording one needs no
rebuild. A correction applies to indexed text on the same path whose coordinates overlap the corrected
section and that contains the quoted claim (whitespace-collapsed); when the claim is no longer in that
section (``claim-not-found``) it applies to all overlapping text, so a drifted open correction stays
visible until it is confirmed or retracted.
"""

from contextlib import closing
import json

from tools import policy
from tools.context_index import confidence
from tools.context_index.context_index import (
    fail, freshness, get_section, load_manifest, readonly, validate_db_path,
)


def _store():
    try:
        from tools.review_events import corrections
    except ImportError as error:
        fail(f"Claim-correction store unavailable; install ./tools/context-mcp: {error}")
    return corrections


def target(root, locator, claim):
    """The stored target of a new correction; the claim must be in the fresh index's text now."""
    store = _store()
    manifest = policy.selection_path(root)
    config, manifest_hash = load_manifest(manifest, root)
    db = validate_db_path(root, root / policy.validate_instance_roots(root)["context_index_db"])
    with closing(readonly(db)) as connection:
        reasons = freshness(connection, root, manifest, db, config, manifest_hash)
        if reasons:
            fail("Index is stale; run build before recording a correction. " + "; ".join(reasons))
        section = get_section(connection, locator, root)
        if store.normalize(claim) not in store.normalize(section["text"]):
            fail(f"Claim is not in the indexed text at {section['locator']}; quote it exactly as get returns it")
        source_hash = connection.execute("SELECT hash FROM documents WHERE path=?", (section["path"],)).fetchone()[0]
        rendition = connection.execute("SELECT payload FROM rendition_documents WHERE path=?",
                                       (section["path"],)).fetchone()
    return {"locator": section["locator"], "path": section["path"], "line_start": section["line_start"],
            "line_end": section["line_end"], "source_hash": source_hash,
            "receipt_id": json.loads(rendition[0])["source"]["receipt_id"] if rendition else None,
            "claim": claim, "claim_sha256": store.claim_hash(claim)}


def load(root):
    """The validated correction snapshot, or None without a store; an invalid store refuses the read."""
    store = _store()
    try:
        return store.read(root if root is not None else store.contract.ROOT)
    except store.contract.ReviewError as error:
        fail(f"Claim-correction store failed validation; no result returned: {error}")


def _cite(source):
    return None if source is None else {key: value for key, value in source.items() if value is not None}


def _entry(snapshot, record, drift):
    value = {"correction_id": record["correction_id"], "kind": record["kind"], "status": snapshot.status(record),
             "claim": record["target"]["claim"]}
    if record["statement"] is not None:
        value["statement"] = record["statement"]
    value.update(summary=record["summary"], source=_cite(record["source"]), actor=record["actor"],
                 recorded_at=record["recorded_at"])
    if drift is not None:
        value["drift"] = drift
    resolver = snapshot.resolved_by.get(record["correction_id"])
    if resolver is not None:
        resolution = snapshot.by_id[resolver]
        value["resolved_by"] = {key: resolution[key] for key in ("correction_id", "kind", "summary", "actor",
                                                                  "recorded_at")}
        value["resolved_by"]["source"] = _cite(resolution["source"])
    return value


def place(connection, snapshot):
    """One placement per correct/dispute record: its current section, drift and display entry."""
    if snapshot is None:
        return []
    store = _store()
    placements = []
    for record in snapshot.records:
        if record["kind"] not in store.OPENING:
            continue
        target_ = record["target"]
        section = connection.execute(
            "SELECT s.path,s.line_start,s.line_end,s.text,d.hash FROM sections s "
            "JOIN documents d ON d.path=s.path WHERE s.locator=?", (target_["locator"],)).fetchone()
        claim = store.normalize(target_["claim"])
        if section is None:
            drift = "locator-missing"
        elif claim not in store.normalize(section["text"]):
            drift = "claim-not-found"
        elif section["hash"] != target_["source_hash"]:
            drift = "source-changed"
        else:
            drift = None
        placements.append({"section": section, "claim": claim, "drift": drift,
                           "open": snapshot.status(record) == "open",
                           "entry": _entry(snapshot, record, drift)})
    return placements


def _applies(placement, path, start, end, text):
    section = placement["section"]
    if section is None or section["path"] != path or start > section["line_end"] or end < section["line_start"]:
        return False
    return placement["drift"] == "claim-not-found" or placement["claim"] in _store().normalize(text)


def _detail(placement):
    entry = placement["entry"]
    detail = f"{entry['correction_id']} {entry['kind']}: {entry['summary']}"
    if "statement" in entry:
        detail += f" Corrected: {entry['statement']}"
    source = entry["source"]
    detail += " Source: " + (source.get("receipt_id") or "question answered by " + source.get("answered_by", "?"))
    if "drift" in entry:
        detail += f" ({entry['drift']})"
    return detail if len(detail) <= confidence.DETAIL_LIMIT else detail[:confidence.DETAIL_LIMIT - 1] + "…"


def _mark(item, found):
    """Attach ``found`` placements to a piece and add the open ones to its label."""
    item["corrections"] = [placement["entry"] for placement in found]
    opened = [placement for placement in found if placement["open"]]
    if opened:
        item["confidence"] = confidence.with_reason(item["confidence"], "open-correction",
                                                    [_detail(placement) for placement in opened])


def annotate_pieces(connection, pieces, placements):
    """Show corrections next to the pieces that carry their claims (exact pieces, any format)."""
    if not placements:
        return
    for piece in pieces:
        start, end = piece["line_start"], piece["line_end"]
        if start is None:
            row = connection.execute("SELECT line_start,line_end FROM sections WHERE locator=?",
                                     (piece["locator"],)).fetchone()
            if row is None:
                continue
            start, end = row
        found = [placement for placement in placements
                 if _applies(placement, piece["path"], start, end, piece["text"])]
        if found:
            _mark(piece, found)


def sections(connection, placements):
    """``{section id: [placement]}`` for every indexed section a placement applies to."""
    found = {}
    for placement in placements:
        section = placement["section"]
        if section is None:
            continue
        for row in connection.execute(
                "SELECT id,line_start,line_end,text FROM sections WHERE path=? AND line_start<=? AND line_end>=?",
                (section["path"], section["line_end"], section["line_start"])):
            if _applies(placement, section["path"], row["line_start"], row["line_end"], row["text"]):
                found.setdefault(row["id"], []).append(placement)
    return found


def flag_hit(hit, label, found):
    """A search hit's compact confidence and correction flag."""
    if found:
        item = {"confidence": label}
        _mark(item, found)
        label = item["confidence"]
        hit["corrections"] = {
            "open": [p["entry"]["correction_id"] for p in found if p["open"]],
            "resolved": [p["entry"]["correction_id"] for p in found if not p["open"]],
            "notice": "Read the locator with get to see each correction's claim, statement and source."}
    hit["confidence"] = confidence.codes(label)
    return label


def summary(connection, snapshot):
    """Status counts: open and resolved corrections, and open ones whose claim drifted."""
    if snapshot is None:
        return {"store": "absent"}
    placements = place(connection, snapshot)
    drifted = {}
    for placement in placements:
        if placement["open"] and placement["drift"] is not None:
            drifted[placement["drift"]] = drifted.get(placement["drift"], 0) + 1
    return {"store": "present", "state_token": snapshot.state["state_token"], "records": len(snapshot.records),
            "open": sum(p["open"] for p in placements), "resolved": sum(not p["open"] for p in placements),
            "open_drifted": drifted}
