#!/usr/bin/env python3
"""Receipt-scoped conversion of office documents and RTF to a derived Markdown text rendition.

The registered original stays the catalogued source. Conversion runs ``convert_worker.py`` (stdlib
only) in a child process per document with a wall-clock timeout, resource limits and an output
cap; legacy binary Office formats are first converted to their OOXML/ODF successor by a local
LibreOffice (``soffice``) only when one is installed. The rendition records the converter identity
(code hash, runtime, fidelity notes) and uses the same line-locator records, registration shape and
review/qualification path as the strict text extractor.
"""
import argparse
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from zipfile import BadZipFile, ZipFile

from tools import policy
from tools.catalog import catalog_store as catalog
from tools.extraction import bounded, extract, office_runtime


VERSION = 1
CONVERTER = "librarian-convert"
WORKER = Path(__file__).with_name("convert_worker.py")
MAX_SOURCE = 64 * 1024 * 1024
MAX_OUTPUT = 80 * 1024 * 1024
MAX_STDERR = 64 * 1024
WALL_SECONDS = 300
SOFFICE_SECONDS = 300
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Content decides the format; the suffix must be one a document of that format plausibly carries
# (a ".doc" that is really a Word package converts as "docx").
FORMATS = {
    "docx": frozenset({".docx", ".docm", ".dotx", ".dotm", ".doc"}),
    "xlsx": frozenset({".xlsx", ".xlsm", ".xltx", ".xltm", ".xls"}),
    "pptx": frozenset({".pptx", ".pptm", ".ppsx", ".potx", ".ppt"}),
    "vsdx": frozenset({".vsdx", ".vsdm", ".vsd"}),
    "rtf": frozenset({".rtf", ".doc"}),
    "office-legacy": frozenset({".doc", ".xls", ".ppt", ".vsd"}),
}
SUFFIXES = frozenset().union(*FORMATS.values())
# Legacy suffix -> (soffice export filter, worker format of the exported file).
LEGACY = {".doc": ("docx", "docx"), ".xls": ("xlsx", "xlsx"), ".ppt": ("pptx", "pptx"),
          ".vsd": ("odg", "odg")}
PACKAGES = (("docx", "word/document.xml"), ("xlsx", "xl/workbook.xml"),
            ("pptx", "ppt/presentation.xml"), ("vsdx", "visio/document.xml"))
ENCRYPTED = "EncryptedPackage".encode("utf-16-le")
WARNING = ("Machine conversion of a {source} original to Markdown text; layout, formatting, images and "
           "embedded objects are not preserved. Conversion is not review or qualification.")
SCOPE = "Text, headings, lists and tables converted to Markdown lines from the {source} original"


class ConversionError(extract.ExtractionError):
    """A refused or failed conversion; ``code`` is a stable hyphenated reason."""

    def __init__(self, code, message):
        super().__init__(message + " (" + code + ")")
        self.code = code


def require(condition, code, message):
    if not condition:
        raise ConversionError(code, message)


def sniff(raw, suffix):
    """``(format, None)`` for a convertible document, ``(None, code)`` for a refused container,
    ``(None, None)`` when the bytes are not a container this module reads (text is decided elsewhere)."""
    package = suffix in SUFFIXES and suffix not in LEGACY and suffix != ".rtf"
    if raw.startswith((b"PK\x03\x04", b"PK\x05\x06")) or (package and raw.startswith(b"PK")):
        # A package-suffixed file starting "PK" is a (possibly truncated) zip, never text.
        try:
            with ZipFile(io.BytesIO(raw)) as archive:
                names = set(archive.namelist())
        except (BadZipFile, ValueError, OSError, EOFError):
            return None, "container-invalid"
        for format_name, part in PACKAGES:
            if part in names:
                return (format_name, None) if suffix in FORMATS[format_name] else (None, "suffix-mismatch")
        return None, "unsupported-container"
    if raw.startswith(OLE):
        if ENCRYPTED in raw:
            return None, "encrypted-document"
        return ("office-legacy", None) if suffix in LEGACY else (None, "unsupported-container")
    if raw.startswith(b"{\\rtf"):
        return ("rtf", None) if suffix in FORMATS["rtf"] else (None, "suffix-mismatch")
    return None, None


