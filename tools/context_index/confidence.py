"""Confidence labels for indexed content: ``normal``, or ``low`` with explicit, correctable reasons.

Low confidence never withholds content. It marks published text as shaky, says why, and says how
to correct it. Every reason has a stable code; the seams that add one are:

- ``RENDITION_REASONS``: rendition kind (``rendition_kind``) -> reason, for converted and
  machine-read renditions (one line per new converter or OCR kind);
- ``review_reasons``: reviewer-noted issues and reviewer deferrals recorded on review events;
- ``origin_reasons``: an origin a submission declares (``agent_generated`` in its sidecar);
- ``with_reason``: adds a reason to an existing label at read time (``open-correction``).
"""

import hashlib
import json

CONTRACT = "confidence-v1"
LEVELS = ("normal", "low")
REASONS = {
    "noted-issues": (
        "The reviewer accepted this rendition but reported contradictions or missing evidence; "
        "the noted issues are attached to its review (qualification.reviewed_scope[].noted_issues).",
        "Confirm or correct the noted issues via a newer document, meeting transcript or answered "
        "question."),
    "reviewer-deferred": (
        "The reviewer deferred judgement under the current standing policy; the text is published "
        "as low confidence instead of being withheld, not as accepted.",
        "Confirm or correct it via a newer document, meeting transcript or answered question, or "
        "re-review it."),
    "converted-format": (
        "Text converted from a more complex format; layout, formatting or structure may be lost.",
        "Check the original file, or confirm via a newer document, meeting transcript or answered "
        "question."),
    "machine-ocr": (
        "Machine-read text (OCR or layout model); characters, tables and reading order may be wrong.",
        "Check the original page or image, or confirm via a newer document, meeting transcript or "
        "answered question."),
    "agent-generated": (
        "Written by an AI agent (declared origin), not a primary source.",
        "Confirm against primary sources via a newer document, meeting transcript or answered "
        "question."),
    "open-correction": (
        "An open claim correction disputes or amends this text; the piece's corrections list the "
        "quoted claim, any corrected statement and the cited source.",
        "Confirm the claim (or retract the correction) with a cited newer document, meeting "
        "transcript or answered question: python -m tools.review_events.corrections resolve."),
}
# Rendition kind -> reason. Kinds are ``docling-<conversion format>`` or an extraction payload
# format; a kind not listed here adds no reason.
RENDITION_REASONS = {
    "docling-pdf": "machine-ocr",
    "docling-image": "machine-ocr",
    "ocr": "machine-ocr",
    # A PDF's own embedded text, not recognised from pixels; only layout and tables are lost.
    "pdf-text": "converted-format",
}
NOTED_PREFIX = "Reviewer-noted "
DEFERRED_PREFIX = "Reviewer-deferred: "
EXPLANATION_PREFIX = "Reviewer explanation: "
SIDECAR_LIMIT = 1024 * 1024
# Labels repeat on every piece of a full read; the review event keeps the whole explanation.
DETAIL_LIMIT = 300


def reason(code, details=()):
    if code not in REASONS:
        raise ValueError(f"Unknown confidence reason: {code}")
    return {"code": code, "details": [str(detail) for detail in details]}


def label(reasons=()):
    """``{"level", "reasons"}`` for ``reasons`` in canonical order, merging details per code."""
    merged = {}
    for item in reasons:
        details = merged.setdefault(item["code"], [])
        details.extend(detail for detail in item["details"] if detail not in details)
    ordered = [{"code": code, "details": merged[code]} for code in REASONS if code in merged]
    return {"level": "low" if ordered else "normal", "reasons": ordered}


def with_reason(current, code, details=()):
    """``current`` with one more reason; the read-time hook (e.g. an open correction)."""
    return label([*current["reasons"], reason(code, details)])


# Reasons added at read time to a piece of text, never stored on a document's indexed label.
READ_TIME = ("open-correction",)


def document_label(current):
    """``current`` without its read-time reasons: the label the document itself was indexed with."""
    return label(item for item in current["reasons"] if item["code"] not in READ_TIME)


def rendition_kind(value):
    """The kind ``RENDITION_REASONS`` maps: ``docling-<format>`` or the extraction payload format."""
    if "conversion" in value:
        return "docling-" + value["conversion"]["format"]
    return value["payload"]["format"]


def review_reasons(reviewed_scope):
    """Reasons recorded on the reviews a qualification binds."""
    found = []
    for scope in reviewed_scope or ():
        noted = scope.get("noted_issues") or []
        if noted:
            contradictions = sum(q.startswith(NOTED_PREFIX + "contradiction: ") for q in noted)
            missing = sum(q.startswith(NOTED_PREFIX + "missing evidence: ") for q in noted)
            found.append(reason("noted-issues", [
                f"{scope['event_id']}: {contradictions} contradictions, {missing} missing-evidence "
                "items attached"]))
        if "deferral" in scope:
            explanation = scope["deferral"]
            if len(explanation) > DETAIL_LIMIT:
                explanation = explanation[:DETAIL_LIMIT - 1] + "…"
            found.append(reason("reviewer-deferred", [f"{scope['event_id']}: {explanation}"]))
    return found


