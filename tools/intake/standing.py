"""Explicit, versioned standing-policy review, qualification and selection.

A standing policy authorizes routine processing of pinned Docling renditions
without a per-file human click.  It never treats parser completion, model
success or confidence as verification: only a pinned, attributed findings
record that inspected every page, text record, table and warning of the exact
rendition can qualify it.  Every other result stays a visible, unpublished
outcome with explicit reasons.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile

from tools.catalog import catalog_store as catalog
from tools.context_index import context_index as index
from tools.context_index import qualification
from tools.context_index import renditions
from tools.review_events import event_reader
from tools.review_events import event_store as events
from .contracts import IntakeError, canonical, pairs

POLICY_SCHEMA = "standing-policy-v1"
FINDINGS_SCHEMA = "standing-findings-v1"
OUTCOMES = ("qualified", "pending-review", "rejected", "deferred", "exception")
LIMIT = 1024 * 1024


class Refusal(Exception):
    """A durable stage refusal mapped by adapters onto StageFailure."""

    def __init__(self, category, code):
        self.category, self.code = category, code
        super().__init__(code)


def _identifier(value, pattern=r"[a-z0-9][a-z0-9-]{0,99}"):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and value.strip() != ""


def _texts(value, limit=50):
    return isinstance(value, list) and len(value) <= limit and all(_text(v, 1000) for v in value)


def _read(root, name, limit):
    """Read one private (0600) regular file below the root without following symlinks."""
    try:
        path = catalog.safe_path(Path(root) / name)
    except catalog.CatalogError as error:
        raise Refusal("blocked", "standing-path") from error
    if not path.exists():
        raise FileNotFoundError(name)
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise Refusal("blocked", "standing-private-file")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    return path, raw


def _decode(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))


def load_policy(root, name, expected_sha):
    """Return the exact pinned standing policy or refuse durably."""
    try:
        _, raw = _read(root, name, 64 * 1024)
    except FileNotFoundError as error:
        raise Refusal("blocked", "standing-policy-missing") from error
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise Refusal("blocked", "standing-policy-changed")
    try:
        value = _decode(raw)
    except (UnicodeError, ValueError, IntakeError) as error:
        raise Refusal("blocked", "standing-policy-invalid") from error
    valid = (isinstance(value, dict) and set(value) == {
        "schema", "policy_id", "version", "status", "reviewer", "scope", "budget", "selection"}
        and value["schema"] == POLICY_SCHEMA and _identifier(value["policy_id"])
        and type(value["version"]) is int and value["version"] > 0
        and value["status"] in {"active", "revoked"}
        and isinstance(value["reviewer"], str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", value["reviewer"]) is not None)
    if valid:
        scope, budget, selection = value["scope"], value["budget"], value["selection"]
        valid = (isinstance(scope, dict) and set(scope) == {"receipts", "audiences"}
                 and isinstance(scope["receipts"], list) and 0 < len(scope["receipts"]) <= 100
                 and all(_identifier(r, r"D0*[1-9][0-9]*") for r in scope["receipts"])
                 and _texts(scope["audiences"]) and scope["audiences"]
                 and isinstance(budget, dict)
                 and set(budget) == {"max_pages", "max_records", "max_findings_bytes"}
                 and all(type(budget[k]) is int for k in budget)
                 and 0 < budget["max_pages"] <= renditions.DOCLING_MAX_PAGES
                 and 0 < budget["max_records"] <= renditions.DOCLING_MAX_RECORDS
                 and 0 < budget["max_findings_bytes"] <= LIMIT
                 and isinstance(selection, dict) and set(selection) == {"context"}
                 and isinstance(selection["context"], list) and selection["context"])
    if not valid:
        raise Refusal("blocked", "standing-policy-invalid")
    try:
        index.validate_context(selection["context"], root)
    except index.IndexError_ as error:
        raise Refusal("blocked", "standing-policy-invalid") from error
    return value


def _findings_valid(value):
    if not (isinstance(value, dict) and set(value) == {
            "schema", "policy", "rendition", "rendition_sha256", "reviewer", "decision",
            "inspected", "explanation", "contradictions", "missing_evidence",
            "correction_of", "new_mapping", "exception_resolution"}):
        return False
    inspected = value["inspected"]
    return (value["schema"] == FINDINGS_SCHEMA
            and isinstance(value["policy"], dict) and set(value["policy"]) == {"policy_id", "version"}
            and value["decision"] in {"accept", "reject", "defer"}
            and isinstance(inspected, dict) and set(inspected) == {"pages", "texts", "tables", "warnings"}
            and all(isinstance(inspected[k], list) and len(inspected[k]) <= renditions.DOCLING_MAX_RECORDS
                    for k in inspected)
            and all(type(p) is int for p in inspected["pages"])
            and all(isinstance(r, str) for k in ("texts", "tables", "warnings") for r in inspected[k])
            and _text(value["explanation"], 2000)
            and _texts(value["contradictions"]) and _texts(value["missing_evidence"])
            and (value["correction_of"] is None or _identifier(value["correction_of"], r"rev-[a-z0-9-]{1,100}"))
            and type(value["new_mapping"]) is bool
            and (value["exception_resolution"] is None or _text(value["exception_resolution"], 1000)))


def _rendition(root, args, engine):
    """Read the pinned rendition and confirm its original is unchanged."""
    name = args["rendition"]
    if not renditions.eligible(name, root):
        raise Refusal("blocked", "standing-rendition-path")
    try:
        path, raw = _read(root, name, 64 * 1024 * 1024)
    except FileNotFoundError as error:
        raise Refusal("blocked", "stale-rendition") from error
    if hashlib.sha256(raw).hexdigest() != args["rendition_sha256"]:
        raise Refusal("blocked", "stale-rendition")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=index.strict_object)
        engine.fields(value, ("schema", "run_id", "extractor", "source", "boundary", "conversion"),
                      "Docling extraction")
        source = value["source"]
        engine.fields(source, ("receipt_id", "artifact_id", "source_ids", "path", "sha256", "bytes",
                               "audience", "handling_refs"), "Extraction source")
        if (value["schema"] != "docling-rendition-v1"
                or PurePosixPath(name).parts[-2] != value["run_id"]
                or PurePosixPath(name).parts[-3] != source["receipt_id"]):
            engine.fail("Docling identity mismatch")
    except (UnicodeError, ValueError, index.IndexError_):
        return raw, None, "rendition-fidelity"
    original = catalog.safe_path(Path(root) / index.relative_name(source["path"]))
    if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != source["sha256"]:
        raise Refusal("blocked", "stale-source")
    return raw, value, None


def _registered(root, value, args):
    """Confirm the catalog registration binds the exact rendition and original."""
    source = value["source"]
    try:
        metadata = catalog.receipt_metadata(root, [source["receipt_id"]])
        original = metadata["registrations"][source["receipt_id"]]["artifact"]
        records = catalog.read_records(root, receipt=source["receipt_id"], limit=100,
                                       expected_state=metadata["state"]["state_token"])
    except catalog.CatalogError:
        return None, None
    by_id = {record["id"]: record for record in records["records"]}
    child = by_id.get("A-docling-" + value["run_id"])
    relation = by_id.get("R-docling-" + value["run_id"], {}).get("data", {})
    data = original["data"]
    if (child is None or child["data"].get("path") != args["rendition"]
            or child["data"].get("sha256") != args["rendition_sha256"]
            or child["data"].get("parent_id") != original["id"]
            or child["data"].get("role") != "generated-rendition"
            or child["data"].get("presence") != "present"
            or relation.get("from_id") != child["id"] or relation.get("to_id") != original["id"]
            or relation.get("relation") != "rendition-of"
            or data["sha256"] != source["sha256"] or data["path"] != source["path"]
            or data["presence"] != "present" or data["coverage"] == "excluded"):
        return None, None
    return child, original


def evaluate(root, args):
    """Classify one pinned rendition under one pinned standing policy; never writes."""
    root = Path(root)
    standing = load_policy(root, args["policy_path"], args["policy_sha256"])
    pinned = {"policy_id": standing["policy_id"], "version": standing["version"],
              "sha256": args["policy_sha256"]}
    result = {"policy": pinned, "rendition": args["rendition"],
              "rendition_sha256": args["rendition_sha256"],
              "findings_sha256": args["findings_sha256"], "reasons": [], "inspected": None}

    def outcome(name, *reasons):
        result.update(outcome=name, reasons=list(reasons))
        return result

    if standing["status"] != "active":
        return outcome("exception", "policy-revoked")
    try:
        _, raw = _read(root, args["findings_path"], LIMIT)
    except FileNotFoundError as error:
        # The authorized reviewer has not produced findings: the route is unavailable.
        raise Refusal("retryable", "review-route-unavailable") from error
    if len(raw) > standing["budget"]["max_findings_bytes"]:
        return outcome("exception", "budget-exceeded")
    if hashlib.sha256(raw).hexdigest() != args["findings_sha256"]:
        raise Refusal("blocked", "stale-findings")
    try:
        findings = _decode(raw)
    except (UnicodeError, ValueError, IntakeError):
        return outcome("exception", "invalid-findings")
    if not _findings_valid(findings):
        return outcome("exception", "invalid-findings")
    result["explanation"] = findings["explanation"]
    result["exception_resolution"] = findings["exception_resolution"]
    if (findings["policy"] != {"policy_id": standing["policy_id"], "version": standing["version"]}
            or findings["rendition"] != args["rendition"]
            or findings["rendition_sha256"] != args["rendition_sha256"]
            or findings["reviewer"] != standing["reviewer"]):
        return outcome("exception", "findings-mismatch")
    engine = index.rendition_api(root)
    rendition_raw, value, failure = _rendition(root, args, engine)
    if failure:
        return outcome("exception", failure)
    source = value["source"]
    if (source["receipt_id"] not in standing["scope"]["receipts"]
            or source["audience"] not in standing["scope"]["audiences"]):
        return outcome("exception", "prohibited-scope")
    child, original = _registered(root, value, args)
    if child is None:
        return outcome("exception", "unregistered-rendition")
    conversion = value["conversion"]
    try:
        records = list(renditions.docling_records(rendition_raw, value, engine))
    except (index.IndexError_, TypeError, AttributeError, KeyError):
        return outcome("exception", "rendition-fidelity")
    document = conversion["document"]
    expected = {"pages": sorted(page["page_no"] for page in conversion["pages"]),
                "texts": sorted(item["self_ref"] for item in document["texts"]),
                "tables": sorted(item["self_ref"] for item in document["tables"]),
                "warnings": sorted({str(w.get("code", "")) for w in conversion["warnings"]})}
    result["inspected"] = {key: len(items) for key, items in expected.items()}
    if (len(expected["pages"]) > standing["budget"]["max_pages"]
            or len(records) > standing["budget"]["max_records"]):
        return outcome("exception", "budget-exceeded")
    if findings["correction_of"] is not None or findings["new_mapping"]:
        return outcome("pending-review", "independent-review-required")
    try:
        snapshot = event_reader.read_snapshot(root)
    except events.contract.ReviewError as error:
        raise Refusal("blocked", "review-store-unavailable") from error
    others = [e for e in snapshot.events if e["source"]["locator"] == args["rendition"]
              and e["event_id"] != args["event_id"] and e["event_id"] not in snapshot.successors]
    if others:
        return outcome("pending-review", "conflicting-review")
    if findings["decision"] == "reject":
        return outcome("rejected", "reviewer-rejected")
    if findings["decision"] == "defer":
        return outcome("deferred", "reviewer-deferred")
    if findings["contradictions"] or findings["missing_evidence"]:
        return outcome("pending-review", "unresolved-evidence")
    inspected = {key: sorted(findings["inspected"][key]) for key in expected}
    if inspected != expected:
        return outcome("pending-review", "insufficient-inspected-scope")
    result.update(source=source, content=["tables", "text"] if expected["tables"] else ["text"],
                  reviewer=standing["reviewer"], context=standing["selection"]["context"],
                  child=child)
    return outcome("qualified", "complete-inspected-scope")


def refusal(result):
    """Visible non-qualified outcomes block durably with their reason."""
    if result["outcome"] != "qualified":
        raise Refusal("blocked", "standing-" + result["outcome"] + "-" + result["reasons"][0])


def review_event(result, args, recorded_at=None):
    recorded = recorded_at or datetime.now(timezone.utc).replace(microsecond=0)
    policy_ref = result["policy"]
    inspected = result["inspected"]
    statements = [
        f"Standing policy {policy_ref['policy_id']} v{policy_ref['version']} "
        f"(sha256 {policy_ref['sha256']}) authorized this exact rendition review.",
        f"Inspected all {inspected['pages']} pages, {inspected['texts']} text records, "
        f"{inspected['tables']} tables and {inspected['warnings']} conversion warnings "
        f"(findings sha256 {result['findings_sha256']}).",
        "Reviewer explanation: " + result["explanation"],
    ]
    if result["exception_resolution"]:
        statements.append("Exception resolution: " + result["exception_resolution"])
    source = result["source"]
    return {
        "event_id": args["event_id"],
        "source": {"receipt_id": source["receipt_id"], "source_id": source["source_ids"][0],
                   "locator": args["rendition"], "parent_locator": source["path"],
                   "checksum": {"sha256": args["rendition_sha256"], "basis": "reported"}},
        "scope": {"content": result["content"], "unit": "file", "extent": "defined_scope",
                  "coordinates": "not_applicable", "ranges": [],
                  "description": qualification.COMPLETE_SCOPE},
        "outcome": "reviewed",
        "counts": {"total": 1, "reviewed": 1, "pending": 0, "unreadable": 0, "dispositioned": 0},
        "reason": "Qualified under explicit standing policy; parser completion is not verification.",
        "reviewer": result["reviewer"], "reviewed_on": recorded.date().isoformat(),
        "recorded_at": recorded.isoformat(),
        "evidence": [{"kind": "agent_statement", "pointer": None, "quote": q} for q in statements],
        "uncertainties": [],
        "handling": [events.contract.HANDLING],
        "provenance": {"origin": "agent_review", "record": None,
                       "mapping_version": None, "unmapped_fields": []},
        "relationships": [], "supersedes": [], "correction_reason": None,
    }


def bound_event(root, result, args):
    """Return the stored active event when it binds exactly this qualification."""
    try:
        found = events.status(root, args["event_id"])
    except events.contract.ReviewError:
        return None
    event = found["event"]
    if (found["superseded_by"] is not None or event["outcome"] != "reviewed"
            or event["reviewer"] != result["reviewer"]
            or event["source"]["locator"] != args["rendition"]
            or event["source"]["checksum"] != {"sha256": args["rendition_sha256"], "basis": "reported"}
            or event["scope"]["content"] != result["content"]):
        return None
    return event


def selection_entry(result, args):
    source = result["source"]
    return {"path": args["rendition"], "sha256": args["rendition_sha256"],
            "receipt_id": source["receipt_id"], "format": "docling",
            "context": list(result["context"]), "review_event_ids": [args["event_id"]]}


def selected_child(result, args):
    child = json.loads(canonical(result["child"]))
    child["data"]["coverage"] = "indexed"
    child["data"]["reason"] = (
        f"Selected under standing policy {result['policy']['policy_id']} "
        f"v{result['policy']['version']} after exact review {args['event_id']}.")
    return child


def write_manifest(root, name, entry):
    """Atomically add one selection entry to the manifest; idempotent for retries."""
    path = index.no_symlinks(Path(root) / name)
    config, _ = index.load_manifest(path, root)
    raw = json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=index.strict_object)
    selected = raw.setdefault("renditions", [])
    current = [item for item in selected if item["path"] == entry["path"]]
    if current == [entry]:
        return index.digest(path.read_bytes())
    if current or len(selected) >= 10:
        raise Refusal("blocked", "selection-conflict")
    selected.append(entry)
    data = (json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix=".selection-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        index.load_manifest(Path(temporary), root)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return index.digest(data)


def manifest_entry(root, name, path):
    raw = json.loads((Path(root) / name).read_bytes().decode("utf-8"),
                     object_pairs_hook=index.strict_object)
    return [item for item in raw.get("renditions", []) if item["path"] == path]
