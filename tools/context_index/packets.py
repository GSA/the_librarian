"""Packet assembly and rendering: exact indexed text with its required context, never truncated."""

from collections import deque
import json
import posixpath
import re
from urllib.parse import unquote, urlsplit

from tools.context_index import confidence
from tools.context_index.context_index import (
    REFERENCE, canonical_locator, envelope, fail, get_section, visible_lines,
)


INLINE_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(?:<([^>]+)>|([^\s)]+))")
REFERENCE_LINK = re.compile(r"!?\[([^\]\n]+)\](?:\[([^\]\n]*)\])?")


def outbound_targets(text, definitions):
    visible = "".join(line if line else "\n" for line in visible_lines(text.splitlines(keepends=True)))
    targets = [match[2] or match[3] for match in REFERENCE.finditer(visible)]
    targets.extend(match[1] or match[2] for match in INLINE_LINK.finditer(visible))
    for match in REFERENCE_LINK.finditer(visible):
        key = " ".join((match[2] or match[1]).lower().split())
        if key in definitions:
            targets.append(definitions[key]["target"])
    return list(dict.fromkeys(targets))


def link_record(connection, path, target):
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return {"target": target, "status": "external-not-fetched"}
    if parsed.query:
        return {"target": target, "status": "outside-selection-not-fetched"}
    if parsed.path.startswith("/"):
        return {"target": target, "status": "outside-selection-not-fetched"}
    local = posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(parsed.path))) if parsed.path else path
    locator = local + ("#" + unquote(parsed.fragment) if parsed.fragment else "")
    exists = connection.execute("SELECT 1 FROM aliases WHERE locator=?", (locator,)).fetchone()
    return {
        "target": target, "locator": locator,
        "status": "indexed" if exists else "outside-selection-or-unindexed-anchor-not-fetched",
    }


def packet(connection, config, locator, max_chars, *, fail_fast=False, root=None):
    if max_chars < 1:
        fail("--max-chars must be a positive integer")
    requested = canonical_locator(locator, root)
    selected = get_section(connection, requested, root)
    docs = {row["path"]: row for row in connection.execute(
        "SELECT d.*,c.label AS confidence FROM documents d JOIN document_confidence c USING(path)")}
    rendition_metadata = dict(connection.execute("SELECT path,payload FROM rendition_documents"))
    pieces = {}
    assembled_chars = 0

    def add_piece(row, start, end, text, reason, source_locator=None):
        nonlocal assembled_chars
        if not text:
            return
        key = (row["path"], start, end, text)
        if key in pieces:
            if reason not in pieces[key]["reasons"]:
                pieces[key]["reasons"].append(reason)
            return
        assembled_chars += len(text)
        if fail_fast and assembled_chars > max_chars:
            fail(f"Individual packet exceeds the combined --max-chars budget of {max_chars}. "
                 "No required context has been truncated.")
        pieces[key] = _piece(connection, docs, rendition_metadata, row, start, end, text, reason,
                             source_locator)

    _add_context(connection, config, requested, docs, add_piece, root)
    # Definitions elsewhere in a document are required to interpret reference-style links.
    for piece in _markdown_pieces(list(pieces.values()), rendition_metadata):
        definitions = json.loads(docs[piece["path"]]["reference_definitions"])
        for match in REFERENCE_LINK.finditer(piece["text"]):
            definition = definitions.get(" ".join((match[2] or match[1]).lower().split()))
            if definition is not None:
                add_piece(get_section(connection, piece["path"], root), definition["line"],
                          definition["line"], definition["text"], "reference-link-definition")
    pieces = list(pieces.values())
    required = sum(len(piece["text"]) for piece in pieces)
    links = _outbound_links(connection, pieces, docs, rendition_metadata)
    if required > max_chars:
        fail(
            f"Complete packet requires {required} characters; --max-chars is {max_chars}. "
            f"Increase --max-chars to at least {required}, or select a narrower locator. "
            "No required context has been truncated."
        )
    result = envelope(
        root=root, command="get", requested_locator=requested, locator=selected["locator"],
        caveat="Exact source text with inherited/configured context, not a synthesized or verified answer.",
        characters=required, confidence=confidence.summary(piece["confidence"] for piece in pieces),
        pieces=pieces, outbound_links=links,
    )
    selected_renditions = {piece["path"] for piece in pieces} & rendition_metadata.keys()
    if selected_renditions:
        result["renditions"] = {path: json.loads(rendition_metadata[path])
                                for path in sorted(selected_renditions)}
    return result


