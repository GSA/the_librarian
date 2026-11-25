"""Explicit, versioned standing-policy review, qualification and selection.

A standing policy authorizes routine processing of pinned Docling or
strict-text/transcript/ODT extraction renditions without a per-file human
click.  It never treats parser completion, model success or confidence as
verification: only a pinned, attributed findings record that inspected every
page, text record, table and warning (Docling) or every extraction record and
warning (text, transcript, ODT) of the exact rendition can qualify it.  Every other result stays a visible, unpublished
outcome with explicit reasons.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat

from tools.catalog import catalog_store as catalog
from tools.context_index import context_index as index
from tools.context_index import renditions
from tools.review_events import event_reader
from tools.review_events import event_store as events
from . import standing_events
from .contracts import IntakeError, pairs

POLICY_SCHEMA = "standing-policy-v1"
# v2 pins the review ``criteria`` (the reviewer prompt) and names earlier versions of the same
# policy whose qualified reviews it ``carries`` forward; a v1 policy is the v1 criteria, carrying none.
POLICY_SCHEMA_V2 = "standing-policy-v2"
POLICY_SCHEMAS = (POLICY_SCHEMA, POLICY_SCHEMA_V2)
CRITERIA = ("standing-review-v1", "standing-review-v2")
# v2 adds the advisory, non-blocking `unverifiable_references` list; v1 findings
# referenced by existing review events stay valid. v3 (written under the v2 criteria) adds the
# reviewer's `embedded_instructions` and `sparse` flags.
FINDINGS_SCHEMA = "standing-findings-v2"
FINDINGS_SCHEMA_V3 = "standing-findings-v3"
FINDINGS_SCHEMAS = {"standing-findings-v1": frozenset(),
                    FINDINGS_SCHEMA: frozenset({"unverifiable_references"}),
                    FINDINGS_SCHEMA_V3: frozenset({"unverifiable_references", "embedded_instructions",
                                                   "sparse"})}
# The findings schema each review criteria writes.
CRITERIA_FINDINGS = {"standing-review-v1": FINDINGS_SCHEMA, "standing-review-v2": FINDINGS_SCHEMA_V3}
OUTCOMES = ("qualified", "pending-review", "rejected", "deferred", "exception")
LIMIT = 1024 * 1024
# How a reviewer ``defer`` ends: ``hold`` (the default) keeps it a visible, unpublished ``deferred``
# outcome; ``qualify-low-confidence`` (an explicit operator opt-in recorded as the ``deferred``
# argument of the standing request) qualifies a completely inspected deferral as low confidence.
DEFERRED_MODES = ("hold", "qualify-low-confidence")
# A model review of a rendition too large for one call is made in contiguous parts (see
# ``model_review.plan``); the findings record each part. ``max_records`` and ``max_pages`` of a
# policy budget then apply to each part, and a rendition needing more than this many parts is over budget.
MAX_PARTS = 64
MAX_REVIEW_RECORDS = MAX_PARTS * renditions.DOCLING_MAX_RECORDS
DEFAULT_MAX_RECORDS = renditions.DOCLING_MAX_RECORDS  # the budget a cohort policy gets
DEFAULT_MAX_PAGES = renditions.DOCLING_MAX_PAGES
DECISION_ORDER = ("accept", "defer", "reject")  # least to most severe
DOCLING_INSPECTED = frozenset({"pages", "texts", "tables", "warnings"})
EXTRACT_INSPECTED = frozenset({"records", "warnings"})


class Refusal(Exception):
    """A durable stage refusal mapped by adapters onto StageFailure."""

    def __init__(self, category, code):
        self.category, self.code = category, code
        super().__init__(code)


class CatalogUnavailable(RuntimeError):
    """The catalog could not be read for a reason other than an absent record.

    Deliberately not a ``CatalogError``: adapters save those as durable stage failures, and an
    unreadable catalog (such as a validator changed under this process) says nothing about the
    rendition, so it stops the run instead and a restarted run re-checks the same stage.
    """
    code = "catalog_unavailable"


def _identifier(value, pattern=r"[a-z0-9][a-z0-9-]{0,99}"):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and value.strip() != ""


def _texts(value, limit=50):
    return isinstance(value, list) and len(value) <= limit and all(_text(v, 1000) for v in value)


def read_private(root, name, limit):
    """Read (up to ``limit + 1`` bytes of) one private (0600) regular file below the root without
    following symlinks."""
    try:
        path = catalog.safe_path(Path(root) / name)
    except catalog.CatalogError as error:
        raise Refusal("blocked", "standing-path") from error
    if not path.exists():
        raise FileNotFoundError(name)
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise Refusal("blocked", "standing-private-file")
    with path.open("rb") as stream:
        return stream.read(limit + 1)


def _decode(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))


def _scope_valid(scope):
    return (isinstance(scope, dict) and set(scope) == {"receipts", "audiences"}
            and isinstance(scope["receipts"], list) and 0 < len(scope["receipts"]) <= renditions.MAX_SELECTIONS
            and all(r == ANY_RECEIPT or _identifier(r, RECEIPT) for r in scope["receipts"])
            and _texts(scope["audiences"]) and bool(scope["audiences"]))


def _budget_valid(budget):
    return (isinstance(budget, dict)
            and set(budget) == {"max_pages", "max_records", "max_findings_bytes"}
            and all(type(budget[k]) is int for k in budget)
            and 0 < budget["max_pages"] <= renditions.DOCLING_MAX_PAGES
            and 0 < budget["max_records"] <= renditions.DOCLING_MAX_RECORDS
            and 0 < budget["max_findings_bytes"] <= LIMIT)


POLICY_KEYS = frozenset({"schema", "policy_id", "version", "status", "reviewer", "scope", "budget",
                         "selection"})


def _carries_valid(carries, version):
    return (isinstance(carries, list) and len(carries) <= 20
            and all(isinstance(c, dict) and set(c) == {"version", "sha256"}
                    and type(c["version"]) is int and 0 < c["version"] < version
                    and isinstance(c["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", c["sha256"])
                    for c in carries)
            and len({c["sha256"] for c in carries}) == len(carries))


def _policy_valid(value):
    if not isinstance(value, dict):
        return False
    if value.get("schema") == POLICY_SCHEMA_V2:
        if (set(value) != POLICY_KEYS | {"criteria", "carries"} or value["criteria"] not in CRITERIA
                or type(value.get("version")) is not int
                or not _carries_valid(value["carries"], value["version"])):
            return False
    elif value.get("schema") != POLICY_SCHEMA or set(value) != POLICY_KEYS:
        return False
    return (_identifier(value["policy_id"])
            and type(value["version"]) is int and value["version"] > 0
            and isinstance(value["status"], str) and value["status"] in {"active", "revoked"}
            and isinstance(value["reviewer"], str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", value["reviewer"]) is not None
            and _scope_valid(value["scope"]) and _budget_valid(value["budget"])
            and isinstance(value["selection"], dict) and set(value["selection"]) == {"context"}
            and isinstance(value["selection"]["context"], list) and bool(value["selection"]["context"]))


def load_policy(root, name, expected_sha):
    """Return the exact pinned standing policy or refuse durably."""
    try:
        raw = read_private(root, name, 64 * 1024)
    except FileNotFoundError as error:
        raise Refusal("blocked", "standing-policy-missing") from error
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise Refusal("blocked", "standing-policy-changed")
    try:
        value = _decode(raw)
    except (UnicodeError, ValueError, IntakeError) as error:
        raise Refusal("blocked", "standing-policy-invalid") from error
    if not _policy_valid(value):
        raise Refusal("blocked", "standing-policy-invalid")
    try:
        index.validate_context(value["selection"]["context"], root)
    except index.IndexError_ as error:
        raise Refusal("blocked", "standing-policy-invalid") from error
    return value


def criteria(standing):
    """The review criteria a loaded policy pins (a v1 policy: the original criteria)."""
    return standing.get("criteria", CRITERIA[0])


def carried(standing):
    """``(policy_id, version, sha256)`` of each earlier version whose qualified reviews this policy
    carries forward: publication accepts their selections and ``qualify`` reuses them unreviewed."""
    return [(standing["policy_id"], c["version"], c["sha256"]) for c in standing.get("carries", [])]


RECEIPT = r"D0*[1-9][0-9]*"
# A scope entry that stands for every upload receipt (``D<number>``), so a policy need not list IDs
# that grow with the instance. Audiences are still listed.
ANY_RECEIPT = "D*"


def permits(scope, receipt_id, audience):
    """True when the policy scope covers this receipt and audience; the one scope rule."""
    receipts = scope["receipts"]
    return ((receipt_id in receipts
             or (ANY_RECEIPT in receipts and re.fullmatch(RECEIPT, receipt_id) is not None))
            and audience in scope["audiences"])


def refused_scope(root, standing, rendition, rendition_sha256):
    """True when ``evaluate`` would end this rendition as ``prohibited-scope``, so no review is needed.

    The rendition is validated exactly as ``evaluate`` does before it checks scope; one that
    does not validate, or cannot be read, is not judged here and takes the ordinary path.
    """
    try:
        _, value, failure = _rendition(
            root, {"rendition": rendition, "rendition_sha256": rendition_sha256},
            index.rendition_api(root))
    except Refusal:
        return False
    return failure is None and not permits(
        standing["scope"], value["source"]["receipt_id"], value["source"]["audience"])


def expand_records(items, limit):
    """Expand ``inspected_records``: integers and inclusive ``"first-last"`` strings.

    Returns the indices in the order given, or None when an item is malformed, a range runs
    backwards, or the expansion would exceed ``limit`` indices (the rendition's record count),
    so a hostile range cannot allocate. Overlaps and repeats surface as duplicates, which the
    caller rejects exactly as for a plain list.
    """
    if not isinstance(items, list):
        return None
    out = []
    for item in items:
        if type(item) is int:
            out.append(item)
            continue
        found = re.fullmatch(r"\s*([0-9]{1,9})\s*-\s*([0-9]{1,9})\s*", item) if isinstance(item, str) else None
        if not found:
            return None
        first, last = int(found[1]), int(found[2])
        if last < first or last - first + 1 > limit or len(out) + last - first + 1 > limit:
            return None
        out.extend(range(first, last + 1))
    return out if len(out) <= limit else None


def compress_records(indices):
    """``indices`` (distinct integers) as ascending runs: a lone index, or a ``"first-last"`` string."""
    out, run = [], None
    for i in sorted(indices):
        if run is not None and i == run[1] + 1:
            run[1] = i
            continue
        if run is not None:
            out.append(run[0] if run[0] == run[1] else f"{run[0]}-{run[1]}")
        run = [i, i]
    if run is not None:
        out.append(run[0] if run[0] == run[1] else f"{run[0]}-{run[1]}")
    return out


def _inspected_valid(inspected):
    if not (isinstance(inspected, dict) and set(inspected) in (DOCLING_INSPECTED, EXTRACT_INSPECTED)):
        return False
    records = inspected.get("records")
    # Records may be written as ranges (a part-reviewed rendition lists tens of thousands); every
    # other list is bounded as before.
    expanded = expand_records(records, MAX_REVIEW_RECORDS) if records is not None else []
    return (expanded is not None and len(set(expanded)) == len(expanded)
            and all(isinstance(inspected[k], list) and (k == "records" and len(records) <= MAX_REVIEW_RECORDS
                                                        or len(inspected[k]) <= renditions.DOCLING_MAX_RECORDS)
                    for k in inspected)
            and all(type(p) is int for p in inspected.get("pages", []))
            and all(isinstance(r, str) for k in ("texts", "tables", "warnings")
                    for r in inspected.get(k, [])))


def _count(value, limit=1 << 40):
    return type(value) is int and 0 <= value <= limit


def _parts_valid(parts):
    return (isinstance(parts, list) and 0 < len(parts) <= MAX_PARTS
            and all(isinstance(p, dict) and set(p) == {"first", "last", "warnings", "decision", "inspected"}
                    and _count(p["first"]) and _count(p["last"]) and _count(p["warnings"])
                    and p["last"] >= p["first"] and p["decision"] in DECISION_ORDER
                    and isinstance(p["inspected"], dict) and set(p["inspected"]) == {"records", "warnings"}
                    and _count(p["inspected"]["records"]) and _count(p["inspected"]["warnings"])
                    for p in parts))


def _reductions_valid(value):
    keys = {"count", "bytes", "images"}
    return (isinstance(value, dict) and keys <= set(value) <= keys | {"more"}
            and _count(value["count"]) and _count(value["bytes"]) and _count(value.get("more", 0))
            and isinstance(value["images"], list) and len(value["images"]) <= 20
            and all(isinstance(i, dict) and set(i) == {"record", "mime", "bytes", "sha256"}
                    and _count(i["record"]) and _count(i["bytes"]) and _text(i["mime"], 200)
                    and isinstance(i["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", i["sha256"])
                    for i in value["images"]))


# Present only when a review needed them: ``parts`` (one entry per contiguous part of a rendition
# reviewed in parts) and ``reductions`` (inline data replaced by placeholders in the review prompt).
OPTIONAL_FINDINGS = frozenset({"parts", "reductions"})


def _findings_valid(value):
    if not (isinstance(value, dict) and isinstance(value.get("schema"), str)
            and value["schema"] in FINDINGS_SCHEMAS and set(value) == {
            "schema", "policy", "rendition", "rendition_sha256", "reviewer", "decision",
            "inspected", "explanation", "contradictions", "missing_evidence",
            "correction_of", "new_mapping", "exception_resolution"}
            | FINDINGS_SCHEMAS[value["schema"]] | (set(value) & OPTIONAL_FINDINGS)):
        return False
    return (("parts" not in value or _parts_valid(value["parts"]))
            and ("reductions" not in value or _reductions_valid(value["reductions"]))
            and _texts(value.get("unverifiable_references", []))
            and _texts(value.get("embedded_instructions", []))
            and type(value.get("sparse", False)) is bool
            and isinstance(value["policy"], dict) and set(value["policy"]) == {"policy_id", "version"}
            and isinstance(value["decision"], str) and value["decision"] in {"accept", "reject", "defer"}
            and _inspected_valid(value["inspected"])
            and _text(value["explanation"], 2000)
            and _texts(value["contradictions"]) and _texts(value["missing_evidence"])
            and (value["correction_of"] is None or _identifier(value["correction_of"], r"rev-[a-z0-9-]{1,100}"))
            and type(value["new_mapping"]) is bool
            and (value["exception_resolution"] is None or _text(value["exception_resolution"], 1000)))


def _parse_rendition(name, raw, engine):
    """The rendition's JSON once its shape and path identity validate; raises when they do not."""
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=index.strict_object)
    if isinstance(value, dict) and "payload" in value:
        engine.fields(value, ("schema_version", "run_id", "extractor", "source", "boundary",
                              "payload"), "Extraction")
        payload = value["payload"]
        if (value["schema_version"] != 1 or not isinstance(payload, dict)
                or payload.get("format") not in EXTRACT_FORMATS
                or not isinstance(payload.get("warnings"), list)):
            engine.fail("Unsupported extraction schema")
    else:
        engine.fields(value, ("schema", "run_id", "extractor", "source", "boundary",
                              "conversion"), "Docling extraction")
        if value["schema"] != "docling-rendition-v1":
            engine.fail("Docling identity mismatch")
    source = value["source"]
    engine.fields(source, ("receipt_id", "artifact_id", "source_ids", "path", "sha256", "bytes",
                           "audience", "handling_refs"), "Extraction source")
    if (PurePosixPath(name).parts[-2] != value["run_id"]
            or PurePosixPath(name).parts[-3] != source["receipt_id"]):
        engine.fail("Docling identity mismatch")
    return value


