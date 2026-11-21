"""Native review-event contracts; data is supplied explicitly, never seeded."""
from datetime import date, datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from tools import policy
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


# Events already proven schema-valid, per item schema. Re-reading a store
# revalidates the same immutable history on every read; only unseen events
# need the (expensive) schema walk. Keys are SHA-256 digests, not event structure: 32 bytes per
# event instead of a frozen copy of it (about 300 MB at 40k events).
_SCHEMA_VALID = {}
_SCHEMA_VALID_LIMIT = 200_000
_BUNDLE_KEYWORDS = {"$schema", "$id", "title", "description", "type", "additionalProperties",
                    "required", "properties", "$defs"}
_EVENTS_KEYWORDS = {"type", "minItems", "maxItems", "items"}
# The fast path compiles the item schema to one Python predicate. It supports only the keywords
# below; any other keyword, an unresolvable, cyclic or external reference, or an unusual value
# (non-string unique items, non-scalar const/enum) disables it, never the validation.
_COMPILED = {}
_TYPES = {"string": lambda v: type(v) is str, "integer": lambda v: type(v) is int,
          "null": lambda v: v is None, "array": lambda v: type(v) is list,
          "object": lambda v: type(v) is dict, "boolean": lambda v: type(v) is bool}
_IGNORED = {"title", "description"}


class _NotCompilable(Exception):
    pass


def _exact(value):
    """A scalar compared by Python type and value (``False`` is not ``0``, ``1.0`` is not ``1``)."""
    if type(value) not in (str, int, bool, type(None)):
        raise _NotCompilable
    return type(value), value


def _compile(node, defs, trail, format_checker):
    """A predicate that is True only for instances the schema ``node`` accepts.

    A False answer means "not proven valid", never "invalid": the caller then runs the full walk.
    """
    if node is True:
        return lambda value: True
    if not isinstance(node, dict):
        raise _NotCompilable
    tests = []
    props = {}
    for keyword, rule in node.items():
        if keyword in _IGNORED:
            continue
        if keyword == "$ref":
            name = rule[len("#/$defs/"):] if isinstance(rule, str) and rule.startswith("#/$defs/") else None
            if name is None or name in trail or not isinstance(defs.get(name), dict):
                raise _NotCompilable
            tests.append(_compile(defs[name], defs, trail + (name,), format_checker))
        elif keyword == "type":
            kinds = rule if isinstance(rule, list) else [rule]
            checks = [_TYPES.get(kind) for kind in kinds]
            if not kinds or None in checks:
                raise _NotCompilable
            tests.append((lambda c: lambda v: c(v))(checks[0]) if len(checks) == 1
                         else (lambda cs: lambda v: any(c(v) for c in cs))(checks))
        elif keyword == "enum":
            if not isinstance(rule, list):
                raise _NotCompilable
            allowed = {_exact(item) for item in rule}
            tests.append(lambda v, allowed=allowed: type(v) in (str, int, bool, type(None)) and (type(v), v) in allowed)
        elif keyword == "const":
            wanted = _exact(rule)
            tests.append(lambda v, wanted=wanted: type(v) in (str, int, bool, type(None)) and (type(v), v) == wanted)
        elif keyword == "required":
            names = tuple(rule)
            tests.append(lambda v, names=names: type(v) is not dict or all(n in v for n in names))
        elif keyword == "properties":
            props = {name: _compile(sub, defs, trail, format_checker) for name, sub in rule.items()}
        elif keyword == "additionalProperties":
            if rule is not False and not isinstance(rule, dict):
                raise _NotCompilable
        elif keyword == "items":
            item = _compile(rule, defs, trail, format_checker)
            tests.append(lambda v, item=item: type(v) is not list or all(item(x) for x in v))
        elif keyword == "oneOf":
            branches = [_compile(sub, defs, trail, format_checker) for sub in rule]
            tests.append(lambda v, branches=branches: sum(1 for b in branches if b(v)) == 1)
        elif keyword == "minItems":
            tests.append(lambda v, n=rule: type(v) is not list or len(v) >= n)
        elif keyword == "maxItems":
            tests.append(lambda v, n=rule: type(v) is not list or len(v) <= n)
        elif keyword == "uniqueItems":
            if rule is not True:
                raise _NotCompilable
            # Only strings are proven unique here; any other item makes the full walk decide.
            tests.append(lambda v: type(v) is not list or (all(type(x) is str for x in v) and len(set(v)) == len(v)))
        elif keyword == "minLength":
            tests.append(lambda v, n=rule: type(v) is not str or len(v) >= n)
        elif keyword == "maxLength":
            tests.append(lambda v, n=rule: type(v) is not str or len(v) <= n)
        elif keyword == "minimum":
            tests.append(lambda v, n=rule: type(v) is not int or v >= n)
        elif keyword == "pattern":
            matcher = re.compile(rule).search
            tests.append(lambda v, matcher=matcher: type(v) is not str or matcher(v) is not None)
        elif keyword == "format":
            if format_checker is None:
                raise _NotCompilable
            tests.append(lambda v, name=rule: format_checker.conforms(v, name))
        else:
            raise _NotCompilable
    if "properties" in node or node.get("additionalProperties") is False:
        closed = node.get("additionalProperties") is False
        extra = node.get("additionalProperties")
        extra = _compile(extra, defs, trail, format_checker) if isinstance(extra, dict) else None

        def members(value):
            if type(value) is not dict:
                return True
            for key, member in value.items():
                test = props.get(key)
                if test is None:
                    if closed or (extra is not None and not extra(member)):
                        return False
                elif not test(member):
                    return False
            return True
        tests.append(members)
    if len(tests) == 1:
        return tests[0]
    return lambda value: all(test(value) for test in tests)


