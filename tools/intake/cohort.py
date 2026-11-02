"""Read-only local cohort intake with explicit, counted dispositions.

Every file below a read-only inbox gets exactly one disposition. Admitted files
are submitted through manual upload intake as ``local-path`` copies (the inbox
is never written, moved or given a remote identity) and extracted through the
durable ledger. Extraction is not review: renditions stay deferred until a
standing-policy review qualifies them. Printed output is body-free counts.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import quote

from tools import policy
from tools.catalog import catalog_store as catalog
from tools.extraction import extract, mhtml
from . import runner, store, upload
from .contracts import IntakeError

ASSET_SUFFIXES = {".js", ".css", ".ico", ".map", ".woff", ".woff2", ".ttf", ".svg"}
RUNTIME_FORMATS = {".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image"}
EXPANDED_DIRECTORY = "_extracted"
REASON = "Read-only local cohort intake; copy only, not extraction review or qualification."
OUTCOMES = ("received", "extracted", "failed", "changed-since-intake", "not-processed")


def submission(relative, system, fmt=None):
    """Stable ASCII-safe submission identity, name and locator for one inbox path.

    Files read as MIME HTML get a distinct identity from the plain-text identity an
    earlier toolkit version may have used, so they are received and extracted again.
    """
    salt = "\0" + fmt if fmt == "mhtml" else ""
    key = hashlib.sha256((system + "\0" + relative + salt).encode("utf-8")).hexdigest()[:24]
    base = unicodedata.normalize("NFKD", relative.rsplit("/", 1)[-1])
    base = base.encode("ascii", "ignore").decode("ascii")
    stem, dot, suffix = base.rpartition(".") if "." in base else (base, "", "")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._") or "file"
    suffix = re.sub(r"[^A-Za-z0-9]+", "", suffix)
    name = (stem[:180] + (dot + suffix if suffix else "")).lstrip("-._")
    if name.endswith(upload.DESCRIPTOR_SUFFIX) or name == upload.SIDECAR:
        name = "file-" + name
    return {"submission_id": "inbox-" + key, "name": name,
            "locator": quote(relative, safe="/")}


def _format(path, relative, raw, root):
    """Return (format, refusal code); MIME content wins over any text suffix."""
    suffix = path.suffix.lower()
    if suffix == ".odt":
        return "odt", None
    if suffix in extract.TEXT_SUFFIXES and mhtml.recognised(raw):
        try:
            mhtml.payload(raw)
        except mhtml.MhtmlError as error:
            return None, error.code
        return "mhtml", None
    if suffix == ".txt":
        try:
            extract.gemini(raw, root)
            return "gemini-txt", None
        except (extract.ExtractionError, UnicodeDecodeError):
            return "text", None
    if suffix in extract.TEXT_SUFFIXES:
        return "text", None
    return None, None


def _disposition(root, inbox, path, relative):
    entry = {"relative": relative, "disposition": None, "format": None}
    name, suffix = path.name, path.suffix.lower()
    if path.is_symlink() or not path.is_file():
        return {**entry, "disposition": "unsafe-path"}
    size = path.stat(follow_symlinks=False).st_size
    entry["bytes"] = size
    if ":" in name:
        # Windows alternate-data-stream sidecars (Zone.Identifier, DLP tags).
        return {**entry, "disposition": "skipped-artifact"}
    if suffix in ASSET_SUFFIXES:
        return {**entry, "disposition": "skipped-asset"}
    if suffix == ".zip":
        expanded = path.parent / EXPANDED_DIRECTORY / path.stem
        return {**entry, "disposition": "archive-expanded" if expanded.is_dir() else "needs-reader"}
    if size == 0:
        return {**entry, "disposition": "empty"}
    if suffix in RUNTIME_FORMATS:
        fmt = RUNTIME_FORMATS[suffix]
        over = size > upload.FORMATS[fmt][1]
        return {**entry, "format": fmt, "disposition": "over-limit" if over else "needs-runtime"}
    if suffix not in extract.TEXT_SUFFIXES | {".odt"}:
        return {**entry, "disposition": "needs-reader"}
    limit = max(upload.FORMATS[f][1] for f in ("text", "gemini-txt", "mhtml", "odt"))
    if size > limit:
        return {**entry, "disposition": "over-limit"}
    try:
        raw = upload.read_file(path, limit)
    except IntakeError as error:
        return {**entry, "disposition": "unsafe-path", "code": error.code}
    fmt, refusal = _format(path, relative, raw, root)
    entry.update(format=fmt, sha256=hashlib.sha256(raw).hexdigest())
    if refusal:
        # MIME that cannot be read strictly is never admitted as raw encoded text.
        return {**entry, "disposition": "needs-reader", "code": refusal}
    if size > upload.FORMATS[fmt][1]:
        return {**entry, "disposition": "over-limit"}
    try:
        upload.check_type(raw, fmt, name)
    except IntakeError as error:
        return {**entry, "disposition": "unreadable", "code": error.code}
    return {**entry, "disposition": "admit"}


def classify(root, inbox):
    """Return one explicit disposition per inbox file; never writes the inbox."""
    root, inbox = Path(root).resolve(), Path(inbox).resolve()
    if root == inbox or inbox in root.parents:
        raise IntakeError("instance_inside_inbox")
    if not inbox.is_dir():
        raise IntakeError("inbox_unavailable")

    def unavailable(error):
        raise IntakeError("inbox_unavailable") from error
    entries = []
    for directory, names, files in os.walk(inbox, onerror=unavailable, followlinks=False):
        here = Path(directory)
        for name in [*files, *(n for n in names if (here / n).is_symlink())]:
            path = here / name
            relative = path.relative_to(inbox).as_posix()
            entries.append(_disposition(root, inbox, path, relative))
    return sorted(entries, key=lambda e: e["relative"])


def _request(root, correlation_id, stage_id, operation, arguments):
    """Stage one single-stage request unless it already exists; return its stage summary."""
    try:
        store.inspect(root, correlation_id)
    except IntakeError as error:
        if error.code != "unknown_request":
            raise
        store.stage(root, json.dumps({
            "version": 1, "correlation_id": correlation_id, "correction_of": None,
            "policy": {"version": 1, "sha256": policy.for_root(root)[1]},
            "retry": {"max_attempts": 3, "backoff_seconds": 0},
            "stages": [{"id": stage_id, "kind": "extraction", "operation": operation,
                        "arguments": arguments}]}).encode())
    stage = store.inspect(root, correlation_id)["stages"][0]
    if stage["status"] in {"staged", "running", "retryable"}:
        stage = runner.run(root, correlation_id, stage_id)["stages"][0]
    return stage


def _extract(root, entry, actor):
    base = {"receipt": entry["receipt_id"], "format_name": entry["format"]}
    # The reader version is part of the request identity: a new MIME reader version extracts again.
    version = "-m%d" % mhtml.METHOD_VERSION if entry["format"] == "mhtml" else ""
    stage = _request(root, "extract-" + entry["submission_id"] + version, "prepare", "extract-prepare", base)
    if stage["status"] == "succeeded":
        # Registration pins the catalog state when staged, immediately before it runs;
        # a repeated registration is idempotent by its saved request.
        stage = _request(root, "register-" + entry["submission_id"] + version, "register",
                         "extract-register", {**base, "actor": actor,
                                              "expected_state": catalog.status(root)["state_token"]})
    if stage["status"] != "succeeded":
        code = stage["attempts"][-1]["code"] if stage["attempts"] else stage["status"]
        return {"outcome": "failed", "stage": stage["id"], "code": code}
    evidence = stage["evidence"]
    return {"outcome": "extracted", "rendition": evidence["rendition"],
            "rendition_sha256": evidence["rendition_sha256"]}


def _process(root, inbox, entry, system, actor):
    identity = submission(entry["relative"], system, entry["format"])
    entry.update(identity)
    superseded = None
    if entry["format"] == "mhtml":
        # An entry ingested before MIME recognition (as plain text) is corrected, not kept.
        earlier = submission(entry["relative"], system)["submission_id"]
        if upload.ledger_request(root, upload.correlation(earlier)) is not None:
            superseded = earlier
    try:
        ack = upload.submit(root, Path(inbox) / entry["relative"], upload.descriptor(
            submission_id=identity["submission_id"], format_name=entry["format"],
            correction_of=superseded,
            name=identity["name"], actor=actor, reason=REASON, origin_kind="local-path",
            origin_system=system, origin_locator=identity["locator"],
            expected_sha256=entry["sha256"]))
    except IntakeError as error:
        outcome = "changed-since-intake" if error.code == "changed_input" else "failed"
        return {"outcome": outcome, "stage": "receipt", "code": error.code}
    entry.update(original=ack["original"]["path"], receipt_id=ack["records"]["receipt_id"])
    if ack["receipt"]["state"] != "committed":
        return {"outcome": "failed", "stage": "receipt",
                "code": ack["receipt"].get("code", ack["receipt"]["state"])}
    entry["received"] = True
    return _extract(root, entry, actor)


def ingest(root, inbox, *, system, actor, limit=None):
    """Classify the inbox, then submit and extract admitted files; return the report."""
    entries = classify(root, inbox)
    admitted = [e for e in entries if e["disposition"] == "admit"]
    outcome = Counter({name: 0 for name in OUTCOMES})
    for number, entry in enumerate(admitted):
        if limit is not None and number >= limit:
            entry["outcome"] = "not-processed"
            outcome["not-processed"] += 1
            continue
        entry.update(_process(root, inbox, entry, system, actor))
        outcome["received"] += bool(entry.get("received"))
        outcome[entry["outcome"]] += 1
    counts = {"files": len(entries),
              "disposition": dict(Counter(e["disposition"] for e in entries)),
              "format": dict(Counter(e["format"] for e in admitted)),
              "outcome": dict(outcome)}
    return {"counts": counts, "entries": entries}


def _write_private(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("classify", "ingest"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--inbox", required=True, type=Path)
        command.add_argument("--report", type=Path,
                             help="private (0600) per-file report with paths and hashes")
        if name == "ingest":
            command.add_argument("--system", required=True)
            command.add_argument("--actor", required=True)
            command.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest":
            report = ingest(args.root, args.inbox, system=args.system, actor=args.actor,
                            limit=args.limit)
        else:
            entries = classify(args.root, args.inbox)
            report = {"counts": {"files": len(entries),
                                 "disposition": dict(Counter(e["disposition"] for e in entries))},
                      "entries": entries}
    except IntakeError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 2
    if args.report:
        _write_private(args.report, report)
    print(json.dumps({"counts": report["counts"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