def _rendition(root, args, engine):
    """Read the pinned rendition and confirm its original is unchanged."""
    name = args["rendition"]
    if not renditions.eligible(name, root):
        raise Refusal("blocked", "standing-rendition-path")
    try:
        raw = read_private(root, name, 64 * 1024 * 1024)
    except FileNotFoundError as error:
        raise Refusal("blocked", "stale-rendition") from error
    if hashlib.sha256(raw).hexdigest() != args["rendition_sha256"]:
        raise Refusal("blocked", "stale-rendition")
    try:
        value = _parse_rendition(name, raw, engine)
    except (UnicodeError, ValueError, index.IndexError_):
        return raw, None, "rendition-fidelity"
    source = value["source"]
    if not engine.original_allowed(source["path"]):
        raise Refusal("blocked", "prohibited-source")
    original = catalog.safe_path(Path(root) / index.relative_name(source["path"]))
    if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != source["sha256"]:
        raise Refusal("blocked", "stale-source")
    if "payload" in value:
        try:
            if value["payload"]["format"] == "text":
                renditions.validate_text_mapping(value["payload"], original.read_bytes(), engine)
            elif value["payload"]["format"] == "mhtml":
                renditions.validate_mhtml(value["payload"], original.read_bytes(), engine)
        except (index.IndexError_, TypeError, KeyError):
            return raw, None, "rendition-fidelity"
    return raw, value, None


