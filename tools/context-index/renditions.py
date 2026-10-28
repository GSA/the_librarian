"""Explicitly selected, registered extraction records; never a raw-intake crawl."""

import hashlib
import json
import re
from bisect import bisect_right
from pathlib import PurePosixPath

import qualification


def eligible(path):
    return isinstance(path, str) and bool(re.fullmatch(
        r"data/extractions/\.local/D\d+/[a-f0-9]{64}/extraction\.json", path
    ))


def validate(config, engine):
    selections = config.get("renditions", [])
    if not isinstance(selections, list) or len(selections) > 10:
        engine.fail("renditions must be a list of at most ten explicit selections")
    seen = set()
    for item in selections:
        engine.fields(item, ("path", "sha256", "receipt_id", "format", "context"),
                      "Rendition selection", optional=("review_event_ids",))
        if (not eligible(item["path"]) or item["path"] in seen
                or PurePosixPath(item["path"]).parts[3] != item["receipt_id"]
                or not isinstance(item["format"], str) or item["format"] not in {"gemini-txt", "text", "odt", "docling"}
                or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            engine.fail("Invalid or duplicate rendition selection")
        review_ids = item.get("review_event_ids")
        valid_review_ids = (isinstance(review_ids, list) and bool(review_ids)
                            and all(isinstance(event_id, str)
                                    and re.fullmatch(r"rev-[a-z0-9-]{1,100}", event_id)
                                    for event_id in review_ids))
        if (item["format"] == "docling" and
                (not valid_review_ids or len(review_ids) != len(set(review_ids)))):
            engine.fail("Docling selection requires unique review_event_ids")
        if item["format"] != "docling" and review_ids is not None:
            engine.fail("review_event_ids are supported only for Docling selections")
        if not item["context"]:
            engine.fail("Rendition selection requires maintained source context")
        engine.validate_context(item["context"])
        if any(not engine.eligible(engine.canonical_locator(locator).partition("#")[0])
               for locator in item["context"]):
            engine.fail("Rendition context must select maintained Markdown")
        seen.add(item["path"])


def inputs(root, config, engine, review_snapshot=None):
    """Pin selected catalog records, original bytes and retained extraction bytes."""
    selections = config.get("renditions", [])
    if not selections:
        return [], ""
    catalog = engine.load_catalog()
    if any(item["format"] == "docling" for item in selections) and review_snapshot is None:
        review_snapshot = engine.load_event_reader().read_snapshot(root)
    snapshot = catalog.receipt_metadata(root, [item["receipt_id"] for item in selections])
    state = snapshot["state"]["state_token"]
    result, identities = [], []
    for item in selections:
        original = snapshot["registrations"][item["receipt_id"]]["artifact"]
        data = original["data"]
        if not engine.original_allowed(data["path"]):
            engine.fail("Original source excluded by current policy")
        path = engine.no_symlinks(root / item["path"])
        raw = catalog.private_file(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            engine.fail("Selected rendition hash changed: " + item["path"])
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=engine.strict_object)
        if item["format"] == "docling":
            engine.fields(value, ("schema", "run_id", "extractor", "source", "boundary", "conversion"),
                          "Docling extraction")
        else:
            engine.fields(value, ("schema_version", "run_id", "extractor", "source", "boundary", "payload"),
                          "Extraction")
        if item["format"] != "docling":
            engine.fields(value["extractor"], ("version", "sha256"), "Extractor identity")
        engine.fields(value["source"], ("receipt_id", "artifact_id", "source_ids", "path",
                                       "sha256", "bytes", "audience", "handling_refs"), "Extraction source")
        payload = value.get("payload")
        required = (("format", "scope", "warnings", "lines", "precedence")
                    if item["format"] == "gemini-txt" else
                    ("format", "scope", "warnings", "lines", "bom_bytes", "ends_with_terminator")
                    if item["format"] == "text" else
                    ("format", "scope", "warnings", "nodes", "xml_sha256", "members", "unrendered_features"))
        if item["format"] != "docling":
            engine.fields(payload, required, "Extraction payload")
        if item["format"] == "docling":
            if value.get("schema") != "docling-rendition-v1":
                engine.fail("Unsupported Docling extraction schema")
        elif (type(value["schema_version"]) is not int
                or not isinstance(payload["warnings"], list)
                or not all(isinstance(warning, str) for warning in payload["warnings"])):
            engine.fail("Unsupported extraction schema or warnings")
        records = catalog.read_records(root, receipt=item["receipt_id"], limit=100,
                                       expected_state=state)
        if records["next_after"] is not None:
            engine.fail("Selected rendition receipt exceeds the bounded catalog lookup")
        by_id = {record["id"]: record for record in records["records"]}
        source = value["source"]
        run = PurePosixPath(item["path"]).parts[4]
        prefix = "docling" if item["format"] == "docling" else "extract"
        child = by_id.get("A-" + prefix + "-" + run, {})
        relation = by_id.get("R-" + prefix + "-" + run, {})
        expected_source = {
            "receipt_id": item["receipt_id"], "artifact_id": original["id"],
            "source_ids": snapshot["registrations"][item["receipt_id"]]["receipt"]["data"]["source_ids"],
            **{key: data[key] for key in ("path", "sha256", "bytes", "audience", "handling_refs")},
        }
        child_data = child.get("data", {})
        relation_data = relation.get("data", {})
        schema_matches = (value.get("schema") == "docling-rendition-v1"
                          if item["format"] == "docling" else value.get("schema_version") == 1)
        format_matches = (item["format"] == "docling" or payload["format"] == item["format"])
        if (not schema_matches or value["run_id"] != run
                or not format_matches or source != expected_source
                or data["role"] != "original" or data["presence"] != "present"
                or data["coverage"] == "excluded"
                or child_data.get("path") != item["path"]
                or child_data.get("sha256") != item["sha256"]
                or child_data.get("bytes") != len(raw)
                or child_data.get("parent_id") != original["id"]
                or child_data.get("role") != "generated-rendition"
                or child_data.get("presence") != "present"
                or child_data.get("coverage") != "indexed"
                or child_data.get("handling_refs") != source["handling_refs"]
                or child_data.get("audience") != source["audience"]
                or relation_data.get("from_id") != child.get("id")
                or relation_data.get("to_id") != original["id"]
                or relation_data.get("relation") != "rendition-of"):
            engine.fail("Rendition/source registration mismatch: " + item["path"])
        original_path = engine.no_symlinks(root / engine.relative_name(source["path"]))
        if not original_path.is_file() or original_path.stat().st_size != source["bytes"]:
            engine.fail("Original source missing or changed: " + source["path"])
        with original_path.open("rb") as stream:
            original_raw = stream.read()
            original_hash = hashlib.sha256(original_raw).hexdigest()
        if original_hash != source["sha256"]:
            engine.fail("Original source missing or changed: " + source["path"])
        payload = value.get("payload")
        if item["format"] == "text":
            validate_text_mapping(payload, original_raw, engine)
        identity = {"original": original, "rendition": child, "relationship": relation,
                    "source": source, "rendition_sha256": item["sha256"]}
        docling_qualification = None
        if item["format"] == "docling":
            docling_qualification = qualification.docling(
                item, value, identity, review_snapshot, engine)
            validate_docling_conversion(value, engine)
        metadata = {
            "source": source, "rendition_path": item["path"], "rendition_sha256": item["sha256"],
            "extractor": value["extractor"], "run_id": run,
            "extraction_boundary": value["boundary"],
            "format": item["format"],
            "extraction_scope": payload["scope"] if payload is not None else "Docling conversion envelope",
            "warnings": (payload["warnings"] if payload is not None
                         else value["conversion"]["warnings"]),
            "selection": ("Transcript-classified records only; generated notes and footer not indexed."
                          if item["format"] == "gemini-txt" else
                          "All byte-mapped physical text lines explicitly selected; decoding is not review."
                          if item["format"] == "text" else
                          "Strictly qualified selected Docling structural records."
                          if item["format"] == "docling" else
                          "Located XML records only; text rendition, not a rendered or approved view."),
            "citation_contract": (
                "Docling packet line ranges are null; source_location gives the exact retained-extraction "
                "byte range, JSON pointer, self_ref and page/bounding-box provenance."
                if item["format"] == "docling" else
                "Packet line ranges cite extraction JSON, not original lines or pages. "
                "Resolve source_location for the original line, byte range or XML member/element. "
                "Extraction and retrieval are not new review, corroboration or acceptance."),
        }
        for key in ("precedence", "xml_sha256", "unrendered_features", "bom_bytes", "ends_with_terminator"):
            if payload is not None and key in payload:
                metadata[key] = payload[key]
        if docling_qualification is not None:
            metadata["qualification"] = docling_qualification
            identity["qualification"] = docling_qualification
        result.append((item, raw, value, metadata))
        identities.append(identity)
    return result, engine.digest(json.dumps(identities, sort_keys=True).encode("utf-8"))


def validate_text_mapping(payload, source_raw, engine):
    if type(payload["bom_bytes"]) is not int or payload["bom_bytes"] not in {0, 3}:
        engine.fail("Invalid text BOM declaration")
    if (payload["bom_bytes"] == 3) != source_raw.startswith(b"\xef\xbb\xbf"):
        engine.fail("Text BOM declaration does not match original bytes")
    for record in payload["lines"]:
        if (not isinstance(record, dict) or not isinstance(record.get("text"), str)
                or type(record.get("line")) is not int or type(record.get("terminator")) is not str
                or type(record.get("byte_start")) is not int or type(record.get("byte_end")) is not int):
            engine.fail("Invalid text line field types")
    offset = payload["bom_bytes"]
    canonical = []
    for number, match in enumerate(re.finditer(b".*?(?:\\r\\n|\\r|\\n|\\Z)", source_raw[offset:]), 1):
        raw_line = match.group()
        if not raw_line:
            continue
        text = raw_line.decode("utf-8")
        terminator = ("crlf" if text.endswith("\r\n") else "lf" if text.endswith("\n")
                      else "cr" if text.endswith("\r") else "none")
        canonical.append({"line": number, "text": text, "terminator": terminator,
                          "byte_start": offset, "byte_end": offset + len(raw_line)})
        offset += len(raw_line)
    if payload["lines"] != canonical:
        engine.fail("Invalid text physical line mapping")
    offset = payload["bom_bytes"]
    for index, record in enumerate(payload["lines"]):
        engine.fields(record, ("line", "text", "terminator", "byte_start", "byte_end"),
                      "Text line")
        expected_terminator = ("crlf" if record["text"].endswith("\r\n") else
                               "lf" if record["text"].endswith("\n") else
                               "cr" if record["text"].endswith("\r") else "none")
        encoded_size = len(record["text"].encode("utf-8"))
        if (record["line"] != index + 1 or record["terminator"] != expected_terminator
                or record["byte_start"] != offset
                or record["byte_end"] != record["byte_start"] + encoded_size):
            engine.fail("Invalid text line numbering or byte mapping")
        if source_raw[record["byte_start"]:record["byte_end"]] != record["text"].encode("utf-8"):
            engine.fail("Text record does not match original byte slice")
        offset = record["byte_end"]
    expected_end = bool(payload["lines"] and payload["lines"][-1]["terminator"] != "none")
    if offset != len(source_raw) or type(payload["ends_with_terminator"]) is not bool \
            or payload["ends_with_terminator"] != expected_end:
        engine.fail("Invalid text byte mapping or final terminator state")


def validate_docling_conversion(value, engine):
    conversion = value.get("conversion")
    engine.fields(conversion, ("schema", "format", "document", "pages", "warnings",
                               "confidence", "omissions"), "Docling conversion")
    if conversion["schema"] != "docling-conversion-v1" or conversion["format"] not in {"pdf", "image"}:
        engine.fail("Unsupported Docling conversion schema or format")
    if (not isinstance(conversion["warnings"], list)
            or not all(isinstance(warning, dict) for warning in conversion["warnings"])):
        engine.fail("Invalid Docling conversion warnings")
    return conversion


def docling_records(raw, value, engine):
    conversion = validate_docling_conversion(value, engine)
    document = conversion["document"]
    engine.fields(document, ("texts", "tables"), "Docling document", optional=(
        "schema_name", "version", "name", "body", "pages", "pictures", "key_value_items",
        "form_items", "furniture", "groups"))
    unsupported = ("pictures", "key_value_items", "form_items", "furniture", "groups")
    if any(document.get(key) not in (None, [], {}) for key in unsupported):
        engine.fail("Docling document contains unsupported content collections")
    pages = conversion["pages"]
    if (not isinstance(pages, list) or not pages
            or any(not isinstance(page, dict) or type(page.get("page_no")) is not int
                   for page in pages)):
        engine.fail("Invalid Docling page inventory")
    page_numbers = {page["page_no"] for page in pages}
    if len(page_numbers) != len(pages):
        engine.fail("Duplicate Docling page inventory")
    ordinal = 0
    for kind, items in (("text", document["texts"]), ("table", document["tables"])):
        if not isinstance(items, list):
            engine.fail("Invalid Docling document records")
        for index, item in enumerate(items):
            expected_ref = f"#/{kind}s/{index}"
            if (not isinstance(item, dict) or item.get("self_ref") != expected_ref
                    or ("label" in item and not isinstance(item["label"], str))
                    or not isinstance(item.get("prov"), list) or not item["prov"]):
                engine.fail("Invalid Docling record identity or provenance")
            locations = []
            for provenance in item["prov"]:
                bbox = provenance.get("bbox", {}) if isinstance(provenance, dict) else {}
                if (type(provenance.get("page_no")) is not int
                        or provenance["page_no"] not in page_numbers
                        or set(bbox) != {"l", "t", "r", "b", "coord_origin"}
                        or bbox["coord_origin"] not in {"TOPLEFT", "BOTTOMLEFT"}
                        or any(type(bbox[key]) not in {int, float}
                               for key in ("l", "t", "r", "b"))):
                    engine.fail("Invalid Docling page provenance")
                locations.append({"page_no": provenance["page_no"], "bbox": bbox})
            if kind == "text":
                if not isinstance(item.get("text"), str) or not item["text"]:
                    engine.fail("Invalid Docling text record")
                search_text = item["text"]
            else:
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
                       or not (0 <= cell["start_row_offset_idx"] < cell["end_row_offset_idx"]
                               <= data["num_rows"])
                       or not (0 <= cell["start_col_offset_idx"] < cell["end_col_offset_idx"]
                               <= data["num_cols"])
                       for cell in cells):
                    engine.fail("Invalid Docling table cells")
                search_text = " ".join(cell["text"] for cell in cells)
            exact_raw = json.dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            byte_start = raw.find(exact_raw)
            if byte_start < 0 or raw.find(exact_raw, byte_start + 1) >= 0:
                engine.fail("Docling record does not have a unique exact retained byte range")
            byte_end = byte_start + len(exact_raw)
            exact = raw[byte_start:byte_end].decode("utf-8")
            ordinal += 1
            yield {"anchor": kind + "-" + str(index + 1),
                   "title": (item.get("label") or kind).replace("_", " ").title(),
                   "start": ordinal, "end": ordinal, "text": exact,
                   "search_text": search_text, "parent": None,
                   "source_location": {"json_pointer": f"/conversion/document/{kind}s/{index}",
                                       "byte_start": byte_start, "byte_end": byte_end,
                                       "self_ref": expected_ref, "page_no": locations[0]["page_no"],
                                       "bbox": locations[0]["bbox"], "provenance": locations}}


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
    key = "lines" if payload["format"] in {"gemini-txt", "text"} else "nodes"
    if not payload[key]:
        return
    match = re.search(r'^\s*"' + key + r'": \[\s*$', text, re.M)
    if match is None:
        engine.fail("Unsupported extraction serialization: missing record array")
    offset = match.end()
    decoder = json.JSONDecoder(object_pairs_hook=engine.strict_object)
    timestamp = None
    parents = {}
    for index, record in enumerate(payload[key]):
        if payload["format"] == "gemini-txt":
            engine.fields(record, ("line", "text", "section"), "Transcript line",
                          optional=("speaker_label_as_written", "timestamp_label"))
            if (type(record["line"]) is not int or record["line"] != index + 1
                    or record["section"] not in {
                        "unclassified", "generated-notes", "transcript", "transcript-footer"}):
                engine.fail("Invalid transcript line numbering/classification")
        elif payload["format"] != "text":
            engine.fields(record, ("member", "locator", "tag", "attributes", "text", "tail"),
                          "ODT node", optional=("paragraph_text",))
        while offset < len(text) and text[offset] in " \t\r\n,":
            offset += 1
        decoded, end = decoder.raw_decode(text, offset)
        if decoded != record:
            engine.fail("Extraction record/range mismatch")
        start_line = bisect_right(line_starts, offset)
        end_line = bisect_right(line_starts, end - 1)
        offset = end
        parent = None
        if payload["format"] == "gemini-txt":
            if record["section"] != "transcript":
                continue
            anchor = "line-" + str(record["line"])
            location = {"json_pointer": f"/payload/lines/{index}",
                        "original_line": record["line"], "section": record["section"]}
            if "timestamp_label" in record:
                timestamp = anchor
            elif timestamp:
                parent = timestamp
            title = record.get("speaker_label_as_written", record.get("timestamp_label", "Transcript"))
            search_text = record["text"]
        elif payload["format"] == "text":
            anchor = "line-" + str(record["line"])
            location = {"json_pointer": f"/payload/lines/{index}",
                        "original_line": record["line"], "byte_start": record["byte_start"],
                        "byte_end": record["byte_end"], "terminator": record["terminator"]}
            title = "Line " + str(record["line"])
            search_text = record["text"]
        else:
            anchor = "node-" + str(index + 1)
            location = {"json_pointer": f"/payload/nodes/{index}", "member": record["member"],
                        "xml_locator": record["locator"]}
            parent_path = re.sub(r"/\{[^}]*\}[^/]+\[\d+\]$", "", record["locator"])
            parent = parents.get((record["member"], parent_path))
            parents[(record["member"], record["locator"])] = anchor
            title = record["tag"].split("}")[-1]
            search_text = record.get("paragraph_text", record["text"]) + record["tail"]
        yield {
            "anchor": anchor, "title": title, "start": start_line, "end": end_line,
            "text": "".join(lines[start_line - 1:end_line]), "search_text": search_text,
            "parent": parent, "source_location": location,
        }


def store(connection, selected, engine):
    for item, raw, value, metadata in selected:
        path = item["path"]
        connection.execute("INSERT INTO documents VALUES (?,?,?,?,?)", (
            path, item["sha256"], "machine-rendition-not-independent-evidence", "[]", "{}",
        ))
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
