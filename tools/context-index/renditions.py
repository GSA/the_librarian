"""Explicitly selected, registered extraction records; never a raw-intake crawl."""

import hashlib
import json
import re
from bisect import bisect_right
from pathlib import PurePosixPath


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
        engine.fields(item, ("path", "sha256", "receipt_id", "format", "context"), "Rendition selection")
        if (not eligible(item["path"]) or item["path"] in seen
                or PurePosixPath(item["path"]).parts[3] != item["receipt_id"]
                or not isinstance(item["format"], str) or item["format"] not in {"gemini-txt", "odt"}
                or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            engine.fail("Invalid or duplicate rendition selection")
        if not item["context"]:
            engine.fail("Rendition selection requires maintained source context")
        engine.validate_context(item["context"])
        if any(not engine.eligible(engine.canonical_locator(locator).partition("#")[0])
               for locator in item["context"]):
            engine.fail("Rendition context must select maintained Markdown")
        seen.add(item["path"])


def inputs(root, config, engine):
    """Pin selected catalog records, original bytes and retained extraction bytes."""
    selections = config.get("renditions", [])
    if not selections:
        return [], ""
    catalog = engine.load_catalog()
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
        engine.fields(value, ("schema_version", "run_id", "extractor", "source", "boundary", "payload"),
                      "Extraction")
        engine.fields(value["extractor"], ("version", "sha256"), "Extractor identity")
        engine.fields(value["source"], ("receipt_id", "artifact_id", "source_ids", "path",
                                       "sha256", "bytes", "audience", "handling_refs"), "Extraction source")
        payload = value["payload"]
        required = (("format", "scope", "warnings", "lines", "precedence")
                    if item["format"] == "gemini-txt" else
                    ("format", "scope", "warnings", "nodes", "xml_sha256", "members", "unrendered_features"))
        engine.fields(payload, required, "Extraction payload")
        if (type(value["schema_version"]) is not int
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
        child = by_id.get("A-extract-" + run, {})
        relation = by_id.get("R-extract-" + run, {})
        expected_source = {
            "receipt_id": item["receipt_id"], "artifact_id": original["id"],
            "source_ids": snapshot["registrations"][item["receipt_id"]]["receipt"]["data"]["source_ids"],
            **{key: data[key] for key in ("path", "sha256", "bytes", "audience", "handling_refs")},
        }
        child_data = child.get("data", {})
        relation_data = relation.get("data", {})
        if (value["schema_version"] != 1 or value["run_id"] != run
                or value["payload"]["format"] != item["format"] or source != expected_source
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
            original_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        if original_hash != source["sha256"]:
            engine.fail("Original source missing or changed: " + source["path"])
        payload = value["payload"]
        metadata = {
            "source": source, "rendition_path": item["path"], "rendition_sha256": item["sha256"],
            "extractor": value["extractor"], "run_id": run,
            "extraction_boundary": value["boundary"],
            "format": payload["format"], "extraction_scope": payload["scope"],
            "warnings": payload["warnings"],
            "selection": ("Transcript-classified records only; generated notes and footer not indexed."
                          if item["format"] == "gemini-txt" else
                          "Located XML records only; text rendition, not a rendered or approved view."),
            "citation_contract": "Packet line ranges cite extraction JSON, not original lines or pages. "
                                 "Resolve source_location for the original line or XML member/element. "
                                 "Extraction and retrieval are not new review, corroboration or acceptance.",
        }
        for key in ("precedence", "xml_sha256", "unrendered_features"):
            if key in payload:
                metadata[key] = payload[key]
        result.append((item, raw, value, metadata))
        identities.append({"original": original, "rendition": child, "relationship": relation,
                           "source": source, "rendition_sha256": item["sha256"]})
    return result, engine.digest(json.dumps(identities, sort_keys=True).encode("utf-8"))


def records(raw, value, engine):
    """Return exact on-disk JSON line spans alongside decoded search-only text."""
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    line_starts, position = [], 0
    for line in lines:
        line_starts.append(position)
        position += len(line)
    payload = value["payload"]
    key = "lines" if payload["format"] == "gemini-txt" else "nodes"
    match = re.search(r'^\s*"' + key + r'": \[\s*$', text, re.M)
    if match is None:
        engine.fail("Unsupported extraction serialization: missing record array")
    offset = match.end()
    decoder = json.JSONDecoder(object_pairs_hook=engine.strict_object)
    timestamp = None
    parents = {}
    for index, record in enumerate(payload[key]):
        if key == "lines":
            engine.fields(record, ("line", "text", "section"), "Transcript line",
                          optional=("speaker_label_as_written", "timestamp_label"))
            if (type(record["line"]) is not int or record["line"] != index + 1
                    or record["section"] not in {
                        "unclassified", "generated-notes", "transcript", "transcript-footer"}):
                engine.fail("Invalid transcript line numbering/classification")
        else:
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
        if key == "lines":
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
