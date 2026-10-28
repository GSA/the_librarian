"""Versioned, fail-closed admission policies for selected renditions."""

POLICY_VERSION = "docling-exact-rendition-review-v1"


def docling(selection, value, identity, snapshot, engine):
    """Qualify one exact Docling rendition against configured active review events."""
    source = value.get("source", {})
    conversion = value.get("conversion", {})
    document = conversion.get("document", {}) if isinstance(conversion, dict) else {}
    tables = document.get("tables") if isinstance(document, dict) else None
    if (value.get("schema") != "docling-rendition-v1" or not isinstance(tables, list)
            or source.get("receipt_id") != selection["receipt_id"]
            or not isinstance(source.get("source_ids"), list)
            or not source["source_ids"] or not isinstance(source.get("path"), str)):
        engine.fail("Invalid Docling qualification identity")

    by_id = {event.get("event_id"): event for event in snapshot.events
             if isinstance(event, dict)}
    reviewed_content = set()
    reviewed_scope = []
    for event_id in selection["review_event_ids"]:
        event = by_id.get(event_id)
        if event is None or event_id in snapshot.successors:
            engine.fail("Configured Docling review event is missing or superseded")
        event_source = event.get("source", {})
        scope = event.get("scope", {})
        checksum = event_source.get("checksum", {})
        content = scope.get("content")
        if (event_source.get("receipt_id") != selection["receipt_id"]
                or event_source.get("source_id") not in source["source_ids"]
                or event_source.get("locator") != selection["path"]
                or event_source.get("parent_locator") != source["path"]
                or checksum != {"sha256": selection["sha256"], "basis": "reported"}
                or event.get("outcome") != "reviewed"
                or scope.get("unit") != "file" or scope.get("extent") != "defined_scope"
                or scope.get("coordinates") != "not_applicable" or scope.get("ranges") != []
                or not isinstance(content, list) or not content
                or not event.get("reviewer") or not event.get("reviewed_on")
                or not isinstance(event.get("evidence"), list) or not event["evidence"]
                or event.get("uncertainties") != []):
            engine.fail("Docling review does not bind the exact complete rendition")
        reviewed_content.update(content)
        reviewed_scope.append({"event_id": event_id, "content": sorted(content),
                               "unit": "file", "extent": "defined_scope"})
    if "text" not in reviewed_content:
        engine.fail("Docling text content lacks exact substantive review")
    if tables and "tables" not in reviewed_content:
        engine.fail("Docling table content lacks exact substantive review")
    return {"policy_version": POLICY_VERSION,
            "review_event_ids": list(selection["review_event_ids"]),
            "review_state": snapshot.state["state_token"],
            "reviewed_content": sorted(reviewed_content),
            "reviewed_scope": reviewed_scope}
