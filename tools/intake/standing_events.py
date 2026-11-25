"""The deterministic review event a standing qualification records, and the checks that bind it.

A qualified rendition is recorded as exactly one review event whose every field is a function of
the qualification result, the review arguments and its recorded time. ``bound_event`` and
``matches`` accept only that exact event; ``earlier_standing_review`` recognises the same policy's
own review under an earlier policy version, which a new version may supersede.
"""
from datetime import datetime, timezone
import re

from tools.context_index import confidence, qualification
from tools.review_events import event_reader
from tools.review_events import event_store as events

STANDING_STATEMENT = re.compile(
    r"Standing policy ([a-z0-9][a-z0-9-]{0,99}) v([1-9][0-9]*) \(sha256 ([a-f0-9]{64})\) "
    r"authorized this exact rendition review\.")


NOTED = (("contradictions", "Reviewer-noted contradiction: "),
         ("missing_evidence", "Reviewer-noted missing evidence: "))
MAX_EVIDENCE = 30  # the review-event contract's limit on evidence statements


def _noted(result, room):
    """Statements attaching the reviewer's reported issues, verbatim, in ``room`` evidence slots.

    One count statement, then each contradiction and each missing-evidence item. When they do
    not all fit, the event says how many were recorded rather than dropping any silently."""
    items = [prefix + text for key, prefix in NOTED for text in result.get(key, [])]
    if not items:
        return []
    counts = (f"Reviewer-noted issues: {len(result.get('contradictions', []))} contradictions and "
              f"{len(result.get('missing_evidence', []))} missing-evidence items; "
              "accepted with these attached.")
    if len(items) + 1 <= room:
        return [counts, *items]
    kept = items[:room - 2]
    return [counts, *kept, f"Reviewer-noted issues truncated: recorded the first {len(kept)} of "
            f"{len(items)} (contradictions first); findings sha256 {result['findings_sha256']} "
            "holds all of them."]


FLAGGED = "Reviewer-flagged "  # confidence.FLAGGED_PREFIX


def _flagged(result):
    """Header statements for the reviewer's flags (v2 criteria), and each embedded-instruction item."""
    headers, instructions = [], result.get("embedded_instructions", [])
    if result.get("sparse"):
        headers.append(FLAGGED + "sparse record: accepted as a coherent record with little "
                       "content; qualified as low confidence.")
    if instructions:
        headers.append(FLAGGED + f"embedded instructions: {len(instructions)} instructions in the "
                       "records were treated as document data, not followed; qualified as low "
                       "confidence.")
    return headers, [FLAGGED + "embedded instruction: " + text for text in instructions]


def _fit(items, room, findings_sha256):
    if len(items) <= room:
        return items
    kept = items[:max(room - 1, 0)]
    return kept + [f"Reviewer-flagged embedded instructions truncated: recorded the first "
                   f"{len(kept)} of {len(items)}; findings sha256 {findings_sha256} holds all "
                   "of them."]


def _parts(result):
    """One statement saying the rendition was reviewed in parts, and what each part covered."""
    parts = result.get("parts")
    if not parts or len(parts) < 2:
        return []
    each = "; ".join(f"part {n}: records {p['first']}-{p['last']}, {p['warnings']} warnings, {p['decision']}"
                     for n, p in enumerate(parts, 1))
    return [confidence.PARTS_PREFIX + f"{len(parts)} parts, each inspected separately and combined "
            f"by the strictest decision; consistency across parts was not checked. {each} "
            f"(findings sha256 {result['findings_sha256']})."]


def _reductions(result):
    reduced = result.get("reductions")
    if not reduced:
        return []
    return [confidence.REDUCED_PREFIX + f"{reduced['count']} image(s), {reduced['bytes']} bytes of inline data "
            f"were replaced by placeholders before review; the original bytes are unchanged "
            f"(findings sha256 {result['findings_sha256']} lists them)."]


def _statements(result, scope_counts):
    policy_ref = result["policy"]
    statements = [
        f"Standing policy {policy_ref['policy_id']} v{policy_ref['version']} "
        f"(sha256 {policy_ref['sha256']}) authorized this exact rendition review.",
        scope_counts + f" (findings sha256 {result['findings_sha256']}).",
        "Reviewer explanation: " + result["explanation"],
    ]
    if result.get("deferred"):
        # confidence.DEFERRED_PREFIX: the index reads this statement as a low-confidence reason.
        statements.append("Reviewer-deferred: the reviewer deferred judgement; qualified as low "
                          "confidence by operator opt-in, not accepted.")
    if result["exception_resolution"]:
        statements.append("Exception resolution: " + result["exception_resolution"])
    if result.get("unverifiable_references"):
        statements.append(f"Reviewer noted {result['unverifiable_references']} advisory "
                          "references to material outside the rendition.")
    statements += _parts(result) + _reductions(result)
    headers, instructions = _flagged(result)
    statements += headers
    # Keep one slot for the embedded instructions (an item, or their truncation statement).
    statements += _noted(result, MAX_EVIDENCE - len(statements) - bool(instructions))
    return statements + _fit(instructions, MAX_EVIDENCE - len(statements), result["findings_sha256"])