def _fast_item_validator(schema, format_checker=None):
    """A predicate for one event with the item schema compiled, or ``None`` when it cannot be.

    jsonschema walks the reference-heavy item schema at about 1 ms per event; the compiled predicate
    does the same checks in about 1/20 of that. Callers treat only True as an answer; every other
    case is decided by the full validation, which also reports the exact error.
    """
    try:
        defs = schema.get("$defs", {})
        item = schema["properties"]["events"]["items"]
        key = (hashlib.sha256(json.dumps([item, defs], sort_keys=True, allow_nan=False).encode()).digest(),
               id(format_checker))
        compiled = _COMPILED.get(key)
        if compiled is None:
            compiled = _compile(item, defs, (), format_checker)
            if len(_COMPILED) >= 64:
                _COMPILED.clear()
            _COMPILED[key] = (compiled, format_checker)
        else:
            compiled = compiled[0]
    except (_NotCompilable, KeyError, TypeError, ValueError, AttributeError, re.error):
        return None
    return compiled


def _frozen(value):
    """Exact hashable identity of a decoded JSON value, including Python types."""
    if isinstance(value, dict):
        return dict, tuple(sorted((k, _frozen(v)) for k, v in value.items()))
    if isinstance(value, list):
        return list, tuple(_frozen(v) for v in value)
    return type(value), value


def _digest(value):
    """Exact identity of a decoded JSON value: Python's repr separates ``1``, ``1.0``, ``True`` and ``"1"``."""
    return hashlib.sha256(repr(value).encode("utf-8")).digest()


def _valid_by_event(validator, schema, bundle, format_checker=None):
    """True only when every event and the envelope are known valid; False means "do the full walk".

    Applies only when the schema validates ``events`` purely per item (type, item
    count and ``items``), so bundle validity equals envelope validity plus count
    bounds plus validity of each event alone. Any failure falls back to the full
    validation, which reports the exact error.
    """
    events = bundle.get("events") if isinstance(bundle, dict) else None
    properties = schema.get("properties")
    rule = properties.get("events") if isinstance(properties, dict) else None
    if (not isinstance(events, list) or not events or set(schema) - _BUNDLE_KEYWORDS
            or schema.get("type") != "object" or not isinstance(rule, dict)
            or set(rule) - _EVENTS_KEYWORDS or rule.get("type") != "array" or "items" not in rule
            or not (rule.get("minItems", 0) <= len(events) <= rule.get("maxItems", len(events)))):
        return False
    try:
        item_schema = hashlib.sha256(json.dumps({**schema, "properties": {
            **properties, "events": {k: v for k, v in rule.items() if k not in {"minItems", "maxItems"}}}},
            sort_keys=True, allow_nan=False).encode()).digest()
        keys = [_digest(event) for event in events]
        known = _SCHEMA_VALID.get(item_schema, set())
        unseen = {key: event for key, event in zip(keys, events) if key not in known}
    except (TypeError, ValueError, UnicodeError):
        return False
    # The envelope is always checked, with the first event, by the full walk.
    if next(validator.iter_errors({**bundle, "events": [events[0]]}), None) is not None:
        return False
    unseen.pop(keys[0], None)
    if unseen:
        fast = _fast_item_validator(schema, format_checker)
        for event in unseen.values():
            # Without the inlined validator, or when it does not accept the event, the full walk decides.
            if fast is None or not fast(event):
                if next(validator.iter_errors({**bundle, "events": [event]}), None) is not None:
                    return False
    proven = set(unseen) | {keys[0]}
    if sum(len(v) for v in _SCHEMA_VALID.values()) + len(proven) > _SCHEMA_VALID_LIMIT:
        _SCHEMA_VALID.clear()
    _SCHEMA_VALID.setdefault(item_schema, set()).update(proven)
    return True


