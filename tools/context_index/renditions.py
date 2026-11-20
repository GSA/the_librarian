"""Explicitly selected, registered extraction records; never a raw-intake crawl."""

import hashlib
import json
import math
import os
import re
import stat
from bisect import bisect_right
from pathlib import Path, PurePosixPath

from tools.context_index import confidence, qualification


# One instance publishes a reviewed cohort (tens of thousands of files), not a hand-picked handful.
# Sized for a 36,000-document cohort at the observed acceptance rate (about 13,000 selections) with
# headroom; publish cost and manifest rewrite cost at this size are in docs/design/current-state.md.
MAX_SELECTIONS = 50000
DOCLING_MAX_PAGES = 100
DOCLING_MAX_RECORDS = 10000
DOCLING_RENDER_SCALE = 3.0
DOCLING_MAX_PAGE_PIXELS = 32_000_000


def _eligible_pattern(root=None):
    from tools import policy
    prefix = re.escape(policy.storage_roots(root)["extractions_home"])
    return re.compile(prefix + r"/D\d+/[a-f0-9]{64}/extraction\.json")


def eligible(path, root=None):
    return isinstance(path, str) and bool(_eligible_pattern(root).fullmatch(path))


def validate(config, engine, root=None):
    selections = config.get("renditions", [])
    if not isinstance(selections, list) or len(selections) > MAX_SELECTIONS:
        engine.fail(f"renditions must be a list of at most {MAX_SELECTIONS} explicit selections")
    seen, valid_contexts = set(), set()
    # Read the storage policy once per manifest, not once per selection.
    pattern = _eligible_pattern(root) if selections else None
    for item in selections:
        validate_item(item, engine, pattern, seen, valid_contexts)


FORMATS = frozenset({"gemini-txt", "text", "mhtml", "odt", "docling", "pdf-text", "ocr", "converted",
                     "pdf-pages"})
# Page-located line renditions of PDFs and images (tools/extraction/pdf_image.py, pdf_pages.py).
PAGE_LINE_FORMATS = frozenset({"pdf-text", "ocr", "pdf-pages"})


def validate_item(item, engine, pattern, seen, valid_contexts):
    """Check one selection and add its path to ``seen``; ``pattern`` is ``_eligible_pattern(root)``.

    ``seen`` holds the paths already accepted (a duplicate is refused) and ``valid_contexts`` the
    context lists already proven, so a caller adding one selection to a validated manifest can
    check just that selection against the state it kept.
    """
    engine.fields(item, ("path", "sha256", "receipt_id", "format", "context"),
                  "Rendition selection", optional=("review_event_ids",))
    if not _valid_selection(item, pattern, seen):
        engine.fail("Invalid or duplicate rendition selection")
    review_ids = item.get("review_event_ids")
    if (item["format"] == "docling" or review_ids is not None) and not _unique_review_ids(review_ids):
        engine.fail("Docling selection requires unique review_event_ids" if item["format"] == "docling"
                    else "Reviewed extraction selection requires unique review_event_ids")
    if not item["context"]:
        engine.fail("Rendition selection requires maintained source context")
    _validate_context(item["context"], engine, valid_contexts)
    seen.add(item["path"])


def _valid_selection(item, pattern, seen):
    path = item["path"]
    return (isinstance(path, str) and bool(pattern.fullmatch(path)) and path not in seen
            and PurePosixPath(path).parts[-3] == item["receipt_id"]
            and isinstance(item["format"], str) and item["format"] in FORMATS
            and isinstance(item["sha256"], str) and bool(re.fullmatch(r"[a-f0-9]{64}", item["sha256"])))


def _unique_review_ids(review_ids):
    return (isinstance(review_ids, list) and bool(review_ids)
            and all(isinstance(event_id, str) and re.fullmatch(r"rev-[a-z0-9-]{1,100}", event_id)
                    for event_id in review_ids)
            and len(review_ids) == len(set(review_ids)))


