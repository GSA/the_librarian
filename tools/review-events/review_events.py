"""Native review-event contracts; data is supplied explicitly, never seeded."""
from datetime import date, datetime
import json
from pathlib import Path, PurePosixPath
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import policy
ROOT = Path(__file__).resolve().parents[2]
SCHEMA = Path(__file__).with_name("review-event-v1.schema.json")
LIMIT = 8 * 1024 * 1024
HANDLING = policy.SETTINGS["handling"]

class ReviewError(ValueError):
    """Explicit contract, integrity or unsupported-mapping failure."""


def require(condition, message):
    if not condition:
        raise ReviewError(message)


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def safe_path(path):
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), "Symlinks are not permitted")
    return path


def relative(value):
    require(isinstance(value, str) and value and "\\" not in value and "\0" not in value,
            "Expected a root-relative path")
    path = PurePosixPath(value)
    require(not path.is_absolute() and all(p not in ("", ".", "..") for p in value.split("/")),
            "Path traversal or noncanonical path")
    require(not any(ord(c) < 32 for c in value) and ":" not in value, "Invalid local path")
    return value


def read_json(path):
    with safe_path(path).open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    require(len(raw) <= LIMIT, "JSON input exceeds 8 MiB")
    def invalid_constant(_):
        raise ReviewError("Non-finite JSON number")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=invalid_constant), raw


def timestamp(value):
    require(isinstance(value, str) and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
        value) is not None, "recorded_at requires an RFC 3339 timestamp with timezone")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value)
    except ValueError as error:
        raise ReviewError("Invalid recorded_at calendar, clock or offset; leap seconds are unsupported") from error


def schema_validate(bundle, schema=None):
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry
        from referencing.exceptions import NoSuchResource
    except ImportError as error:
        raise ReviewError("Install tools/review-events/requirements.txt in a local environment") from error
    if schema is None:
        schema, _ = read_json(SCHEMA)
        schema["properties"]["audience"] = {"const": policy.SETTINGS["audience"]}
    def offline(uri):
        raise NoSuchResource(ref=uri)
    Draft202012Validator.check_schema(schema)
    checker = FormatChecker()
    @checker.checks("date-time", raises=(ValueError, ReviewError))
    def check_timestamp(value):
        return not isinstance(value, str) or bool(timestamp(value))
    validator = Draft202012Validator(schema, format_checker=checker,
                                    registry=Registry(retrieve=offline))
    error = next(validator.iter_errors(bundle), None)
    if error is not None:
        location = "/" + "/".join(str(p) for p in error.absolute_path)
        raise ReviewError(f"Schema violation at {location} ({error.validator}); input value omitted")
    require(type(bundle["schema_version"]) is int, "schema_version must be an integer")