def text_refusal(raw):
    """None when ``raw`` is strict UTF-8 text the text extractor admits, else a refusal code."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
        return "unsupported-encoding"
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "not-utf8-text"
    if "\0" in decoded:
        return "binary-content"
    controls = sum(1 for c in decoded if (ord(c) < 32 and c not in "\t\n\r\f\v") or c == "\x7f")
    if controls * 100 > max(len(decoded), 1):
        return "control-characters"
    return None


def soffice():
    """The ``soffice`` a legacy conversion would use (see ``office_runtime.locate``), or None."""
    path = office_runtime.locate()
    return str(path) if path is not None else None


def runtime_refusal():
    """``needs-converter-runtime`` (no LibreOffice), ``needs-converter-sandbox`` (no confinement) or None."""
    return office_runtime.refusal()


def admission(raw, suffix):
    """``sniff`` plus runtime availability: legacy formats need a provisioned, confinable LibreOffice."""
    format_name, code = sniff(raw, suffix)
    if format_name == "office-legacy":
        code = runtime_refusal()
        if code is not None:
            return None, code
    return format_name, code


def runtime(format_name):
    """The external runtime a format's conversion depends on; part of the conversion identity."""
    if format_name != "office-legacy":
        return {"python": "stdlib"}
    try:
        return {"python": "stdlib", **office_runtime.identity()}
    except bounded.BoundedError as error:
        raise ConversionError(error.code, str(error)) from error


def code_sha256():
    return extract.digest(Path(__file__).read_bytes() + WORKER.read_bytes() + Path(office_runtime.__file__).read_bytes())


def request_version(format_name):
    """The extraction request-name suffix of a converted format (like MHTML's ``-mN``).

    It covers the converter code and, for legacy input, the LibreOffice version, so changing
    either makes cohort intake extract the document again instead of reusing the old stages.
    """
    try:
        external = runtime(format_name)
    except ConversionError:
        external = {"python": "stdlib", "soffice": None}
    key = (code_sha256(), extract.encoded(external))
    if key not in _VERSIONS:
        _VERSIONS.clear()
        _VERSIONS[key] = "-c" + extract.digest((key[0] + key[1].decode()).encode())[:12]
    return _VERSIONS[key]


_VERSIONS = {}


def fingerprint(root, format_name):
    """Converter identity: the converter, worker and sandbox files, the toolkit policy and the external runtime."""
    return extract.digest((code_sha256() + policy.fingerprint(root)
                           + extract.encoded(runtime(format_name)).decode()).encode())


def _read(path):
    path = catalog.safe_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "unsafe-path", "Source must be a regular file")
        raw = stream.read(MAX_SOURCE + 1)
    require(len(raw) <= MAX_SOURCE, "over-limit", "Source exceeds size limit")
    return raw


def source(root, receipt, format_name):
    require(format_name in FORMATS, "unsupported-format", "Unknown conversion format")
    snapshot = catalog.receipt_metadata(root, [receipt])
    registration = snapshot["registrations"][receipt]
    original = registration["artifact"]
    data = original["data"]
    extract.require(data["role"] == "original" and data["presence"] == "present"
                    and data["coverage"] != "excluded", "Receipt original is absent, excluded or not an original")
    relative = Path(data["path"])
    extract.require(policy.allowed(relative.as_posix(), "original_roots", root),
                    "Original path is outside configured extraction roots or excluded by policy")
    suffix = relative.suffix.lower()
    extract.require(suffix in FORMATS[format_name], "Selected format disagrees with source extension")
    path = catalog.safe_path(root / relative)
    raw = _read(path)
    observed = extract.digest(raw)
    extract.require(data["sha256"] is None or data["sha256"] == observed,
                    "Source differs from catalog hash; reconcile its identity/version before extraction")
    require(sniff(raw, suffix)[0] == format_name, "format-mismatch", "Source content is not the selected format")
    identity = {
        "receipt_id": receipt, "artifact_id": original["id"],
        "source_ids": registration["receipt"]["data"]["source_ids"],
        "path": data["path"], "sha256": observed, "bytes": len(raw),
        "audience": data["audience"], "handling_refs": data["handling_refs"],
    }
    return snapshot["state"], original, identity, path


