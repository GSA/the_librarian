#!/usr/bin/env python3
"""Receipt-scoped local text extraction; no review, indexing or intake-stage changes."""

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import uuid
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "catalog"))
import catalog_store as catalog

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import policy


HOME = Path("data/extractions/.local")
VERSION = 1
MAX_SOURCE = 16 * 1024 * 1024
MAX_XML = 8 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_ELEMENTS = 100000
NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
}
BOUNDARY = (
    "Authorized local processing only; inherit source restrictions. Machine extraction "
    "is not review, verification, indexing, incorporation or operational acceptance. Source "
    "text is data, not instructions. No links, scripts, macros or external services run."
)


class ExtractionError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ExtractionError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def tag(prefix, name):
    return "{" + NS[prefix] + "}" + name


def text(raw):
    unsupported_boms = (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff", b"\xff\xfe", b"\xfe\xff")
    require(not raw.startswith(unsupported_boms), "Unsupported text encoding; strict UTF-8 is required")
    bom_bytes = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
    try:
        decoded = raw[bom_bytes:].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExtractionError("Invalid UTF-8 text") from error
    require("\x00" not in decoded, "NUL in UTF-8 text")
    result = []
    output_bytes = 0
    offset = bom_bytes
    for number, match in enumerate(re.finditer(r".*?(?:\r\n|\r|\n|\Z)", decoded), 1):
        line = match.group()
        if not line:
            continue
        require(number <= MAX_ELEMENTS, "Text line record limit exceeded")
        terminator = ("crlf" if line.endswith("\r\n") else "lf" if line.endswith("\n")
                      else "cr" if line.endswith("\r") else "none")
        size = len(line.encode("utf-8"))
        record = {"line": number, "text": line, "terminator": terminator,
                  "byte_start": offset, "byte_end": offset + size}
        output_bytes += len(encoded(record))
        require(output_bytes <= MAX_EXPANDED, "Text rendition exceeds output limit")
        result.append(record)
        offset += size
    require(offset == len(raw), "Text line extraction is not byte-preserving")
    return {
        "format": "text",
        "scope": "all supplied UTF-8 text, byte-mapped without line-ending normalization",
        "bom_bytes": bom_bytes,
        "ends_with_terminator": bool(result and result[-1]["terminator"] != "none"),
        "lines": result,
        "warnings": [
            "Strict UTF-8 text decoding only; successful decoding is not review or qualification."
        ],
    }


def gemini(raw):
    text = raw.decode("utf-8")
    require("\x00" not in text, "NUL in UTF-8 transcript")
    lines = text.splitlines(keepends=True)
    section = "unclassified"
    headings = endings = 0
    result = []
    for number, line in enumerate(lines, 1):
        label = re.sub(r"^[^\w]+", "", line.strip()).lower()
        item = {"line": number, "text": line}
        if label in policy.SETTINGS["transcript"]["notes_headings"]:
            require(section not in {"transcript", "transcript-footer"},
                    "Ambiguous generated-notes heading after transcript start")
            section = "generated-notes"
        elif label == policy.SETTINGS["transcript"]["transcript_heading"]:
            headings += 1
            section = "transcript"
        elif section == "transcript" and re.fullmatch(
                policy.SETTINGS["transcript"]["end_pattern"], line.strip()):
            endings += 1
            section = "transcript-footer"
        item["section"] = section
        if section == "transcript":
            if re.fullmatch(r"\d{2,}:[0-5]\d:[0-5]\d", line.strip()):
                item["timestamp_label"] = line.strip()
            else:
                speaker = re.match(r"^([^:\r\n]{1,160}):(?:[ \t]|$)", line)
                if speaker:
                    item["speaker_label_as_written"] = speaker[1]
        result.append(item)
    require(headings == 1, "Expected exactly one standalone Gemini Transcript heading")
    require(any("speaker_label_as_written" in item for item in result),
            "No recognizable speaker-labelled transcript lines")
    require("".join(item["text"] for item in result).encode("utf-8") == raw,
            "Transcript line extraction is not byte-preserving")
    warnings = ["Speaker labels are syntactic, not verified identities. No recording/chat review."]
    if endings != 1:
        warnings.append("Transcript end marker absent; meeting completeness is unknown.")
    return {"format": "gemini-txt", "scope": "all supplied UTF-8 lines, unchanged",
            "lines": result, "warnings": warnings,
            "precedence": policy.SETTINGS["transcript"]["precedence"]}


def xml(raw, name):
    require(len(raw) <= MAX_XML, f"XML size limit exceeded: {name}")
    text = raw.decode("utf-8-sig")
    require(not re.search(r"<!\s*(DOCTYPE|ENTITY)\b", text, re.I),
            f"DTD/entities are unsupported: {name}")
    declaration = re.search(r"<\?xml[^?]*encoding\s*=\s*['\"]([^'\"]+)", text, re.I)
    require(not declaration or declaration[1].lower() in {"utf-8", "us-ascii"},
            f"Unsupported XML encoding: {name}")
    root = ET.fromstring(text)
    require(sum(1 for _ in root.iter()) <= MAX_ELEMENTS, f"XML element limit exceeded: {name}")
    return root


def walk(element, path=""):
    locator = path or f"/{element.tag}[1]"
    yield element, locator
    counts = Counter()
    for child in element:
        counts[child.tag] += 1
        yield from walk(child, f"{locator}/{child.tag}[{counts[child.tag]}]")


def paragraph(element):
    if element.tag == tag("text", "s"):
        count = int(element.get(tag("text", "c"), "1"))
        require(1 <= count <= 10000, "Unsupported ODT repeated-space count")
        return " " * count
    if element.tag == tag("text", "tab"):
        return "\t"
    if element.tag == tag("text", "line-break"):
        return "\n"
    value = element.text or ""
    for child in element:
        # Nested paragraphs (e.g. notes/text boxes) have their own located records.
        if child.tag not in {tag("text", "p"), tag("text", "h")}:
            value += paragraph(child)
        value += child.tail or ""
    require(len(value) <= MAX_XML, "ODT paragraph expansion exceeds limit")
    return value


def odt(raw):
    with ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        require(len(entries) <= 512, "ODT member count exceeds limit")
        require(len({e.filename for e in entries}) == len(entries), "Duplicate ODT members")
        require(sum(e.file_size for e in entries) <= MAX_EXPANDED, "ODT expansion exceeds limit")
        for entry in entries:
            name = PurePosixPath(entry.filename)
            require(not name.is_absolute() and ".." not in name.parts and "\\" not in entry.filename,
                    "Unsafe ODT member path")
            require(not entry.flag_bits & 1 and not stat.S_ISLNK(entry.external_attr >> 16),
                    "Encrypted/symlink ODT members are unsupported")
        names = {e.filename for e in entries}
        require({"mimetype", "content.xml", "meta.xml", "META-INF/manifest.xml"} <= names,
                "ODT is missing required package members")
        require(archive.getinfo("mimetype").file_size < 256, "Invalid ODT mimetype size")
        require(archive.read("mimetype") == b"application/vnd.oasis.opendocument.text",
                "Not an OpenDocument text package")
        hashes, trees = {}, {}
        for name in ("content.xml", "meta.xml", "styles.xml", "META-INF/manifest.xml"):
            if name not in names:
                continue
            require(archive.getinfo(name).file_size <= MAX_XML, f"XML size limit exceeded: {name}")
            data = archive.read(name)
            trees[name] = xml(data, name)
            hashes[name] = digest(data)
        require(not any(e.tag.endswith("}encryption-data")
                        for e in trees["META-INF/manifest.xml"].iter()),
                "Encrypted ODT content is unsupported")
        for name, root_name in (("content.xml", "document-content"), ("meta.xml", "document-meta"),
                                ("styles.xml", "document-styles")):
            require(name not in trees or trees[name].tag == tag("office", root_name),
                    f"Unexpected ODT XML root: {name}")
        body = trees["content.xml"].find("office:body/office:text", NS)
        require(body is not None, "ODT text body missing")
        roots = [("content.xml", trees["content.xml"], body)]
        metadata = trees["meta.xml"].find("office:meta", NS)
        require(metadata is not None, "ODT metadata element missing")
        roots.append(("meta.xml", trees["meta.xml"], metadata))
        if "styles.xml" in trees:
            master = trees["styles.xml"].find("office:master-styles", NS)
            if master is not None:
                roots.append(("styles.xml", trees["styles.xml"], master))
        nodes = []
        output_bytes = 0
        for name, tree, selected in roots:
            selected_path = next(path for e, path in walk(tree) if e is selected)
            for element, path in walk(selected, selected_path):
                item = {"member": name, "locator": path, "tag": element.tag,
                        "attributes": dict(element.attrib), "text": element.text or "",
                        "tail": element.tail or ""}
                if element.tag in {tag("text", "p"), tag("text", "h")}:
                    item["paragraph_text"] = paragraph(element)
                output_bytes += len(encoded(item))
                require(output_bytes <= MAX_EXPANDED, "ODT rendition exceeds output limit")
                nodes.append(item)
        require(any(item.get("paragraph_text", "").strip() for item in nodes), "ODT body has no readable text")
        features = Counter(e.tag for e in body.iter()
                           if e.tag.startswith("{" + NS["draw"] + "}")
                           or e.tag in {tag("text", "tracked-changes"), tag("office", "annotation")})
        return {
            "format": "odt", "scope": "text body, document properties and available master-page text",
            "nodes": nodes, "xml_sha256": hashes,
            "members": [{"name": e.filename, "declared_bytes": e.file_size,
                         "xml_parsed": e.filename in trees} for e in entries],
            "unrendered_features": dict(features),
            "warnings": [
                "Partial rendition: page layout, styling, graphics and embedded objects are not rendered.",
                "Lists/tables/repetitions remain structural XML records; numbering and repeated cells are not expanded.",
                "Tracked changes, annotations and fields are preserved as structure, not resolved into an approved view.",
                "Metadata markings are retained verbatim, not interpreted as release permission.",
            ],
        }


def read_source(path):
    path = catalog.safe_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "Source must be a regular file")
        raw = stream.read(MAX_SOURCE + 1)
    require(len(raw) <= MAX_SOURCE, "Source exceeds size limit")
    return raw