def _validate_context(context, engine, valid_contexts):
    key = (tuple(context) if isinstance(context, list)
           and all(type(locator) is str for locator in context) else None)
    if key is not None and key in valid_contexts:
        # Identical context lists get identical answers within one manifest check.
        return
    engine.validate_context(context)
    if any(not engine.eligible(engine.canonical_locator(locator).partition("#")[0])
           for locator in context):
        engine.fail("Rendition context must select maintained Markdown")
    if key is not None:
        valid_contexts.add(key)


def _checker(root, engine):
    """A ``no_symlinks`` that looks only at the components below ``root``.

    The instance root's own ancestors are checked once per call, not once per selected file; every
    component below it (the extraction directories and the file) is checked each time. A path that
    is not below the root takes the full check.
    """
    top = str(engine.no_symlinks(root))
    prefix = top.rstrip(os.sep) + os.sep

    def check(path):
        name = os.path.abspath(path)
        if not name.startswith(prefix) or ".." in Path(path).parts:
            return engine.no_symlinks(path)
        current = top
        for part in name[len(prefix):].split(os.sep):
            current = current + os.sep + part
            try:
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    return engine.no_symlinks(path)
            except OSError:
                break  # nothing at or below a missing component can be a symlink; the read fails next
        return Path(name)
    return check


SOURCE_FIELDS = ("receipt_id", "artifact_id", "source_ids", "path", "sha256", "bytes", "audience",
                 "handling_refs")
PAYLOAD_FIELDS = {
    "gemini-txt": ("format", "scope", "warnings", "lines", "precedence"),
    "text": ("format", "scope", "warnings", "lines", "bom_bytes", "ends_with_terminator"),
    "mhtml": ("format", "scope", "warnings", "lines", "method", "parts"),
    "pdf-text": ("format", "scope", "warnings", "lines", "source_format", "method", "pages"),
    "ocr": ("format", "scope", "warnings", "lines", "source_format", "method", "pages"),
    "converted": ("format", "scope", "warnings", "lines", "conversion"),
    "pdf-pages": ("format", "scope", "warnings", "lines", "source_format", "method", "pages", "page_count",
                  "unread_pages"),
}
ODT_PAYLOAD_FIELDS = ("format", "scope", "warnings", "nodes", "xml_sha256", "members", "unrendered_features")
SELECTION_NOTES = {
    "gemini-txt": "Transcript-classified records only; generated notes and footer not indexed.",
    "text": "All byte-mapped physical text lines explicitly selected; decoding is not review.",
    "mhtml": "All converted plain-text lines of the decoded MIME HTML parts explicitly selected; "
             "conversion is not review.",
    "docling": "Strictly qualified selected Docling structural records.",
    "pdf-text": "All non-blank lines of the PDF's embedded text layer, by page; tables and layout are not kept.",
    "ocr": "All machine OCR/layout lines, by page; characters and reading order may be misread.",
    "converted": "All Markdown lines machine-converted from an office/RTF original explicitly selected; "
                 "conversion is not review.",
    "pdf-pages": "All non-blank lines of every page (text layer, else machine OCR of that page), by page; "
                 "a page that could not be read is a placeholder line.",
}
ODT_SELECTION_NOTE = "Located XML records only; text rendition, not a rendered or approved view."
DOCLING_CITATION = ("Docling packet line ranges are null; source_location gives the exact retained-extraction "
                    "byte range, JSON pointer, self_ref and page/bounding-box provenance.")
EXTRACTION_CITATION = ("Packet line ranges cite extraction JSON, not original lines or pages. "
                       "Resolve source_location for the original line, byte range or XML member/element. "
                       "Extraction and retrieval are not new review, corroboration or acceptance.")
PAYLOAD_METADATA = ("precedence", "xml_sha256", "unrendered_features", "bom_bytes", "ends_with_terminator",
                    "method", "parts", "source_format", "conversion", "page_count", "unread_pages")