def _piece(connection, docs, rendition_metadata, row, start, end, text, reason, source_locator):
    value = {
        "locator": source_locator or row["locator"], "path": row["path"],
        "title": row["title"], "role": docs[row["path"]]["role"],
        "source_hash": docs[row["path"]]["hash"], "line_start": start,
        "line_end": end, "text": text, "reasons": [reason],
        "confidence": json.loads(docs[row["path"]]["confidence"]),
    }
    if row["path"] in rendition_metadata:
        location = connection.execute("SELECT payload FROM rendition_locations WHERE locator=?",
                                      (row["locator"],)).fetchone()
        value["source_location"] = json.loads(location[0])
        if json.loads(rendition_metadata[row["path"]])["format"] == "docling":
            value["line_start"] = None
            value["line_end"] = None
    return value


def _add_context(connection, config, requested, docs, add_piece, root):
    """Add the requested section, then every section its inherited and configured context requires."""
    document_context = {doc["path"]: doc["context"]
                        for doc in [*config["documents"], *config.get("renditions", [])]}
    rules, rule_sections = {}, {}
    for key, values in config["context_rules"].items():
        section = get_section(connection, key, root)
        rules.setdefault(section["id"], []).extend(values)
        rule_sections[section["id"]] = section
    queue = deque([(requested, "selected-section")])
    seen = set()
    while queue:
        target, reason = queue.popleft()
        row = get_section(connection, target, root)
        add_piece(row, row["line_start"], row["line_end"], row["text"], reason)
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        inherited = _add_inherited(connection, row, docs, add_piece)
        companions = [(locator, "configured-document-context") for locator in document_context[row["path"]]]
        # A broad selection must retain corrections attached to its included subsections.
        companions += [(locator, "configured-contained-context")
                       for rule_id, section in rule_sections.items()
                       if rule_id != row["id"] and section["path"] == row["path"]
                       and row["line_start"] <= section["line_start"]
                       and section["line_end"] <= row["line_end"]
                       for locator in rules[rule_id]]
        companions += [(locator, "configured-locator-context")
                       for ancestor in inherited for locator in rules.get(ancestor["id"], [])]
        queue.extend((canonical_locator(locator, root), reason) for locator, reason in companions)


def _add_inherited(connection, row, docs, add_piece):
    """Add the document introduction, ancestor lead-ins and table headers; return row and ancestors."""
    for start, end, text in json.loads(docs[row["path"]]["introduction"]):
        add_piece(row, start + 1, end, text, "document-introduction", row["path"])
    inherited = [row]
    parent_id = row["parent_id"]
    while parent_id is not None:
        parent = connection.execute("SELECT * FROM sections WHERE id=?", (parent_id,)).fetchone()
        inherited.append(parent)
        add_piece(parent, parent["line_start"], parent["lead_end"], parent["lead_text"], "ancestor-lead-in")
        parent_id = parent["parent_id"]
    table = json.loads(row["table_header"])
    if table:
        add_piece(row, table[0] + 1, table[1], table[2], "table-column-headers")
    return inherited


def _markdown_pieces(pieces, rendition_metadata):
    return [piece for piece in pieces if piece["path"] not in rendition_metadata]


def _outbound_links(connection, pieces, docs, rendition_metadata):
    links = []
    for piece in _markdown_pieces(pieces, rendition_metadata):
        definitions = json.loads(docs[piece["path"]]["reference_definitions"])
        for target in outbound_targets(piece["text"], definitions):
            record = {"source": piece["locator"], **link_record(connection, piece["path"], target)}
            if record not in links:
                links.append(record)
    return links


# Per-document fields compact and get-many formats share once per document instead of per piece.
DOCUMENT_KEYS = ("path", "role", "source_hash", "confidence")