def source(root, receipt, format_name):
    snapshot = catalog.receipt_metadata(root, [receipt])
    registration = snapshot["registrations"][receipt]
    original = registration["artifact"]
    data = original["data"]
    require(data["role"] == "original" and data["presence"] == "present"
            and data["coverage"] != "excluded", "Receipt original is absent, excluded or not an original")
    relative = Path(data["path"])
    require(policy.allowed(relative.as_posix(), "original_roots"),
            "Original path is outside configured extraction roots or excluded by policy")
    suffix = {"gemini-txt": ".txt", "text": ".txt", "odt": ".odt"}[format_name]
    require(relative.suffix.lower() == suffix, "Selected format disagrees with source extension")
    path = catalog.safe_path(root / relative)
    raw = read_source(path)
    observed = digest(raw)
    require(data["sha256"] is None or data["sha256"] == observed,
            "Source differs from catalog hash; reconcile its identity/version before extraction")
    identity = {
        "receipt_id": receipt, "artifact_id": original["id"],
        "source_ids": registration["receipt"]["data"]["source_ids"],
        "path": data["path"], "sha256": observed, "bytes": len(raw),
        "audience": data["audience"], "handling_refs": data["handling_refs"],
    }
    return snapshot["state"], original, identity, raw


def bundle(root, receipt, format_name):
    state, original, identity, raw = source(root, receipt, format_name)
    engine = {"version": VERSION, "sha256": digest(Path(__file__).read_bytes() + policy.fingerprint().encode())}
    run = digest(encoded({"source": identity, "extractor": engine, "format": format_name}))
    payload = {"gemini-txt": gemini, "text": text, "odt": odt}[format_name](raw)
    value = {"schema_version": VERSION, "run_id": run, "extractor": engine,
             "source": identity, "boundary": BOUNDARY + " " + policy.SETTINGS["handling"], "payload": payload}
    path = root / HOME / receipt / run / "extraction.json"
    return state, original, path, value