def inputs(root, config, engine, review_snapshot=None, *, reads=None, keep=True):
    """Pin selected catalog records, original bytes and retained extraction bytes.

    ``reads``, a set, receives every selected extraction, original and sidecar path read (the stores
    are the caller's to name). Without ``keep`` only the identity is returned, so a freshness check
    holds one extraction at a time instead of every payload.
    """
    selections = config.get("renditions", [])
    if not selections:
        return [], ""
    checked = _checker(root, engine)

    def no_symlinks(path):
        path = checked(path)
        if reads is not None:
            reads.add(path)
        return path
    catalog = engine.load_catalog()
    if (any(item["format"] == "docling" or "review_event_ids" in item for item in selections)
            and review_snapshot is None):
        review_snapshot = engine.load_event_reader().read_snapshot(root, full=True)
    snapshot = catalog.receipt_metadata(root, [item["receipt_id"] for item in selections])
    state = snapshot["state"]["state_token"]
    # One validated catalog snapshot for every selected receipt, not one per rendition.
    receipt_records = catalog.read_records_by_receipt(
        root, [item["receipt_id"] for item in selections], limit=100, expected_state=state)
    result, identities = [], []
    for item in selections:
        registration = snapshot["registrations"][item["receipt_id"]]
        original = registration["artifact"]
        if not engine.original_allowed(original["data"]["path"]):
            engine.fail("Original source excluded by current policy")
        raw = catalog.private_file(no_symlinks(root / item["path"])).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            engine.fail("Selected rendition hash changed: " + item["path"])
        value = _load_extraction(item, raw, engine)
        run = PurePosixPath(item["path"]).parts[-2]
        child, relation = _registered_rendition(
            item, value, raw, registration, receipt_records[item["receipt_id"]], run, engine)
        source = value["source"]
        original_raw = _read_original(root, source, no_symlinks, engine)
        if item["format"] == "text":
            validate_text_mapping(value["payload"], original_raw, engine)
        elif item["format"] == "mhtml":
            validate_mhtml(value["payload"], original_raw, engine)
        identity = {"original": original, "rendition": child, "relationship": relation,
                    "source": source, "rendition_sha256": item["sha256"]}
        review = None
        if item["format"] == "docling":
            review = qualification.docling(item, value, identity, review_snapshot, engine)
            validate_docling_conversion(value, engine)
        elif "review_event_ids" in item:
            review = qualification.extract(item, value, identity, review_snapshot, engine)
        if review is not None:
            identity["qualification"] = review
        origin = confidence.declared_origin(
            root, receipt_records[item["receipt_id"]]["records"], original,
            lambda path: catalog.private_file(no_symlinks(path)), engine)
        label = confidence.for_rendition(value, review, origin)
        identity["confidence"] = label
        if keep:
            result.append((item, raw, value, _metadata(item, value, run, review), label))
        identities.append(identity)
    return result, engine.digest(json.dumps(identities, sort_keys=True).encode("utf-8"))


def _load_extraction(item, raw, engine):
    """Decode a retained extraction and check its envelope for the selected format."""
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=engine.strict_object)
    if item["format"] == "docling":
        engine.fields(value, ("schema", "run_id", "extractor", "source", "boundary", "conversion"),
                      "Docling extraction")
        engine.fields(value["source"], SOURCE_FIELDS, "Extraction source")
        if value.get("schema") != "docling-rendition-v1":
            engine.fail("Unsupported Docling extraction schema")
        return value
    engine.fields(value, ("schema_version", "run_id", "extractor", "source", "boundary", "payload"),
                  "Extraction")
    engine.fields(value["extractor"], ("version", "sha256"), "Extractor identity")
    engine.fields(value["source"], SOURCE_FIELDS, "Extraction source")
    payload = value["payload"]
    engine.fields(payload, PAYLOAD_FIELDS.get(item["format"], ODT_PAYLOAD_FIELDS), "Extraction payload")
    if (type(value["schema_version"]) is not int
            or not isinstance(payload["warnings"], list)
            or not all(isinstance(warning, str) for warning in payload["warnings"])):
        engine.fail("Unsupported extraction schema or warnings")
    return value