_SCHEMA_CHECKED = set()


def _check_schema(validator_class, schema):
    """Metaschema check, done once per exact schema; it is a pure function of the schema."""
    try:
        key = _frozen(schema)
    except TypeError:
        key = None
    if key is None or key not in _SCHEMA_CHECKED:
        validator_class.check_schema(schema)
        if key is not None:
            if len(_SCHEMA_CHECKED) >= 1024:
                _SCHEMA_CHECKED.clear()
            _SCHEMA_CHECKED.add(key)


def schema_validate(bundle, schema=None, *, root=None):
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry
        from referencing.exceptions import NoSuchResource
    except ImportError as error:
        raise ReviewError("Install ./tools/context-mcp in a local environment") from error
    if schema is None:
        schema, _ = read_json(SCHEMA)
        audience = policy.for_root(root)[0]["audience"]
        schema["properties"]["audience"] = {"const": audience}
    def offline(uri):
        raise NoSuchResource(ref=uri)
    _check_schema(Draft202012Validator, schema)
    checker = FormatChecker()
    @checker.checks("date-time", raises=(ValueError, ReviewError))
    def check_timestamp(value):
        return not isinstance(value, str) or bool(timestamp(value))
    validator = Draft202012Validator(schema, format_checker=checker,
                                    registry=Registry(retrieve=offline))
    error = None if _valid_by_event(validator, schema, bundle, checker) else next(validator.iter_errors(bundle), None)
    if error is not None:
        location = "/" + "/".join(str(p) for p in error.absolute_path)
        raise ReviewError(f"Schema violation at {location} ({error.validator}); input value omitted")
    require(type(bundle["schema_version"]) is int, "schema_version must be an integer")


def validate(bundle, root=ROOT, *, schema=None, stored=False, known=None, taken=None):
    """Validate ``bundle``. ``known(event_id)`` returns an already validated event, and
    ``taken(event_id)`` the event that corrects it, so a delta of new events is checked against a
    validated history without handing that history in again (Kata m0tg); without them the bundle
    must stand alone."""
    root = safe_path(root)
    schema_validate(bundle, schema, root=root)
    events = {}
    lookup = known or (lambda event_id: None)
    for event in bundle["events"]:
        event_id = event["event_id"]
        require(event_id not in events and lookup(event_id) is None, "Duplicate event ID")
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
            related = events.get(target) or lookup(target)
            require(related is not None and target != event["event_id"],
                    "Unresolved/self duplicate relationship")
            require(event["source"]["receipt_id"] != related["source"]["receipt_id"],
                    "Duplicate receipts must retain distinct receipt identities")
        require(bool(event["supersedes"]) == (event["correction_reason"] is not None), "Correction reason mismatch")
        for target in event["supersedes"]:
            prior = events.get(target) or lookup(target)
            require(prior is not None and target != event["event_id"], "Unresolved/self correction")
            require(target not in successors and not (taken and taken(target) is not None),
                    "Conflicting correction branches")
            successors[target] = event["event_id"]
            require(all(event["source"][k] == prior["source"][k] for k in ("receipt_id", "source_id", "locator")),
                    "Correction crosses source identity")
            require(event["scope"] == prior["scope"], "Correction crosses declared scope")
            require(timestamp(event["recorded_at"]) > timestamp(prior["recorded_at"]),
                    "Correction must be recorded after its predecessor")
    return {"valid": True, "authority": bundle["authority"], "events": len(events),
            "active_events": len(events) - len(successors), "superseded_events": len(successors),
            "boundary": "Contract validity is not review verification, intake disposition or acceptance."}