def _environment():
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8",
            "HOME": os.environ.get("HOME", "/")}


def _bounded(command, timeout, limit, cwd=None):
    """Run ``command`` in its own session; return (returncode, stdout) or raise a coded refusal."""
    try:
        return bounded.run(command, timeout, limit, env=_environment(), stderr_limit=MAX_STDERR, cwd=cwd)
    except bounded.BoundedError as error:
        raise ConversionError(error.code, str(error)) from error


def _worker(format_name, path, sha):
    code, raw = _bounded([sys.executable, "-I", str(WORKER), format_name, str(path), sha],
                         WALL_SECONDS, MAX_OUTPUT)
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ConversionError("conversion-failed", "Converter produced no result (exit %s)" % code) from error
    require(isinstance(result, dict) and type(result.get("ok")) is bool, "conversion-failed",
            "Invalid converter result")
    if not result["ok"]:
        code_ = result.get("code")
        require(isinstance(code_, str) and code_.replace("-", "").isalnum(), "conversion-failed",
                "Invalid converter refusal")
        raise ConversionError(code_, "Conversion refused: " + str(result.get("message", ""))[:200])
    require(code == 0 and isinstance(result.get("markdown"), str) and isinstance(result.get("fidelity"), dict),
            "conversion-failed", "Invalid converter result")
    return result["markdown"], result["fidelity"]


def _legacy(root, path, sha):
    """Convert a legacy binary original with the sandboxed LibreOffice into its successor format, then convert that."""
    target, worker_format = LEGACY[path.suffix.lower()]
    raw = _read(path)
    require(extract.digest(raw) == sha, "source-changed", "Source changed during conversion")
    home = catalog.private_directory(root / extract.HOME)
    try:
        with office_runtime.exported(home, raw, path.suffix.lower(), target, SOFFICE_SECONDS) as produced:
            converted = _read(produced)
            markdown, fidelity = _worker(worker_format, produced, extract.digest(converted))
    except bounded.BoundedError as error:
        raise ConversionError(error.code, str(error)) from error
    fidelity = deepcopy(fidelity)
    fidelity["notes"] = sorted(set(fidelity.get("notes", [])) | {"legacy binary converted by LibreOffice to "
                                                                  + target + " first"})
    return markdown, fidelity, target


def convert(root, format_name, path, sha):
    """The ``converted`` payload of one original; raises ``ConversionError`` with a code on refusal."""
    if format_name == "office-legacy":
        markdown, fidelity, via = _legacy(root, path, sha)
    else:
        markdown, fidelity = _worker(format_name, path, sha)
        via = None
    lines = [{"line": number, "text": text}
             for number, text in enumerate(markdown.splitlines(keepends=True), 1)]
    require(lines, "conversion-empty", "The document has no convertible text")
    require(len(lines) <= extract.MAX_ELEMENTS, "conversion-limit", "Converted line record limit exceeded")
    warnings = [WARNING.format(source=format_name)]
    if fidelity.get("truncated"):
        warnings.append("Conversion was truncated at a size cap; see conversion.fidelity.notes.")
    return {
        "format": "converted",
        "scope": SCOPE.format(source=format_name),
        "warnings": warnings,
        "lines": lines,
        "conversion": {
            "source_format": format_name, "via": via, "converter": CONVERTER, "version": VERSION,
            "code_sha256": code_sha256(), "lock_sha256": None, "runtime": runtime(format_name),
            "fidelity": fidelity,
        },
    }