def compact_packet(result):
    documents = {}
    references = {}
    pieces = []
    for piece in result["pieces"]:
        path = piece["path"]
        if path not in references:
            reference = f"d{len(documents) + 1}"
            references[path] = reference
            documents[reference] = {
                key: piece[key] for key in DOCUMENT_KEYS
            }
        pieces.append({
            **{key: value for key, value in piece.items() if key not in DOCUMENT_KEYS},
            "document_ref": references[path],
        })
    return {
        **{key: value for key, value in result.items()
           if key not in ("pieces", "outbound_links")},
        "format": "qualified-compact-v2",
        "documents": documents,
        "pieces": pieces,
        "navigation": {
            "included": False,
            "omitted_links": len(result["outbound_links"]),
            "notice": "Only the navigation inventory is omitted; source links and required context remain. "
                      "Document references are local to this response. A later full read may reflect changed "
                      "sources; compare source hashes before combining reads.",
            "full_request": {
                "command": "get", "locator": result["requested_locator"],
                "format": "full", "max_chars": result["characters"],
            },
        },
    }


def validate_many_limits(locators, max_chars, max_output_chars=None):
    if not 1 <= len(locators) <= 10:
        fail("get-many requires between 1 and 10 locators")
    if max_chars < 1:
        fail("--max-chars must be a positive integer")
    if max_output_chars is not None and max_output_chars < 1:
        fail("--max-output-chars must be a positive integer")


def packet_many(connection, config, locators, max_chars=30000, *, format_="compact", root=None):
    """Pool exact spans without merging the per-request qualification relationships."""
    validate_many_limits(locators, max_chars)
    if format_ not in ("compact", "full"):
        fail("get-many format must be compact or full")
    documents, document_refs = {}, {}
    spans, span_refs = {}, {}
    requests = []
    characters = 0
    for locator in locators:
        original = packet(connection, config, locator, max_chars, fail_fast=True, root=root)
        projected = compact_packet(original) if format_ == "compact" else original
        pieces = []
        for piece in original["pieces"]:
            document_key = (piece["path"], piece["source_hash"], piece["role"])
            if document_key not in document_refs:
                reference = f"d{len(documents) + 1}"
                document_refs[document_key] = reference
                documents[reference] = {key: piece[key] for key in DOCUMENT_KEYS}
            span_key = tuple(piece[key] for key in
                             ("source_hash", "path", "line_start", "line_end", "text"))
            if span_key not in span_refs:
                characters += len(piece["text"])
                if characters > max_chars:
                    fail(f"Combined unique source text exceeds --max-chars of {max_chars}; "
                         f"requires at least {characters} characters. "
                         "No required context has been truncated.")
                reference = f"s{len(spans) + 1}"
                span_refs[span_key] = reference
                spans[reference] = {
                    "document_ref": document_refs[document_key],
                    **{key: piece[key] for key in ("line_start", "line_end", "text")},
                }
            pieces.append({
                **{key: value for key, value in piece.items()
                   if key not in (*DOCUMENT_KEYS, "line_start", "line_end", "text")},
                "span_ref": span_refs[span_key],
            })
        requests.append({
            "request_ref": f"r{len(requests) + 1}",
            "input_locator": locator,
            "packet": {
                **{key: value for key, value in projected.items()
                   if key not in ("audience", "notice", "documents", "pieces", "confidence")},
                "pieces": pieces,
            },
        })
    return envelope(
        root=root, command="get-many", format=f"qualified-many-{format_}-v2",
        characters=characters, document_count=len(documents), span_count=len(spans),
        confidence=confidence.summary(document["confidence"] for document in documents.values()),
        documents=documents, spans=spans, requests=requests,
        reference_notice="All references are local to this response. Each request's ordered pieces "
                         "retain its qualification relationships; resolve span_ref through spans and "
                         "document_ref through documents. Shared audience and notice apply to every "
                         "request, as does the confidence legend; each document carries its own "
                         "confidence label. Identical text in different sources is not corroboration. "
                         "Later reads may reflect changed sources; compare source hashes.",
    )


def render_result(result, *, compact=False, max_output_chars=None):
    output = json.dumps(result, ensure_ascii=False, separators=(",", ":") if compact else None) + "\n"
    if max_output_chars is not None:
        if max_output_chars < 1:
            fail("--max-output-chars must be a positive integer")
        if len(output) > max_output_chars:
            fail(f"Complete serialized output requires {len(output)} characters; "
                 f"--max-output-chars is {max_output_chars}. Increase the budget or select a narrower "
                 "locator. No required context has been truncated.")
    return output
