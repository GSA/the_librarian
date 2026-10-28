"""Catalog record vocabulary and exact validation contracts."""

from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata


class CatalogError(ValueError):
    """A catalog contract or integrity violation."""


KINDS = {"receipt", "source", "artifact", "policy", "relationship"}
ROLES = {
    "maintained-context", "historical-context", "instructions", "navigation",
    "original", "extracted-child", "sidecar", "generated-rendition", "tooling",
    "preserved-metadata", "other",
}
AUDIENCES = {"agent", "human", "source"}
COVERAGES = {"indexed", "pending-index", "pending-extraction", "deferred", "excluded"}
PRESENCES = {"present", "reported-deleted", "unresolved"}
EXTRACTIONS = {"unknown", "not-required", "existing-rendition", "pending"}
KEYS = {
    "receipt": {"source_ids", "artifact_id", "provenance"},
    "source": {"category", "provenance"},
    "artifact": {
        "path", "parent_id", "receipt_id", "role", "audience", "coverage", "reason",
        "presence", "sha256", "hash_basis", "bytes", "extraction", "handling_refs", "provenance",
    },
    "policy": {"default_audience", "human_documents", "exclusions", "provenance"},
    "relationship": {"from_id", "to_id", "relation", "scope", "basis", "provenance"},
}

def require(condition, message):
    if not condition:
        raise CatalogError(message)


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise CatalogError("Value is not canonical JSON") from error


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, name, maximum=500):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and not any(unicodedata.category(c) in {"Cc", "Cs"} for c in value),
            f"{name} must be nonempty short text without control characters")


def safe_relative(value):
    text(value, "Relative path", 4096)
    require(not value.startswith("/") and "\\" not in value and ":" not in value.split("/")[0],
            "Path must be root-relative, not absolute or a backslash path")
    parts = value.split("/")
    require(".." not in parts, "Path traversal is forbidden")
    result = "/".join(part for part in parts if part not in {"", "."})
    require(bool(result), "Path must name a root-relative entry")
    return result


def path_value(value):
    require(safe_relative(value) == value, "Stored paths must be canonical root-relative paths")


def exact(value, keys, name):
    require(isinstance(value, dict) and set(value) == set(keys), f"Invalid {name} fields")


def choice(value, choices, name):
    require(isinstance(value, str) and value in choices, f"Invalid {name}")


def identifier(value, kind):
    patterns = {"receipt": r"D[0-9]+", "source": r"[SIO][0-9]+",
                "artifact": r"A-[A-Za-z0-9][A-Za-z0-9-]*", "policy": r"corpus-policy",
                "relationship": r"R-[A-Za-z0-9][A-Za-z0-9-]*"}
    require(isinstance(value, str) and len(value) <= 200
            and re.fullmatch(patterns[kind], value) is not None, f"Invalid {kind} ID")


def hash_value(value):
    require(value is None or (isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)),
            "SHA-256 must be null or 64 lowercase hexadecimal characters")


def refs(value):
    require(isinstance(value, list), "References must be a list")
    for ref in value:
        exact(ref, {"path", "sha256", "line_start", "line_end"}, "reference")
        path_value(ref["path"])
        hash_value(ref["sha256"])
        start, end = ref["line_start"], ref["line_end"]
        require((start is None and end is None) or
                (type(start) is int and type(end) is int and 1 <= start <= end),
                "Reference line bounds must both be null or positive ordered integers")