# One remembered conversion per (format, source hash, converter identity); see ``extract._PARSED``.
_PARSED = OrderedDict()
_KEEP, _KEEP_BYTES = 1, 32 * 1024 * 1024


def forget_parsed():
    _PARSED.clear()


def _trim_parsed():
    while len(_PARSED) > _KEEP or (len(_PARSED) > 1
                                   and sum(size for _, size in _PARSED.values()) > _KEEP_BYTES):
        _PARSED.popitem(last=False)


@contextmanager
def keeping_parsed(count):
    global _KEEP
    previous = _KEEP
    _KEEP = max(previous, count)
    try:
        yield
    finally:
        _KEEP = previous
        _trim_parsed()


def _parse(root, format_name, path, identity, engine):
    key = (format_name, identity["sha256"], engine["sha256"])
    if key in _PARSED:
        _PARSED.move_to_end(key)
        return _PARSED[key][0]
    payload = convert(root, format_name, path, identity["sha256"])
    _PARSED[key] = (payload, sum(len(line["text"]) for line in payload["lines"]))
    _trim_parsed()
    return payload


def bundle(root, receipt, format_name):
    state, original, identity, path = source(root, receipt, format_name)
    settings = policy.for_root(root)[0]
    extractions_home = policy.validate_instance_roots(root)["extractions_home"]
    engine = {"version": VERSION, "sha256": fingerprint(root, format_name)}
    run = extract.digest(extract.encoded({"source": identity, "extractor": engine, "format": format_name}))
    payload = _parse(root, format_name, path, identity, engine)
    value = {"schema_version": extract.VERSION, "run_id": run, "extractor": engine,
             "source": identity, "boundary": extract.BOUNDARY + " " + settings["handling"], "payload": payload}
    output = root / extractions_home / receipt / run / "extraction.json"
    return state, original, output, value


def unchanged(root, value):
    path = catalog.safe_path(root / value["source"]["path"])
    extract.require(extract.digest(_read(path)) == value["source"]["sha256"],
                    "Source changed during extraction/registration")
    extract.require(fingerprint(root, value["payload"]["conversion"]["source_format"]) == value["extractor"]["sha256"],
                    "Extractor changed during operation")


def registration_records(root, original, path, value):
    relative = path.relative_to(root).as_posix()
    raw = extract.encoded(value)
    ref = extract.reference(relative, extract.digest(raw))
    parent = deepcopy(original)
    data = parent["data"]
    data.update(sha256=value["source"]["sha256"], hash_basis="observed", bytes=value["source"]["bytes"],
                extraction="existing-rendition", coverage="deferred",
                reason="Markdown text converted from the original in a derived machine rendition; "
                       "layout and embedded objects are not preserved; review/indexing remain separate.")
    if ref not in data["provenance"]:
        data["provenance"].append(ref)
    identifier = "A-extract-" + value["run_id"]
    child = {"id": identifier, "kind": "artifact", "data": {
        "path": relative, "parent_id": original["id"], "receipt_id": value["source"]["receipt_id"],
        "role": "generated-rendition", "audience": data["audience"], "coverage": "deferred",
        "reason": "Machine-converted Markdown rendition; inherits source restrictions; not reviewed, "
                  "indexed or independent evidence.",
        "presence": "present", "sha256": ref["sha256"], "hash_basis": "observed",
        "bytes": len(raw), "extraction": "not-required",
        "handling_refs": deepcopy(data["handling_refs"]),
        "provenance": [extract.reference(value["source"]["path"], value["source"]["sha256"]), ref],
    }}
    relation = {"id": "R-extract-" + value["run_id"], "kind": "relationship", "data": {
        "from_id": identifier, "to_id": original["id"], "relation": "rendition-of",
        "scope": "Machine-converted Markdown text; not visual equivalence, independent evidence, review "
                 "or acceptance.",
        "basis": "reported", "provenance": [ref],
    }}
    return [parent, child, relation]