EXTRACT_FORMATS = {"text": "lines", "gemini-txt": "lines", "mhtml": "lines", "odt": "nodes",
                   "pdf-text": "lines", "ocr": "lines", "converted": "lines", "pdf-pages": "lines"}


def _kind(value):
    return "extract" if "payload" in value else "docling"


# Re-reads of one receipt when a commit lands between its two catalog reads.
STALE_REREADS = 3


def _registration_reads(root, receipt_id):
    """The receipt's metadata and records from one catalog state, or None when it is not there.

    Only an absent receipt or record means unregistered. A commit landing between the reads is
    re-read and, if it persists, retryable; so is a SQLite or filesystem error the runner would
    retry. Any other catalog failure (a changed validator, a malformed database, a permission
    error) raises ``CatalogUnavailable`` instead of being recorded as an outcome.
    """
    try:
        catalog.identifier(receipt_id, "receipt")
    except catalog.CatalogError:
        return None
    for _ in range(STALE_REREADS):
        try:
            metadata = catalog.receipt_metadata(root, [receipt_id])
            records = catalog.read_records(root, receipt=receipt_id, limit=100,
                                           expected_state=metadata["state"]["state_token"])
        except catalog.MissingRecord:
            return None
        except catalog.StaleState:
            continue
        except catalog.WriterBusy as error:
            raise Refusal("retryable", error.code) from error
        except catalog.CatalogError as error:
            raise CatalogUnavailable(str(error)) from error
        except (sqlite3.Error, OSError) as error:
            from .runner import classify  # runner imports this module through adapters
            if classify(error).category == "retryable":
                raise
            raise CatalogUnavailable(f"{type(error).__name__}: {error}") from error
        return metadata, records
    raise Refusal("retryable", catalog.StaleState.code)