def _registered_rendition(item, value, raw, registration, records, run, engine):
    """Return the catalog rendition record and relation that register exactly these bytes."""
    if records["next_after"] is not None:
        engine.fail("Selected rendition receipt exceeds the bounded catalog lookup")
    by_id = {record["id"]: record for record in records["records"]}
    prefix = "docling" if item["format"] == "docling" else "extract"
    child = by_id.get("A-" + prefix + "-" + run, {})
    relation = by_id.get("R-" + prefix + "-" + run, {})
    original = registration["artifact"]
    data = original["data"]
    source = value["source"]
    expected_source = {
        "receipt_id": item["receipt_id"], "artifact_id": original["id"],
        "source_ids": registration["receipt"]["data"]["source_ids"],
        **{key: data[key] for key in ("path", "sha256", "bytes", "audience", "handling_refs")},
    }
    if item["format"] == "docling":
        schema_matches = value.get("schema") == "docling-rendition-v1"
        format_matches = True
    else:
        schema_matches = value.get("schema_version") == 1
        format_matches = value["payload"]["format"] == item["format"]
    child_data = child.get("data", {})
    expected_child = {
        "path": item["path"], "sha256": item["sha256"], "bytes": len(raw), "parent_id": original["id"],
        "role": "generated-rendition", "presence": "present", "coverage": "indexed",
        "handling_refs": source["handling_refs"], "audience": source["audience"],
    }
    relation_data = relation.get("data", {})
    expected_relation = {"from_id": child.get("id"), "to_id": original["id"], "relation": "rendition-of"}
    if (not schema_matches or value["run_id"] != run
            or not format_matches or source != expected_source
            or data["role"] != "original" or data["presence"] != "present"
            or data["coverage"] == "excluded"
            or any(child_data.get(key) != expected for key, expected in expected_child.items())
            or any(relation_data.get(key) != expected for key, expected in expected_relation.items())):
        engine.fail("Rendition/source registration mismatch: " + item["path"])
    return child, relation


def _read_original(root, source, no_symlinks, engine):
    original_path = no_symlinks(root / engine.relative_name(source["path"]))
    if not original_path.is_file() or original_path.stat().st_size != source["bytes"]:
        engine.fail("Original source missing or changed: " + source["path"])
    with original_path.open("rb") as stream:
        original_raw = stream.read()
    if hashlib.sha256(original_raw).hexdigest() != source["sha256"]:
        engine.fail("Original source missing or changed: " + source["path"])
    return original_raw


def _metadata(item, value, run, review):
    payload = value.get("payload")
    docling = item["format"] == "docling"
    metadata = {
        "source": value["source"], "rendition_path": item["path"], "rendition_sha256": item["sha256"],
        "extractor": value["extractor"], "run_id": run,
        "extraction_boundary": value["boundary"],
        "format": item["format"],
        "extraction_scope": payload["scope"] if payload is not None else "Docling conversion envelope",
        "warnings": payload["warnings"] if payload is not None else value["conversion"]["warnings"],
        "selection": SELECTION_NOTES.get(item["format"], ODT_SELECTION_NOTE),
        "citation_contract": DOCLING_CITATION if docling else EXTRACTION_CITATION,
    }
    for key in PAYLOAD_METADATA:
        if payload is not None and key in payload:
            metadata[key] = payload[key]
    if review is not None:
        metadata["qualification"] = review
    if docling:
        metadata["confidence"] = value["conversion"]["confidence"]
        metadata["omissions"] = value["conversion"]["omissions"]
    return metadata


def validate_mhtml(payload, source_raw, engine):
    """The stored conversion must equal a fresh strict conversion of the exact original bytes."""
    from tools.extraction import mhtml
    try:
        mhtml.verify(payload, source_raw)
    except mhtml.MhtmlError:
        engine.fail("MIME HTML rendition does not match a fresh conversion of the original")