def validate_record(record):
    exact(record, {"id", "kind", "data"}, "record")
    kind, data = record["kind"], record["data"]
    choice(kind, KINDS, "record kind")
    identifier(record["id"], kind)
    exact(data, KEYS[kind], kind)
    refs(data["provenance"])
    if kind == "receipt":
        require(isinstance(data["source_ids"], list), "source_ids must be a list")
        for source_id in data["source_ids"]:
            identifier(source_id, "source")
        require(len(set(data["source_ids"])) == len(data["source_ids"]), "Duplicate source IDs")
        identifier(data["artifact_id"], "artifact")
    elif kind == "source":
        category = {"S": "artifact", "I": "live-note", "O": "owner-direction"}[record["id"][0]]
        require(data["category"] == category, "Source category must agree with ID prefix")
    elif kind == "relationship":
        for endpoint in ("from_id", "to_id"):
            value = data[endpoint]
            require(isinstance(value, str), "Relationship endpoint must be an artifact or source ID")
            identifier(value, "artifact" if value.startswith("A-") else "source")
        require(data["from_id"] != data["to_id"], "Relationship cannot link an endpoint to itself")
        choice(data["relation"], {"version-of", "duplicate-of", "rendition-of", "same-event"},
               "relationship relation")
        text(data["scope"], "Relationship scope")
        require(data["basis"] == "reported", "Relationship basis must be reported")
    elif kind == "policy":
        require(data["default_audience"] == "agent", "Default audience must be agent")
        require(isinstance(data["human_documents"], list), "human_documents must be a list")
        for path in data["human_documents"]:
            path_value(path)
        require(len(set(data["human_documents"])) == len(data["human_documents"]),
                "Duplicate human document paths")
        require(isinstance(data["exclusions"], list), "exclusions must be a list")
        for exclusion in data["exclusions"]:
            text(exclusion, "Exclusion")
    else:
        choice(data["presence"], PRESENCES, "presence")
        if data["path"] is None:
            require(data["presence"] != "present", "Present artifacts require a path")
        else:
            path_value(data["path"])
        for key, target in (("parent_id", "artifact"), ("receipt_id", "receipt")):
            if data[key] is not None:
                identifier(data[key], target)
        choice(data["role"], ROLES, "artifact role")
        choice(data["audience"], AUDIENCES, "audience")
        choice(data["coverage"], COVERAGES, "coverage")
        text(data["reason"], "Artifact reason")
        hash_value(data["sha256"])
        choice(data["hash_basis"], {"reported", "observed", "unknown"}, "hash basis")
        require((data["sha256"] is None) == (data["hash_basis"] == "unknown"),
                "Unknown hash requires unknown basis; known hash requires reported or observed basis")
        require(data["bytes"] is None or (type(data["bytes"]) is int and data["bytes"] >= 0),
                "bytes must be null or a nonnegative integer")
        choice(data["extraction"], EXTRACTIONS, "extraction")
        refs(data["handling_refs"])


def validate_records(records):
    require(isinstance(records, list) and bool(records), "records must be a nonempty list")
    seen = set()
    for record in records:
        validate_record(record)
        require(record["id"] not in seen, "Duplicate IDs in submission")
        seen.add(record["id"])


def validate_graph(records):
    paths, parents = {}, {}

    def target(record_id, kind):
        require(record_id in records and records[record_id]["kind"] == kind,
                f"Missing {kind} reference: {record_id}")
        return records[record_id]["data"]

    for record_id, record in records.items():
        data = record["data"]
        if record["kind"] == "receipt":
            for source_id in data["source_ids"]:
                target(source_id, "source")
            artifact = target(data["artifact_id"], "artifact")
            require(artifact["receipt_id"] == record_id, "Receipt artifact must link back to receipt")
        elif record["kind"] == "relationship":
            for endpoint in ("from_id", "to_id"):
                endpoint_id = data[endpoint]
                require(endpoint_id in records and records[endpoint_id]["kind"] in {"artifact", "source"},
                        f"Missing artifact/source relationship endpoint: {endpoint_id}")
        elif record["kind"] == "artifact":
            path = data["path"]
            if path is not None:
                require(path not in paths, f"Distinct artifacts share path: {path}")
                paths[path] = record_id
            if data["receipt_id"] is not None:
                target(data["receipt_id"], "receipt")
            parent_id = data["parent_id"]
            if parent_id is not None:
                parent = target(parent_id, "artifact")
                require(parent_id != record_id, "Artifact cannot parent itself")
                if parent["receipt_id"] is not None:
                    require(data["receipt_id"] == parent["receipt_id"],
                            "Child receipt must agree with known parent receipt")
                parents[record_id] = parent_id
    visited = set()
    for record_id in parents:
        active = set()
        cursor = record_id
        while cursor in parents and cursor not in visited:
            require(cursor not in active, "Cyclic artifact parent relationship")
            active.add(cursor)
            cursor = parents[cursor]
        visited.update(active)