def _registered(root, value, args):
    """The catalog's rendition child when it binds the exact rendition and original, else None."""
    source = value["source"]
    reads = _registration_reads(root, source["receipt_id"])
    if reads is None:
        return None
    metadata, records = reads
    original = metadata["registrations"][source["receipt_id"]]["artifact"]
    by_id = {record["id"]: record for record in records["records"]}
    child = by_id.get(f"A-{_kind(value)}-" + value["run_id"])
    relation = by_id.get(f"R-{_kind(value)}-" + value["run_id"], {}).get("data", {})
    data = original["data"]
    receipt = metadata["registrations"][source["receipt_id"]]["receipt"]["data"]
    current = {"receipt_id": source["receipt_id"], "artifact_id": original["id"],
               "source_ids": receipt["source_ids"],
               **{key: data.get(key) for key in ("path", "sha256", "bytes", "audience",
                                                 "handling_refs")}}
    if (child is None or source != current
            or child["data"].get("audience") != source["audience"]
            or child["data"].get("handling_refs") != source["handling_refs"] or child["data"].get("path") != args["rendition"]
            or child["data"].get("sha256") != args["rendition_sha256"]
            or child["data"].get("parent_id") != original["id"]
            or child["data"].get("role") != "generated-rendition"
            or child["data"].get("presence") != "present"
            or relation.get("from_id") != child["id"] or relation.get("to_id") != original["id"]
            or relation.get("relation") != "rendition-of"
            or data["sha256"] != source["sha256"] or data["path"] != source["path"]
            or data["presence"] != "present" or data["coverage"] == "excluded"):
        return None
    return child