def validate_text_mapping(payload, source_raw, engine):
    if type(payload["bom_bytes"]) is not int or payload["bom_bytes"] not in {0, 3}:
        engine.fail("Invalid text BOM declaration")
    if (payload["bom_bytes"] == 3) != source_raw.startswith(b"\xef\xbb\xbf"):
        engine.fail("Text BOM declaration does not match original bytes")
    if any(not isinstance(record, dict) or not isinstance(record.get("text"), str)
           or type(record.get("line")) is not int or type(record.get("terminator")) is not str
           or type(record.get("byte_start")) is not int or type(record.get("byte_end")) is not int
           for record in payload["lines"]):
        engine.fail("Invalid text line field types")
    # Exact equality with the canonical split proves field sets, numbering, terminators and that
    # every byte after the BOM is covered by exactly one line slice.
    if payload["lines"] != _canonical_lines(source_raw, payload["bom_bytes"]):
        engine.fail("Invalid text physical line mapping")
    expected_end = bool(payload["lines"] and payload["lines"][-1]["terminator"] != "none")
    if type(payload["ends_with_terminator"]) is not bool or payload["ends_with_terminator"] != expected_end:
        engine.fail("Invalid text byte mapping or final terminator state")


def _canonical_lines(source_raw, offset):
    """Split decoded bytes after ``offset`` into numbered physical lines with exact byte ranges."""
    lines = []
    for number, match in enumerate(re.finditer(b".*?(?:\\r\\n|\\r|\\n|\\Z)", source_raw[offset:]), 1):
        raw_line = match.group()
        if not raw_line:
            continue
        text = raw_line.decode("utf-8")
        terminator = ("crlf" if text.endswith("\r\n") else "lf" if text.endswith("\n")
                      else "cr" if text.endswith("\r") else "none")
        lines.append({"line": number, "text": text, "terminator": terminator,
                      "byte_start": offset, "byte_end": offset + len(raw_line)})
        offset += len(raw_line)
    return lines


def finite_number(value):
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_docling_conversion(value, engine):
    conversion = value.get("conversion")
    engine.fields(conversion, ("schema", "format", "document", "pages", "warnings",
                               "confidence", "omissions"), "Docling conversion")
    if conversion["schema"] != "docling-conversion-v1" or conversion["format"] not in {"pdf", "image"}:
        engine.fail("Unsupported Docling conversion schema or format")
    if (not isinstance(conversion["warnings"], list)
            or not all(isinstance(warning, dict) for warning in conversion["warnings"])):
        engine.fail("Invalid Docling conversion warnings")
    if not isinstance(conversion["confidence"], dict):
        engine.fail("Invalid Docling conversion confidence")
    if (not isinstance(conversion["omissions"], list)
            or not all(isinstance(omission, str) and omission for omission in conversion["omissions"])):
        engine.fail("Invalid Docling conversion omissions")
    return conversion


