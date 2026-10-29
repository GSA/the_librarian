"""Closed, body-preserving intake request contracts."""
import hashlib
import json
import re


class IntakeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code="invalid_request"):
    if not condition:
        raise IntakeError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "duplicate_key")
        result[key] = value
    return result


def decode(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: require(False))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise IntakeError("invalid_json") from error


def fields(value, names):
    require(isinstance(value, dict) and set(value) == set(names))


def text(value, limit=200):
    require(isinstance(value, str) and 0 < len(value) <= limit
            and all(32 <= ord(c) < 127 for c in value))


def identifier(value):
    require(isinstance(value, str) and len(value) <= 200
            and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*", value) is not None)


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None)


def relative(value):
    text(value, 4096)
    require(not value.startswith("/") and "\\" not in value and ":" not in value
            and all(p not in {"", ".", ".."} for p in value.split("/")))


KINDS = ("receipt", "extraction", "review", "publication", "return")
OPERATIONS = {
    "catalog-submit": ("receipt", {"records", "actor", "request_id", "expected_state", "reason"}),
    "docling-prepare": ("extraction", {"receipt", "format_name", "artifacts_path", "python"}),
    "docling-verify": ("extraction", {"receipt", "format_name", "artifacts_path", "python"}),
    "docling-register": ("extraction", {"receipt", "format_name", "artifacts_path", "python", "actor", "expected_state"}),
    "review-append": ("review", {"value", "actor", "request_id", "expected_revision", "expected_state"}),
    "index-build": ("publication", {"manifest", "db"}),
    "await-ack": ("return", {"delivery_correlation", "disposition_path", "disposition_sha256"}),
}


def decode_request(raw):
    require(isinstance(raw, bytes) and 0 < len(raw) <= 1024 * 1024, "request_budget")
    value = decode(raw)
    fields(value, {"version", "correlation_id", "correction_of", "policy", "retry", "stages"})
    require(type(value["version"]) is int and value["version"] == 1)
    identifier(value["correlation_id"])
    if value["correction_of"] is not None:
        identifier(value["correction_of"])
        require(value["correction_of"] != value["correlation_id"])
    fields(value["policy"], {"version", "sha256"})
    require(type(value["policy"]["version"]) is int and value["policy"]["version"] > 0)
    sha(value["policy"]["sha256"])
    fields(value["retry"], {"max_attempts", "backoff_seconds"})
    for key, minimum, maximum in (("max_attempts", 1, 10), ("backoff_seconds", 0, 86400)):
        number = value["retry"][key]
        require(type(number) is int and minimum <= number <= maximum)
    stages = value["stages"]
    require(isinstance(stages, list) and 1 <= len(stages) <= 16)
    seen, previous = set(), -1
    for stage in stages:
        fields(stage, {"id", "kind", "operation", "arguments"})
        identifier(stage["id"])
        require(stage["id"] not in seen)
        seen.add(stage["id"])
        require(isinstance(stage["operation"], str) and stage["operation"] in OPERATIONS)
        kind, names = OPERATIONS[stage["operation"]]
        require(stage["kind"] == kind and KINDS.index(kind) >= previous)
        previous = KINDS.index(kind)
        fields(stage["arguments"], names)
        args = stage["arguments"]
        for key in names - {"records", "value", "expected_revision"}:
            text(args[key], 4096 if key in {"artifacts_path", "python", "manifest", "db", "disposition_path"} else 500)
        if "expected_revision" in args:
            require(type(args["expected_revision"]) is int and args["expected_revision"] >= 0)
        if "format_name" in args:
            require(args["format_name"] in {"pdf", "image"})
        for key in {"manifest", "db", "disposition_path"} & names:
            relative(args[key])
        if "disposition_sha256" in args:
            sha(args["disposition_sha256"])
    return value