def review_event(result, args, recorded_at=None):
    recorded = recorded_at or datetime.now(timezone.utc).replace(microsecond=0)
    policy_ref = result["policy"]
    inspected = result["inspected"]
    if result["format"] == "docling":
        scope = qualification.COMPLETE_SCOPE
        covered = (f"Inspected all {inspected['pages']} pages, {inspected['texts']} text records, "
                   f"{inspected['tables']} tables and {inspected['warnings']} conversion warnings")
    else:
        scope = qualification.EXTRACT_COMPLETE_SCOPE
        covered = (f"Inspected all {inspected['records']} {result['format']} extraction records "
                   f"and {inspected['warnings']} extraction warnings")
    source = result["source"]
    return {
        "event_id": args["event_id"],
        "source": {"receipt_id": source["receipt_id"], "source_id": source["source_ids"][0],
                   "locator": args["rendition"], "parent_locator": source["path"],
                   "checksum": {"sha256": args["rendition_sha256"], "basis": "reported"}},
        "scope": {"content": result["content"], "unit": "file", "extent": "defined_scope",
                  "coordinates": "not_applicable", "ranges": [],
                  "description": scope},
        "outcome": "reviewed",
        "counts": {"total": 1, "reviewed": 1, "pending": 0, "unreadable": 0, "dispositioned": 0},
        "reason": ("Qualified as low confidence under explicit standing policy after a reviewer "
                   "deferral; parser completion is not verification." if result.get("deferred") else
                   "Qualified under explicit standing policy; parser completion is not verification."),
        "reviewer": result["reviewer"], "reviewed_on": recorded.date().isoformat(),
        "recorded_at": recorded.isoformat(),
        "evidence": [{"kind": "agent_statement", "pointer": None, "quote": q}
                     for q in _statements(result, covered)],
        "uncertainties": [],
        "handling": [events.contract.HANDLING],
        "provenance": {"origin": "agent_review", "record": None,
                       "mapping_version": None, "unmapped_fields": []},
        "relationships": [], "supersedes": list(result.get("supersedes", [])),
        "correction_reason": (
            f"Re-reviewed under standing policy {policy_ref['policy_id']} v{policy_ref['version']}, "
            "replacing this policy's earlier-version standing review."
            if result.get("supersedes") else None),
    }


def standing_claims(event):
    """``(policy_id, version, sha256)`` of each standing-authorization statement in ``event``."""
    quotes = [e.get("quote") or "" for e in (event or {}).get("evidence", [])]
    return [m.groups() for q in quotes for m in [STANDING_STATEMENT.fullmatch(q)] if m]


def earlier_standing_review(event, standing, source, qualified, args):
    """True for this standing policy's own review of the same rendition, source and
    scope under an earlier policy version: its event ID must be the one that earlier
    policy's review would have produced, so operator corrections never qualify."""
    if (event["source"]["locator"] != args["rendition"]
            or event["source"]["receipt_id"] != source["receipt_id"]
            or event["source"]["source_id"] != source["source_ids"][0]
            or event["provenance"]["origin"] != "agent_review"
            or event["scope"]["content"] != qualified["content"]
            or event["scope"]["description"] != (qualification.COMPLETE_SCOPE if qualified["format"] == "docling"
                                                 else qualification.EXTRACT_COMPLETE_SCOPE)):
        return False
    claims = standing_claims(event)
    suffix = "-" + args["policy_sha256"][:8]
    if len(claims) != 1 or not args["event_id"].endswith(suffix):
        return False
    policy_id, version, sha = claims[0]
    own = args["event_id"][:-len(suffix)] + "-" + sha[:8]
    reason = event["correction_reason"]
    return (policy_id == standing["policy_id"] and int(version) < standing["version"]
            and event["event_id"] == own
            and (not event["supersedes"]
                 or (reason or "").startswith(f"Re-reviewed under standing policy {policy_id} v{version},")))


def bound_event(root, result, args, snapshot=None):
    """Return the stored active event when it binds exactly this qualification.

    ``snapshot`` is a read taken after the event could have been committed; without one the store
    is read here."""
    try:
        if snapshot is None:
            snapshot = event_reader.read_snapshot(root)
    except events.contract.ReviewError:
        return None
    event = snapshot.event(args["event_id"])
    if event is None or snapshot.superseded_by(args["event_id"]) is not None or not matches(event, result, args):
        return None
    return event


def matches(event, result, args):
    """True only for the exact deterministic standing event of this qualification."""
    if event["event_id"] != args["event_id"]:
        return False
    try:
        recorded = datetime.fromisoformat(event["recorded_at"])
        return event == review_event(result, args, recorded)
    except (TypeError, ValueError, KeyError):
        return False