def registered(root, rendition, rendition_sha256):
    """Whether the catalog binds this exact, current rendition to its unchanged original now."""
    root = Path(root)
    args = {"rendition": rendition, "rendition_sha256": rendition_sha256}
    try:
        _, value, failure = _rendition(root, args, index.rendition_api(root))
    except Refusal:
        return False
    return failure is None and _registered(root, value, args) is not None


def _read_findings(root, args, standing):
    """The decoded, valid findings, or the exception reason that stops evaluation."""
    try:
        raw = read_private(root, args["findings_path"], LIMIT)
    except FileNotFoundError as error:
        if refused_scope(root, standing, args["rendition"], args["rendition_sha256"]):
            # Out of scope, so no review was requested (``qualify`` skips the model call); the
            # outcome is the one the scope check gives once findings exist.
            return None, "prohibited-scope"
        # The authorized reviewer has not produced findings: the route is unavailable.
        raise Refusal("retryable", "review-route-unavailable") from error
    if len(raw) > standing["budget"]["max_findings_bytes"]:
        return None, "budget-exceeded"
    if hashlib.sha256(raw).hexdigest() != args["findings_sha256"]:
        raise Refusal("blocked", "stale-findings")
    try:
        findings = _decode(raw)
    except (UnicodeError, ValueError, IntakeError):
        return None, "invalid-findings"
    if not _findings_valid(findings):
        return None, "invalid-findings"
    return findings, None