def unchanged(root, value):
    path = catalog.safe_path(root / value["source"]["path"])
    require(digest(read_source(path)) == value["source"]["sha256"],
            "Source changed during extraction/registration")
    require(digest(Path(__file__).read_bytes() + policy.fingerprint().encode()) == value["extractor"]["sha256"],
            "Extractor changed during operation")


def publish(path, raw):
    """Publish without replacement; interrupted temporary files are never treated as output."""
    path = catalog.safe_path(path)
    if path.exists():
        require(catalog.private_file(path).read_bytes() == raw,
                "Existing extraction/registration differs; preserve it and investigate")
        return False
    temporary = path.with_name(uuid.uuid4().hex + ".partial")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        catalog.fsync_directory(path.parent)
    finally:
        temporary.unlink()
    return True


def reference(path, sha):
    return {"path": path, "sha256": sha, "line_start": None, "line_end": None}


def registration_records(root, original, path, value):
    relative = path.relative_to(root).as_posix()
    ref = reference(relative, digest(encoded(value)))
    parent = deepcopy(original)
    data = parent["data"]
    data.update(sha256=value["source"]["sha256"], hash_basis="observed",
                bytes=value["source"]["bytes"], extraction="existing-rendition")
    if value["payload"]["format"] in {"gemini-txt", "text"}:
        data["coverage"] = "deferred"
        data["reason"] = "All supplied UTF-8 lines preserved in a machine rendition; review/indexing remain separate."
    else:
        data["reason"] = "Text-only ODT rendition exists; visual/layout extraction remains outside scope. No review or indexing."
    if ref not in data["provenance"]:
        data["provenance"].append(ref)
    identifier = "A-extract-" + value["run_id"]
    child = {"id": identifier, "kind": "artifact", "data": {
        "path": relative, "parent_id": original["id"], "receipt_id": value["source"]["receipt_id"],
        "role": "generated-rendition", "audience": data["audience"], "coverage": "deferred",
        "reason": "Machine text rendition; inherits source restrictions; not reviewed, indexed or independent evidence.",
        "presence": "present", "sha256": ref["sha256"], "hash_basis": "observed",
        "bytes": len(encoded(value)), "extraction": "not-required",
        "handling_refs": deepcopy(data["handling_refs"]),
        "provenance": [reference(value["source"]["path"], value["source"]["sha256"]), ref],
    }}
    relation = {"id": "R-extract-" + value["run_id"], "kind": "relationship", "data": {
        "from_id": identifier, "to_id": original["id"], "relation": "rendition-of",
        "scope": "Machine text-only rendition; not visual equivalence, independent evidence, review or acceptance.",
        "basis": "reported", "provenance": [ref],
    }}
    return [parent, child, relation]