def registration_recipe(root, original, path, value, state, actor, registered=lambda: False):
    """``extract.registration_recipe`` for a converted rendition (same saved-request guarantees)."""
    recipe = {
        "actor": actor, "request_id": "extract-" + value["source"]["receipt_id"] + "-" + value["run_id"],
        "reason": "Explicit machine-conversion registration; no review, index or intake-stage change.",
        "records": registration_records(root, original, path, value),
    }
    catalog.validate_records(recipe["records"])
    saved_path = path.parent / "registration.json"
    if saved_path.exists():
        saved = catalog.decode(catalog.private_file(saved_path).read_text())
        extract.require(set(saved) == set(recipe) and all(
            saved[key] == recipe[key] for key in ("actor", "request_id", "reason")),
            "Saved registration differs in actor, request or reason")
        catalog.validate_records(saved["records"])
        if saved != recipe:
            history = catalog.read_records(
                root, kind="artifact", path=value["source"]["path"],
                include_history=True, limit=1, expected_state=state["state_token"])
            extract.require(registered() or any(version["request_id"] == recipe["request_id"]
                                                for record in history["records"] if record["id"] == original["id"]
                                                for version in record["versions"]),
                            "Uncommitted registration metadata drift; preserve the saved request and reconcile")
            recipe = saved
    else:
        extract.publish(saved_path, extract.encoded(recipe))
    return recipe


def finish_registration(root, path, value, recipe, outcome):
    try:
        completion = path.parent / "completion.json"
        if not completion.exists():
            extract.publish(completion, extract.encoded(outcome))
        else:
            saved = catalog.decode(catalog.private_file(completion).read_text())
            extract.require(saved["request_id"] == recipe["request_id"]
                            and saved["committed_state"] == outcome["committed_state"],
                            "Committed registration differs from saved completion")
        unchanged(root, value)
    except (extract.ExtractionError, catalog.CatalogError, OSError, ValueError, KeyError) as error:
        raise extract.ExtractionError("Catalog registration committed, but completion/final check failed; "
                                      "preserve registration.json and retry the same request: " + str(error)) from error


def _checked(root, receipt, format_name, prepared):
    state, original, path, value = prepared
    extract.require(value["source"]["receipt_id"] == receipt
                    and value["payload"]["conversion"]["source_format"] == format_name
                    and path.is_relative_to(root), "Prepared bundle is for a different receipt, format or root")
    raw = extract.encoded(value)
    require(len(raw) <= extract.MAX_EXPANDED, "conversion-limit", "Complete rendition exceeds output limit")
    return state, original, path, value, raw


def _result(command, receipt, path, value, raw, root, state, created):
    return {"command": command, "receipt_id": receipt, "run_id": value["run_id"],
            "rendition": path.relative_to(root).as_posix(), "created": created,
            "source_sha256": value["source"]["sha256"], "rendition_sha256": extract.digest(raw),
            "catalog_state": state, "boundary": extract.BOUNDARY, "warnings": value["payload"]["warnings"]}