def _findings_match(findings, standing, args):
    written = CRITERIA_FINDINGS[criteria(standing)]
    # Findings record which criteria they were reviewed under: v3 only under the v2 criteria, and
    # the v2 criteria only accept v3 (earlier findings never saw them).
    schema_fits = (findings["schema"] == written if written == FINDINGS_SCHEMA_V3
                   else findings["schema"] != FINDINGS_SCHEMA_V3)
    return (schema_fits
            and findings["policy"] == {"policy_id": standing["policy_id"], "version": standing["version"]}
            and findings["rendition"] == args["rendition"]
            and findings["rendition_sha256"] == args["rendition_sha256"]
            and findings["reviewer"] == standing["reviewer"])


def _expected_inspection(value, records):
    """What complete inspection of this rendition covers: (expected, pages, records, content)."""
    if _kind(value) == "docling":
        conversion = value["conversion"]
        document = conversion["document"]
        expected = {"pages": sorted(page["page_no"] for page in conversion["pages"]),
                    "texts": sorted(item["self_ref"] for item in document["texts"]),
                    "tables": sorted(item["self_ref"] for item in document["tables"]),
                    # Each warning instance is inspected individually, not per code.
                    "warnings": sorted(f"#/warnings/{i}" for i in range(len(conversion["warnings"])))}
        content = ["tables", "text"] if expected["tables"] else ["text"]
        return expected, len(conversion["pages"]), records, content
    payload = value["payload"]
    # Every payload record is inspected, including records retrieval omits
    # (transcript notes and footer), so review covers the whole rendition.
    records = payload[EXTRACT_FORMATS[payload["format"]]]
    expected = {"records": list(range(len(records))),
                "warnings": sorted(f"#/payload/warnings/{i}" for i in range(len(payload["warnings"])))}
    # PDF/image renditions count their pages against the policy's page budget.
    pages = len(payload["pages"]) if payload["format"] in renditions.PAGE_LINE_FORMATS else 0
    return expected, pages, records, ["text"]