def review_notes(evidence):
    """``(noted issues, deferral explanation or None)`` from a review event's evidence quotes."""
    quotes = [e["quote"] for e in evidence
              if isinstance(e, dict) and isinstance(e.get("quote"), str)]
    noted = [q for q in quotes if q.startswith(NOTED_PREFIX)]
    if not any(q.startswith(DEFERRED_PREFIX) for q in quotes):
        return noted, None
    explanation = next((q[len(EXPLANATION_PREFIX):] for q in quotes
                        if q.startswith(EXPLANATION_PREFIX)), "no reviewer explanation recorded")
    return noted, explanation


def origin_reasons(origin):
    """Reasons a submission's declared origin gives (none when it declares nothing)."""
    generated = (origin or {}).get("agent_generated")
    if generated is None:
        return []
    if not isinstance(generated, dict) or not isinstance(generated.get("generator"), str):
        raise ValueError("agent_generated must name its generator")
    detail = "Generated by " + generated["generator"]
    if generated.get("generated_on"):
        detail += " on " + generated["generated_on"]
    repository = generated.get("source_repository")
    if repository:
        # e.g. "from falcon-docs@<commit>:ACTIVE/notes.md"; no commit when the file was uncommitted.
        detail += " from " + repository
        if generated.get("source_commit"):
            detail += "@" + generated["source_commit"]
        if origin.get("locator"):
            detail += ":" + origin["locator"]
    elif generated.get("source_commit"):
        detail += " from commit " + generated["source_commit"]
    return [reason("agent-generated", [detail])]


def origin_label(current):
    """``agent-generated (<detail>)`` for a label with that reason, else ``None``: the search-hit
    marker that keeps agent-written context from being mistaken for a primary source."""
    found = next((item for item in current["reasons"] if item["code"] == "agent-generated"), None)
    return None if found is None else "agent-generated (" + "; ".join(found["details"]) + ")"


def declared_origin(root, records, original, read, engine):
    """The ``origin`` a receipt's upload submission sidecar declares, or ``None`` without one.

    The sidecar is the ``preserved-metadata`` child of the original whose SHA-256 the catalog pins;
    one that is present in the catalog but missing, changed or malformed fails the build.
    """
    meta = [record for record in records
            if record["kind"] == "artifact" and record["data"].get("role") == "preserved-metadata"
            and record["data"].get("parent_id") == original["id"]
            and str(record["data"].get("path", "")).endswith("/submission.json")]
    if not meta:
        return None
    if len(meta) != 1:
        engine.fail("Ambiguous submission records for " + original["id"])
    data = meta[0]["data"]
    try:
        with read(root / engine.relative_name(data["path"])).open("rb") as stream:
            raw = stream.read(SIDECAR_LIMIT + 1)
        if len(raw) > SIDECAR_LIMIT or hashlib.sha256(raw).hexdigest() != data["sha256"]:
            raise ValueError("changed")
        origin = json.loads(raw.decode("utf-8"))["origin"]
        if not isinstance(origin, dict):
            raise ValueError("origin")
        origin_reasons(origin)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        engine.fail(f"Submission record missing or changed: {data.get('path')}: {error}")
    return origin


def for_rendition(value, review, origin):
    """The label of one selected rendition at build time."""
    found = []
    kind = rendition_kind(value)
    if kind in RENDITION_REASONS:
        found.append(reason(RENDITION_REASONS[kind], [kind]))
    if review is not None:
        found.extend(review_reasons(review["reviewed_scope"]))
    found.extend(origin_reasons(origin))
    return label(found)


def legend(codes):
    """``{code: {explanation, correction}}`` for the codes present, in canonical order."""
    return {code: {"explanation": REASONS[code][0], "correction": REASONS[code][1]}
            for code in REASONS if code in codes}


def summary(labels):
    """The top-level confidence block of a read: worst level and the legend of its reasons."""
    labels = list(labels)
    codes = {item["code"] for current in labels for item in current["reasons"]}
    return {"contract": CONTRACT,
            "level": "low" if any(current["level"] == "low" for current in labels) else "normal",
            "reasons": legend(codes),
            "notice": "Low-confidence text is published and marked, not withheld: treat it as "
                      "shaky, cite its reasons, and prefer confirmation from another source."}


def codes(current):
    """The compact search form of a label: level and reason codes only."""
    return {"level": current["level"], "reasons": [item["code"] for item in current["reasons"]]}