def docling_record_ranges(raw, name, expected, engine):
    """Locate canonical array objects in one forward scan of retained bytes."""
    marker = ('"' + name + '":[').encode()
    if raw.count(marker) != 1:
        engine.fail("Docling record collection is missing or ambiguous")
    position = raw.find(marker) + len(marker)
    ranges = []
    for item in expected:
        if position >= len(raw) or raw[position] != ord("{"):
            engine.fail("Docling record/range mismatch")
        start = position
        position = _object_end(raw, start)
        exact = json.dumps(item, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
        if position is None or raw[start:position] != exact:
            engine.fail("Docling record/range mismatch")
        ranges.append((start, position))
        if position < len(raw) and raw[position] == ord(","):
            position += 1
    if position >= len(raw) or raw[position] != ord("]"):
        engine.fail("Docling record collection/range mismatch")
    return ranges


def _object_end(raw, start):
    """Return the offset just past the JSON object opening at ``start``, or None if unclosed."""
    depth = 0
    quoted = escaped = False
    for position in range(start, len(raw)):
        byte = raw[position]
        if escaped:
            escaped = False
        elif quoted:
            escaped = byte == ord("\\")
            quoted = byte != ord('"')
        elif byte == ord('"'):
            quoted = True
        elif byte == ord("{"):
            depth += 1
        elif byte == ord("}"):
            depth -= 1
            if depth == 0:
                return position + 1
    return None


def docling_records(raw, value, engine):
    conversion = validate_docling_conversion(value, engine)
    document = conversion["document"]
    engine.fields(document, ("texts", "tables"), "Docling document", optional=(
        "schema_name", "version", "name", "body", "pages", "pictures", "key_value_items",
        "form_items", "furniture", "groups"))
    unsupported = ("pictures", "key_value_items", "form_items", "furniture", "groups")
    if any(document.get(key) not in (None, [], {}) for key in unsupported):
        engine.fail("Docling document contains unsupported content collections")
    pages = _docling_pages(conversion["pages"], engine)
    collections = {"text": document["texts"], "table": document["tables"]}
    if (any(not isinstance(items, list) for items in collections.values())
            or sum(len(items) for items in collections.values()) > DOCLING_MAX_RECORDS):
        engine.fail("Invalid or excessive Docling record count")
    ranges = {kind: docling_record_ranges(raw, kind + "s", items, engine)
              for kind, items in collections.items()}
    ordinal = 0
    for kind, items in collections.items():
        for index, item in enumerate(items):
            expected_ref = f"#/{kind}s/{index}"
            if (not isinstance(item, dict) or item.get("self_ref") != expected_ref
                    or ("label" in item and not isinstance(item["label"], str))
                    or not isinstance(item.get("prov"), list) or not item["prov"]):
                engine.fail("Invalid Docling record identity or provenance")
            locations = [_docling_location(provenance, pages, engine) for provenance in item["prov"]]
            search_text = _docling_text(item, engine) if kind == "text" else _docling_table_text(item, engine)
            byte_start, byte_end = ranges[kind][index]
            ordinal += 1
            yield {"anchor": kind + "-" + str(index + 1),
                   "title": (item.get("label") or kind).replace("_", " ").title(),
                   "start": ordinal, "end": ordinal, "text": raw[byte_start:byte_end].decode("utf-8"),
                   "search_text": search_text, "parent": None,
                   "source_location": {"json_pointer": f"/conversion/document/{kind}s/{index}",
                                       "byte_start": byte_start, "byte_end": byte_end,
                                       "self_ref": expected_ref, "page_no": locations[0]["page_no"],
                                       "bbox": locations[0]["bbox"], "provenance": locations}}


def _docling_pages(pages, engine):
    """Return the bounded, canonically numbered page inventory keyed by page number."""
    if (not isinstance(pages, list) or not pages or len(pages) > DOCLING_MAX_PAGES
            or any(not isinstance(page, dict) or type(page.get("page_no")) is not int
                   or any(not finite_number(page.get(key)) or page[key] <= 0
                          for key in ("width", "height"))
                   for page in pages)):
        engine.fail("Invalid Docling page inventory")
    for page in pages:
        scaled_width = page["width"] * DOCLING_RENDER_SCALE
        scaled_height = page["height"] * DOCLING_RENDER_SCALE
        if (not finite_number(scaled_width) or not finite_number(scaled_height)
                or math.ceil(scaled_width) * math.ceil(scaled_height) > DOCLING_MAX_PAGE_PIXELS):
            engine.fail("Excessive Docling page geometry")
    if {page["page_no"] for page in pages} != set(range(1, len(pages) + 1)):
        engine.fail("Noncanonical Docling page inventory")
    return {page["page_no"]: page for page in pages}


def _docling_location(provenance, pages, engine):
    """Return one provenance entry's page and in-page bounding box."""
    bbox = provenance.get("bbox", {}) if isinstance(provenance, dict) else {}
    page_no = provenance.get("page_no") if isinstance(provenance, dict) else None
    page = pages.get(page_no) if type(page_no) is int else None
    if (page is None or not isinstance(bbox, dict)
            or set(bbox) != {"l", "t", "r", "b", "coord_origin"}
            or bbox["coord_origin"] not in {"TOPLEFT", "BOTTOMLEFT"}
            or any(not finite_number(bbox[key]) for key in ("l", "t", "r", "b"))
            or not 0 <= bbox["l"] <= bbox["r"] <= page["width"]
            or not 0 <= min(bbox["t"], bbox["b"]) <= max(bbox["t"], bbox["b"]) <= page["height"]):
        engine.fail("Invalid Docling page provenance")
    return {"page_no": provenance["page_no"], "bbox": bbox}


def _docling_text(item, engine):
    if not isinstance(item.get("text"), str) or not item["text"]:
        engine.fail("Invalid Docling text record")
    return item["text"]


def _docling_table_text(item, engine):
    data = item.get("data", {})
    cells = data.get("table_cells") if isinstance(data, dict) else None
    if (type(data.get("num_rows")) is not int or data["num_rows"] <= 0
            or type(data.get("num_cols")) is not int or data["num_cols"] <= 0
            or not isinstance(cells, list)):
        engine.fail("Invalid Docling table structure")
    required = {"text", "start_row_offset_idx", "start_col_offset_idx",
                "end_row_offset_idx", "end_col_offset_idx"}
    if any(not isinstance(cell, dict) or not required <= set(cell)
           or not isinstance(cell["text"], str)
           or any(type(cell[key]) is not int for key in required - {"text"})
           or not (0 <= cell["start_row_offset_idx"] < cell["end_row_offset_idx"] <= data["num_rows"])
           or not (0 <= cell["start_col_offset_idx"] < cell["end_col_offset_idx"] <= data["num_cols"])
           for cell in cells):
        engine.fail("Invalid Docling table cells")
    return " ".join(cell["text"] for cell in cells)


def records(raw, value, engine):
    """Return exact on-disk JSON line spans alongside decoded search-only text."""
    if value.get("schema") == "docling-rendition-v1":
        yield from docling_records(raw, value, engine)
        return
    text = raw.decode("utf-8")
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    line_starts, position = [], 0
    for line in lines:
        line_starts.append(position)
        position += len(line)
    payload = value["payload"]
    # Full text/source-byte comparison was performed by inputs() before this
    # record generator receives a selected rendition.
    key = "lines" if payload["format"] in {"gemini-txt", "text", "mhtml", *PAGE_LINE_FORMATS, "converted"} else "nodes"
    if not payload[key]:
        return
    match = re.search(r'^\s*"' + key + r'": \[\s*$', text, re.M)
    if match is None:
        engine.fail("Unsupported extraction serialization: missing record array")
    offset = match.end()
    decoder = json.JSONDecoder(object_pairs_hook=engine.strict_object)
    # Transcript lines inherit the latest timestamp; ODT nodes inherit their XML parent.
    parents = {}
    for index, record in enumerate(payload[key]):
        _check_record(payload["format"], record, index, engine)
        while offset < len(text) and text[offset] in " \t\r\n,":
            offset += 1
        decoded, end = decoder.raw_decode(text, offset)
        if decoded != record:
            engine.fail("Extraction record/range mismatch")
        start_line = bisect_right(line_starts, offset)
        end_line = bisect_right(line_starts, end - 1)
        offset = end
        if payload["format"] == "gemini-txt" and record["section"] != "transcript":
            continue
        anchor, title, search_text, parent, location = _describe_record(
            payload["format"], record, index, parents)
        yield {
            "anchor": anchor, "title": title, "start": start_line, "end": end_line,
            "text": "".join(lines[start_line - 1:end_line]), "search_text": search_text,
            "parent": parent, "source_location": location,
        }


def _check_record(format_, record, index, engine):
    if format_ == "gemini-txt":
        engine.fields(record, ("line", "text", "section"), "Transcript line",
                      optional=("speaker_label_as_written", "timestamp_label"))
        if (type(record["line"]) is not int or record["line"] != index + 1
                or record["section"] not in {
                    "unclassified", "generated-notes", "transcript", "transcript-footer"}):
            engine.fail("Invalid transcript line numbering/classification")
    elif format_ == "mhtml":
        engine.fields(record, ("line", "text", "part"), "MIME HTML line")
    elif format_ in PAGE_LINE_FORMATS:
        engine.fields(record, ("line", "page", "text"), "Page line")
        if (type(record["line"]) is not int or record["line"] != index + 1
                or type(record["page"]) is not int or record["page"] < 1):
            engine.fail("Invalid page line numbering")
    elif format_ == "converted":
        engine.fields(record, ("line", "text"), "Converted line")
        if type(record["line"]) is not int or record["line"] != index + 1 or not isinstance(record["text"], str):
            engine.fail("Invalid converted line numbering")
    elif format_ != "text":
        engine.fields(record, ("member", "locator", "tag", "attributes", "text", "tail"),
                      "ODT node", optional=("paragraph_text",))


def _describe_record(format_, record, index, parents):
    """Return anchor, title, search text, parent anchor and source location for one record."""
    if format_ == "gemini-txt":
        anchor = "line-" + str(record["line"])
        parent = None
        if "timestamp_label" in record:
            parents["timestamp"] = anchor
        else:
            parent = parents.get("timestamp")
        title = record.get("speaker_label_as_written", record.get("timestamp_label", "Transcript"))
        return (anchor, title, record["text"], parent,
                {"json_pointer": f"/payload/lines/{index}", "original_line": record["line"],
                 "section": record["section"]})
    if format_ == "mhtml":
        return ("line-" + str(record["line"]), "Line " + str(record["line"]), record["text"], None,
                {"json_pointer": f"/payload/lines/{index}", "converted_line": record["line"],
                 "html_part": record["part"]})
    if format_ in PAGE_LINE_FORMATS:
        return ("line-" + str(record["line"]), f"Page {record['page']} line {record['line']}", record["text"],
                None, {"json_pointer": f"/payload/lines/{index}", "converted_line": record["line"],
                       "page": record["page"]})
    if format_ == "converted":
        return ("line-" + str(record["line"]), "Line " + str(record["line"]), record["text"], None,
                {"json_pointer": f"/payload/lines/{index}", "converted_line": record["line"]})
    if format_ == "text":
        return ("line-" + str(record["line"]), "Line " + str(record["line"]), record["text"], None,
                {"json_pointer": f"/payload/lines/{index}", "original_line": record["line"],
                 "byte_start": record["byte_start"], "byte_end": record["byte_end"],
                 "terminator": record["terminator"]})
    anchor = "node-" + str(index + 1)
    parent_path = re.sub(r"/\{[^}]*\}[^/]+\[\d+\]$", "", record["locator"])
    parent = parents.get((record["member"], parent_path))
    parents[(record["member"], record["locator"])] = anchor
    return (anchor, record["tag"].split("}")[-1], record.get("paragraph_text", record["text"]) + record["tail"],
            parent, {"json_pointer": f"/payload/nodes/{index}", "member": record["member"],
                     "xml_locator": record["locator"]})


def store(connection, selected, engine):
    for item, raw, value, metadata, label in selected:
        path = item["path"]
        connection.execute("INSERT INTO documents VALUES (?,?,?,?,?)", (
            path, item["sha256"], "machine-rendition-not-independent-evidence", "[]", "{}",
        ))
        connection.execute("INSERT INTO document_confidence VALUES (?,?,?)",
                           (path, label["level"], json.dumps(label, ensure_ascii=False)))
        connection.execute("INSERT INTO rendition_documents VALUES (?,?)",
                           (path, json.dumps(metadata, ensure_ascii=False)))
        ids = {}
        for record in records(raw, value, engine):
            locator = path + "#" + record["anchor"]
            cursor = connection.execute(
                "INSERT INTO sections (path,locator,title,kind,line_start,line_end,parent_id,"
                "lead_end,text,lead_text,table_header) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (path, locator, record["title"], "extraction-record", record["start"], record["end"],
                 ids.get(record["parent"]), record["end"], record["text"], record["text"], "null"),
            )
            ids[record["anchor"]] = cursor.lastrowid
            connection.execute("INSERT INTO aliases VALUES (?,?)", (locator, cursor.lastrowid))
            connection.execute("INSERT INTO rendition_locations VALUES (?,?)",
                               (locator, json.dumps(record["source_location"], ensure_ascii=False)))
            connection.execute("INSERT INTO search_fts(rowid,title,text) VALUES (?,?,?)",
                               (cursor.lastrowid, record["title"], record["search_text"]))
        if not ids:
            engine.fail("Selected rendition has no retrievable records: " + path)