def _snapshot(root, snapshot):
    if callable(snapshot):
        return snapshot()  # a shared lazy read (adapters.OneRead)
    if snapshot is not None:
        return snapshot
    try:
        return event_reader.read_snapshot(root)
    except events.contract.ReviewError as error:
        raise Refusal("blocked", "review-store-unavailable") from error


def _earlier_reviews(snapshot, same, standing, source, qualified, args):
    """This policy's own earlier-version reviews of the same source and scope, which this review
    replaces; ones it already superseded stay listed so a crash-after-commit retry rebuilds the
    identical event."""
    return sorted(e["event_id"] for e in same
                  if e["event_id"] != args["event_id"]
                  and (snapshot.superseded_by(e["event_id"]) or args["event_id"]) == args["event_id"]
                  and standing_events.earlier_standing_review(e, standing, source, qualified, args))


def _conflicting(snapshot, standing, qualified, result, args):
    """Whether another live review of this rendition stands in the way; records in ``qualified``
    the earlier reviews of this policy that this one supersedes."""
    # Only events on this rendition can matter (both checks require its locator), found by index.
    same = snapshot.at(args["rendition"])
    earlier = _earlier_reviews(snapshot, same, standing, qualified["source"], qualified, args)
    if earlier:
        qualified["supersedes"] = earlier
    candidate = {**result, **qualified}
    # Only this exact standing event (a crash-after-commit retry) is exempt.
    return any(snapshot.superseded_by(e["event_id"]) is None and e["event_id"] not in earlier
               and not standing_events.matches(e, candidate, args) for e in same)


def inspected_sets(inspected):
    """``inspected`` with its ``records`` expanded from ranges, every list sorted."""
    return {key: sorted(expand_records(items, MAX_REVIEW_RECORDS) if key == "records" else items)
            for key, items in inspected.items()}


def _parts_failure(findings, value, expected, budget):
    """The exception code when the parts of a part-reviewed rendition do not add up, else None.

    Parts must be contiguous and cover the rendition's records exactly once, in order; all warnings
    belong to the first part; each part is within the policy's per-review record and page budget; each
    part's inspected counts must equal what the findings list inside its range; and the document's
    decision must be the strictest part decision (``reject``, then ``defer``, then ``accept``).
    """
    parts, total = findings["parts"], len(expected["records"])
    if parts[0]["first"] != 0 or parts[-1]["last"] != total - 1 or any(
            b["first"] != a["last"] + 1 for a, b in zip(parts, parts[1:])):
        return "findings-mismatch"
    if parts[0]["warnings"] != len(expected["warnings"]) or any(p["warnings"] for p in parts[1:]):
        return "findings-mismatch"
    inspected = inspected_sets(findings["inspected"])
    records, listed = set(inspected["records"]), set(inspected["warnings"])
    lines = value["payload"][EXTRACT_FORMATS[value["payload"]["format"]]]
    paged = value["payload"]["format"] in renditions.PAGE_LINE_FORMATS
    for n, part in enumerate(parts):
        span = range(part["first"], part["last"] + 1)
        if len(span) > budget["max_records"]:
            return "budget-exceeded"
        if paged and len({lines[i]["page"] for i in span}) > budget["max_pages"]:
            return "budget-exceeded"
        if (part["inspected"]["records"] != sum(i in records for i in span)
                or part["inspected"]["warnings"] != (len(listed) if n == 0 else 0)):
            return "findings-mismatch"
    worst = max((p["decision"] for p in parts), key=DECISION_ORDER.index)
    return None if findings["decision"] == worst else "findings-mismatch"