def validate(bundle, root=ROOT, *, schema=None, stored=False):
    schema_validate(bundle, schema)
    root = safe_path(root)
    events = {}
    for event in bundle["events"]:
        event_id = event["event_id"]
        require(event_id not in events, "Duplicate event ID")
        events[event_id] = event
        source, scope, counts, provenance = (event[k] for k in ("source", "scope", "counts", "provenance"))
        relative(source["locator"])
        if source["parent_locator"] is not None:
            relative(source["parent_locator"])
        require((source["checksum"]["basis"] == "unknown") == (source["checksum"]["sha256"] is None),
                "Checksum basis/value mismatch")
        require("unspecified" not in scope["content"] or len(scope["content"]) == 1,
                "Unspecified content cannot assert additional dimensions")
        require("unspecified" not in scope["content"] or bool(event["uncertainties"]),
                "Unclassified content requires explicit uncertainty")
        require(bool(event["handling"]), "Explicit source handling is required")
        recorded = timestamp(event["recorded_at"])
        if event["reviewed_on"] is not None:
            require(date.fromisoformat(event["reviewed_on"]) <= recorded.date(), "Review date is after recording")
        require(event["outcome"] not in ("pending", "unreadable") or event["reviewed_on"] is None,
                "Pending/unreadable scope cannot assert a completed review date")
        require(event["outcome"] not in ("pending", "unreadable", "dispositioned") or event["reason"] is not None,
                "Pending, unreadable and dispositioned outcomes need an explicit reason")
        require(not any(x is None for x in (event["reviewer"], event["reviewed_on"], source["source_id"],
                                           source["checksum"]["sha256"], *counts.values()))
                or bool(event["uncertainties"]), "Unknown metadata/counts require explicit uncertainty")
        require(all(v is None or type(v) is int for v in counts.values()), "Counts require integer tokens")
        known = [v for k, v in counts.items() if k != "total" and v is not None]
        if counts["total"] is not None:
            require(sum(known) <= counts["total"], "Counts exceed declared total")
            if len(known) == 4:
                require(sum(known) == counts["total"], "Known counts do not partition the declared scope")
        outcome = event["outcome"]
        if outcome != "partial":
            require(all(v == 0 for k, v in counts.items() if k not in ("total", outcome)),
                    "Single-outcome scope has incompatible counts")
            require(counts[outcome] is None or counts[outcome] > 0, "Outcome cannot have zero matching units")
        else:
            require(counts["reviewed"] is None or counts["reviewed"] > 0, "Partial review needs reviewed scope")
            require(counts["pending"] is None or counts["unreadable"] is None
                    or counts["pending"] + counts["unreadable"] > 0, "Partial review needs remaining scope")
        if counts["total"] is not None:
            if outcome == "partial":
                minimum_unknown = int(counts["reviewed"] is None)
                minimum_unknown += int(sum(counts[k] or 0 for k in ("pending", "unreadable")) == 0)
            else:
                minimum_unknown = int(counts[outcome] is None)
            require(sum(known) + minimum_unknown <= counts["total"],
                    "Declared total leaves no capacity for the required outcome")
        ranged = scope["extent"] == "selected_ranges"
        require(ranged == bool(scope["ranges"]), "Range extent mismatch")
        if not ranged:
            require(scope["coordinates"] == "not_applicable", "Unexpected coordinate convention")
        else:
            require(scope["unit"] in ("line", "character", "page"), "Range unit is unsupported")
            require(scope["coordinates"] != "not_applicable", "Ranges need an explicit coordinate convention")
            previous, total = -1, 0
            inclusive = scope["coordinates"] == "one_based_inclusive"
            for span in scope["ranges"]:
                start, end = span["start"], span["end"]
                require(type(start) is int and type(end) is int, "Range coordinates require integer tokens")
                require(start >= int(inclusive) and end >= start + int(not inclusive), "Invalid range bounds")
                require(start > previous if inclusive else start >= previous, "Unsorted or overlapping ranges")
                total += end - start + int(inclusive)
                previous = end
            require(counts["total"] == total, "Range length does not match the scoped total")
        if "attachments" in scope["content"]:
            require(source["parent_locator"] is not None, "Attachment scope requires a parent locator")
        if provenance["origin"] == "agent_review" and stored:
            require(re.fullmatch(r"D0*[1-9][0-9]*", source["receipt_id"]) is not None,
                    "Agent review requires a real receipt identity")
            require(source["source_id"] is None or re.fullmatch(r"S[1-9][0-9]*", source["source_id"]) is not None,
                    "Agent review requires a real or explicitly unknown source identity")
            require(provenance["record"] is None and provenance["mapping_version"] is None
                    and not provenance["unmapped_fields"], "Native review cannot claim legacy import provenance")
            require(all(e["kind"] == "agent_statement" and e["pointer"] is None for e in event["evidence"]),
                    "Native evidence must be explicitly attributed agent statements")
            require(event["reviewer"] is not None, "Native review requires an attributed agent")
            if event["outcome"] in ("reviewed", "partial"):
                require(event["reviewed_on"] is not None, "Native completed review requires its review date")
        else:
            require(False, "Only attributed native review events are supported")
    successors = {}
    for event in events.values():
        for relation in event["relationships"]:
            target = relation["target_event_id"]
            require(target in events and target != event["event_id"], "Unresolved/self duplicate relationship")
            require(event["source"]["receipt_id"] != events[target]["source"]["receipt_id"],
                    "Duplicate receipts must retain distinct receipt identities")
        require(bool(event["supersedes"]) == (event["correction_reason"] is not None), "Correction reason mismatch")
        for target in event["supersedes"]:
            require(target in events and target != event["event_id"], "Unresolved/self correction")
            require(target not in successors, "Conflicting correction branches")
            successors[target] = event["event_id"]
            prior = events[target]
            require(all(event["source"][k] == prior["source"][k] for k in ("receipt_id", "source_id", "locator")),
                    "Correction crosses source identity")
            require(event["scope"] == prior["scope"], "Correction crosses declared scope")
            require(timestamp(event["recorded_at"]) > timestamp(prior["recorded_at"]),
                    "Correction must be recorded after its predecessor")
    return {"valid": True, "authority": bundle["authority"], "events": len(events),
            "active_events": len(events) - len(successors), "superseded_events": len(successors),
            "boundary": "Contract validity is not review verification, intake disposition or acceptance."}