def register_batch(root, items, *, rebase=True):
    """``extract.register_batch`` for converted renditions: one catalog revision per (actor, reason)."""
    root = catalog.safe_path(root)
    outcomes = [None] * len(items)
    with catalog.writer_lock(root / extract.HOME):
        ready = []
        for index, item in enumerate(items):
            try:
                state, original, path, value, raw = _checked(root, item["receipt"], item["format_name"],
                                                             item["prepared"])
                extract.require(item["actor"] is not None and item["expected_state"] is not None,
                                "Registration requires actor and explicit expected catalog state")
                extract.require(path.exists(), "Prepared rendition missing; run prepare for this source/extractor version")
                extract.require(catalog.private_file(path).read_bytes() == raw,
                                "Rendition differs from repeat extraction; preserve it and investigate")
                unchanged(root, value)
                result = _result("register", item["receipt"], path, value, raw, root, state, False)
                marker = extract.registration_marker(value)
                recipe = registration_recipe(root, original, path, value, state, item["actor"],
                                             registered=lambda marker=marker: catalog.marker_present(root, marker))
                unchanged(root, value)
            except extract.REGISTER_ERRORS as error:
                outcomes[index] = error
                continue
            ready.append((index, item, path, value, recipe, result))
        groups = {}
        for entry in ready:
            groups.setdefault((entry[4]["actor"], entry[4]["reason"]), []).append(entry)
        for (actor, reason), entries in groups.items():
            members = [{"marker": extract.registration_marker(value), "records": recipe["records"],
                        "expected_state": item["expected_state"]} for _, item, _, value, recipe, _ in entries]
            try:
                submitted = catalog.submit_batch(root, members, actor=actor, reason=reason, rebase=rebase)
            except extract.REGISTER_ERRORS as error:
                for entry in entries:
                    outcomes[entry[0]] = error
                continue
            for (index, item, path, value, recipe, result), member in zip(entries, submitted["members"]):
                if member["status"] == "refused":
                    outcomes[index] = member["error"]
                    continue
                outcome = {"command": "submit", "idempotent": member["status"] == "present",
                           **member["committed_state"], "state": member["committed_state"],
                           "request_id": recipe["request_id"], "committed_revision": member["committed_revision"],
                           "committed_state": member["committed_state"], "backup": None, "retention": None}
                try:
                    finish_registration(root, path, value, recipe, outcome)
                except extract.REGISTER_ERRORS as error:
                    outcomes[index] = error
                    continue
                result.update(catalog_state=outcome["state"], registered=True, idempotent=outcome["idempotent"],
                              request_id=recipe["request_id"], committed_revision=member["committed_revision"])
                outcomes[index] = result
    return outcomes


def run(root, command, receipt, format_name, actor=None, expected_state=None, rebase=False, prepared=None):
    """``extract.run`` for a converted rendition: prepare, verify or register one receipt."""
    root = catalog.safe_path(root)
    if prepared is None:
        prepared = bundle(root, receipt, format_name)
    state, original, path, value, raw = _checked(root, receipt, format_name, prepared)
    with catalog.writer_lock(root / extract.HOME):
        if command == "prepare":
            catalog.private_directory(path.parent.parent)
            catalog.private_directory(path.parent)
            unchanged(root, value)
            created = extract.publish(path, raw)
        else:
            extract.require(path.exists(), "Prepared rendition missing; run prepare for this source/extractor version")
            extract.require(catalog.private_file(path).read_bytes() == raw,
                            "Rendition differs from repeat extraction; preserve it and investigate")
            created = False
        unchanged(root, value)
        result = _result(command, receipt, path, value, raw, root, state, created)
        if command == "register":
            extract.require(actor is not None and expected_state is not None,
                            "Registration requires actor and explicit expected catalog state")
            recipe = registration_recipe(
                root, original, path, value, state, actor,
                registered=lambda: catalog.marker_present(root, extract.registration_marker(value)))
            unchanged(root, value)
            outcome = catalog.submit(root, expected_state=expected_state, rebase=rebase,
                                     marker=extract.registration_marker(value), **recipe)
            finish_registration(root, path, value, recipe, outcome)
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
        command.add_argument("--format", choices=sorted(FORMATS), required=True)
        if name == "register":
            command.add_argument("--actor", required=True)
            command.add_argument("--expected-state", required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.root, args.command, args.receipt, args.format,
                     getattr(args, "actor", None), getattr(args, "expected_state", None))
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (extract.ExtractionError, catalog.CatalogError, OSError, ValueError, TypeError,
            KeyError, sqlite3.Error) as error:
        print("convert: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