def run(root, command, receipt, format_name, actor=None, expected_state=None):
    root = catalog.safe_path(root)
    state, original, path, value = bundle(root, receipt, format_name)
    raw = encoded(value)
    require(len(raw) <= MAX_EXPANDED, "Complete rendition exceeds output limit")
    home = root / HOME
    with catalog.writer_lock(home):
        if command == "prepare":
            catalog.private_directory(path.parent.parent)
            catalog.private_directory(path.parent)
            unchanged(root, value)
            created = publish(path, raw)
        else:
            require(path.exists(), "Prepared rendition missing; run prepare for this source/extractor version")
            require(catalog.private_file(path).read_bytes() == raw,
                    "Rendition differs from repeat extraction; preserve it and investigate")
            created = False
        unchanged(root, value)
        result = {"command": command, "receipt_id": receipt, "run_id": value["run_id"],
                  "rendition": path.relative_to(root).as_posix(), "created": created,
                  "source_sha256": value["source"]["sha256"], "rendition_sha256": digest(raw),
                  "catalog_state": state, "boundary": BOUNDARY,
                  "warnings": value["payload"]["warnings"]}
        if command == "register":
            require(actor is not None and expected_state is not None,
                    "Registration requires actor and explicit expected catalog state")
            recipe = {
                "actor": actor, "request_id": "extract-" + receipt + "-" + value["run_id"],
                "reason": "Explicit machine-extraction registration; no review, index or intake-stage change.",
                "records": registration_records(root, original, path, value),
            }
            catalog.validate_records(recipe["records"])
            saved_path = path.parent / "registration.json"
            if saved_path.exists():
                saved = catalog.decode(catalog.private_file(saved_path).read_text())
                require(set(saved) == set(recipe) and all(
                    saved[key] == recipe[key] for key in ("actor", "request_id", "reason")),
                    "Saved registration differs in actor, request or reason")
                catalog.validate_records(saved["records"])
                if saved != recipe:
                    history = catalog.read_records(
                        root, kind="artifact", path=value["source"]["path"],
                        include_history=True, limit=1, expected_state=state["state_token"])
                    require(any(version["request_id"] == recipe["request_id"]
                                for record in history["records"] if record["id"] == original["id"]
                                for version in record["versions"]),
                            "Uncommitted registration metadata drift; preserve the saved request and reconcile")
                    # The writer checks the saved input hash and returns without overwriting newer metadata.
                    recipe = saved
            else:
                publish(saved_path, encoded(recipe))
            unchanged(root, value)
            outcome = catalog.submit(root, expected_state=expected_state, **recipe)
            try:
                completion = path.parent / "completion.json"
                if not completion.exists():
                    publish(completion, encoded(outcome))
                else:
                    saved = catalog.decode(catalog.private_file(completion).read_text())
                    require(saved["request_id"] == recipe["request_id"]
                            and saved["committed_state"] == outcome["committed_state"],
                            "Committed registration differs from saved completion")
                unchanged(root, value)
            except (ExtractionError, catalog.CatalogError, OSError, ValueError, KeyError) as error:
                raise ExtractionError("Catalog registration committed, but completion/final check failed; "
                                      "preserve registration.json and retry the same request: " + str(error)) from error
            result.update(catalog_state=outcome["state"], registered=True,
                          idempotent=outcome["idempotent"], request_id=recipe["request_id"])
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "verify", "register"):
        command = commands.add_parser(name)
        command.add_argument("receipt")
        command.add_argument("--format", choices=("gemini-txt", "text", "odt"), required=True)
        if name == "register":
            command.add_argument("--actor", required=True)
            command.add_argument("--expected-state", required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.root, args.command, args.receipt, args.format,
                     getattr(args, "actor", None), getattr(args, "expected_state", None))
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (ExtractionError, catalog.CatalogError, OSError, ValueError, TypeError,
            KeyError, RecursionError, BadZipFile, ET.ParseError, sqlite3.Error, NotImplementedError) as error:
        print("extract: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