def _decision(findings, expected, deferred):
    if findings["decision"] == "reject":
        return "rejected", ("reviewer-rejected",)
    if findings["decision"] == "defer" and deferred == "hold":
        return "deferred", ("reviewer-deferred",)
    inspected = {key: items for key, items in inspected_sets(findings["inspected"]).items() if key in expected}
    if inspected != expected:
        return "pending-review", ("insufficient-inspected-scope",)
    if findings["decision"] == "defer":
        return "qualified", ("complete-inspected-scope", "reviewer-deferred-low-confidence")
    return "qualified", ("complete-inspected-scope",)


def evaluate(root, args, snapshot=None):
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
    deferred = args.get("deferred", "hold")
    if deferred not in DEFERRED_MODES:
        raise Refusal("blocked", "standing-deferred-mode-invalid")
    findings, failure = _read_findings(root, args, standing)
    if failure:
        return outcome("exception", failure)
    if findings["decision"] == "defer" and deferred != "hold":
        # Set before the conflict check so a retry recognises its own low-confidence event.
        result["deferred"] = True
    result["explanation"] = findings["explanation"]
    result["exception_resolution"] = findings["exception_resolution"]
    result["unverifiable_references"] = len(findings.get("unverifiable_references", []))
    # Reported issues never block an accepted review; they travel to its event. Keys appear only
    # when there is something to attach, so a review without issues pins and records what it did.
    for key in ("contradictions", "missing_evidence"):
        if findings[key]:
            result[key] = list(findings[key])
    # Reviewer flags (v2 criteria) travel the same way and mark the published text low confidence.
    if findings.get("embedded_instructions"):
        result["embedded_instructions"] = list(findings["embedded_instructions"])
    if findings.get("sparse"):
        result["sparse"] = True
    if not _findings_match(findings, standing, args):
        return outcome("exception", "findings-mismatch")
    engine = index.rendition_api(root)
    rendition_raw, value, failure = _rendition(root, args, engine)
    if failure:
        return outcome("exception", failure)
    source = value["source"]
    if not permits(standing["scope"], source["receipt_id"], source["audience"]):
        return outcome("exception", "prohibited-scope")
    child = _registered(root, value, args)
    if child is None:
        return outcome("exception", "unregistered-rendition")
    try:
        records = list(renditions.records(rendition_raw, value, engine))
    except (index.IndexError_, TypeError, AttributeError, KeyError, ValueError):
        return outcome("exception", "rendition-fidelity")
    expected, pages, records, content = _expected_inspection(value, records)
    if set(findings["inspected"]) != set(expected):
        return outcome("exception", "findings-mismatch")
    result["inspected"] = {key: len(items) for key, items in expected.items()}
    if "parts" in findings:
        failure = "findings-mismatch" if "records" not in expected else _parts_failure(findings, value, expected, standing["budget"])
        if failure:
            return outcome("exception", failure)
        result["parts"] = [{k: p[k] for k in ("first", "last", "warnings", "decision")}
                           for p in findings["parts"]]
    elif pages > standing["budget"]["max_pages"] or len(records) > standing["budget"]["max_records"]:
        return outcome("exception", "budget-exceeded")
    if "reductions" in findings:
        result["reductions"] = {k: findings["reductions"][k] for k in ("count", "bytes")}
    if findings["correction_of"] is not None or findings["new_mapping"]:
        return outcome("pending-review", "independent-review-required")
    snapshot = _snapshot(root, snapshot)
    qualified = {"source": source, "content": content,
                 "format": value["payload"]["format"] if _kind(value) == "extract" else "docling",
                 "reviewer": standing["reviewer"], "context": standing["selection"]["context"]}
    if _conflicting(snapshot, standing, qualified, result, args):
        return outcome("pending-review", "conflicting-review")
    name, reasons = _decision(findings, expected, deferred)
    if name == "qualified":
        result.update(qualified, child=child)
    return outcome(name, *reasons)


def refusal(result):
    """Visible non-qualified outcomes block durably with their reason."""
    if result["outcome"] != "qualified":
        raise Refusal("blocked", "standing-" + result["outcome"] + "-" + result["reasons"][0])
