"""Lossless, opt-in presentation for complete live coverage/catalog responses.

The envelope preserves the original response under ``payload``. Only repeated
JSON strings, objects or arrays are pooled, without interpreting their meaning.
Pool entries are literal, never recursively encoded. Array order, object field
order and scalar types are retained. No storage, freshness or budget policy is
implemented here; callers must validate live reads first and budget the complete
serialized envelope afterwards.
"""

import copy
import json
import math


COVERAGE_FORMAT = "coverage-shared-values-v1"
CATALOG_FORMAT = "catalog-shared-values-v1"
NOTICE = (
    "Lossless presentation only; authority and caveats remain in payload. "
    "In payload, exact one-key shared_value_ref objects resolve through this "
    "packet's shared_values; exact one-key literal_value objects unwrap once. "
    "Pool entries and unwrapped values are literal JSON, not further references. "
    "References have no cross-packet meaning."
)
_RESERVED = ({"shared_value_ref"}, {"literal_value"})


class ProjectionError(ValueError):
    """The input is not a supported response or projection."""


def _require(condition, message):
    if not condition:
        raise ProjectionError(message)


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _validate_json(value, ancestors=None):
    """Reject coercions accepted by json.dumps, including non-string map keys."""
    if ancestors is None:
        ancestors = set()
    if type(value) in (str, int, bool, type(None)):
        return
    if type(value) is float:
        _require(math.isfinite(value), "Non-finite numbers are not JSON values")
        return
    _require(type(value) in (dict, list), "Expected plain JSON objects, arrays or scalars")
    identity = id(value)
    _require(identity not in ancestors, "Cyclic values are not JSON")
    ancestors.add(identity)
    if type(value) is dict:
        _require(all(type(key) is str for key in value), "JSON object keys must be strings")
        children = value.values()
    else:
        children = value
    for child in children:
        _validate_json(child, ancestors)
    ancestors.remove(identity)


def _validate_response(payload, domain):
    _validate_json(payload)
    _require(type(payload) is dict, "Response must be an object")
    _require(type(payload.get("records")) is list, "Response records must be an array")
    _require(all(type(record) is dict for record in payload["records"]),
             "Response records must be objects")
    _require(type(payload.get("count")) is int and payload["count"] == len(payload["records"]),
             "Response count must equal the number of records")
    if domain == "coverage":
        _require(payload.get("command") == "coverage"
                 and type(payload.get("contract_version")) is int
                 and payload["contract_version"] in (2, 3)
                 and payload.get("authority")
                 == "live-review-events-with-maintained-markdown-context",
                 "Expected live coverage contract v2 or v3")
        for key in ("notice", "caveat"):
            _require(type(payload.get(key)) is str, f"Coverage {key} must be a string")
        for key in ("batches", "relationships"):
            _require(type(payload.get(key)) is list
                     and all(type(item) is dict for item in payload[key]),
                     f"Coverage {key} must be an array of objects")
        _require(type(payload.get("event_store_state")) is dict,
                 "Coverage event_store_state must be an object")
        if payload["contract_version"] == 3:
            _require(type(payload.get("catalog_state")) is dict,
                     "Coverage v3 catalog_state must be an object")
        for key in ("evidence", "review_artifacts"):
            if key in payload:
                _require(type(payload[key]) is dict, f"Coverage {key} must be an object")
        _require(all(type(record.get("receipt_id")) is str for record in payload["records"]),
                 "Coverage records must have receipt_id strings")
    else:
        _require(payload.get("authority") == "catalog-metadata-only",
                 "Expected a catalog metadata query")
        _require(type(payload.get("state")) is dict, "Catalog state must be an object")
        _require(type(payload.get("total_matching")) is int
                 and payload["total_matching"] >= payload["count"],
                 "Catalog total_matching must be an integer at least count")
        _require("next_after" in payload
                 and (payload["next_after"] is None or type(payload["next_after"]) is str),
                 "Catalog next_after must be a string or null")
        for record in payload["records"]:
            _require(type(record.get("id")) is str
                     and record.get("kind") in ("receipt", "source", "artifact", "relationship", "policy")
                     and type(record.get("data")) is dict,
                     "Expected a catalog record with id, supported kind and data object")


def _reserved(value):
    return type(value) is dict and set(value) in _RESERVED


def _candidates(value, selected, counts, values):
    if type(value) not in (str, dict, list):
        return False
    text = _json_text(value)
    if text in selected:
        return True
    if _reserved(value):
        return False
    children = value.values() if type(value) is dict else value if type(value) is list else ()
    contains_selected = False
    for child in children:
        contains_selected = _candidates(child, selected, counts, values) or contains_selected
    # Short values cannot repay a readable reference and pool entry.
    # Do not replace ancestors of already pooled values and orphan their entries.
    if len(text) >= 64 and not contains_selected:
        counts[text] = counts.get(text, 0) + 1
        values.setdefault(text, value)
    return contains_selected


def _encode(value, selected):
    if type(value) in (str, dict, list):
        reference = selected.get(_json_text(value))
        if reference is not None:
            return {"shared_value_ref": reference}
    if _reserved(value):
        return {"literal_value": copy.deepcopy(value)}
    if type(value) is dict:
        return {key: _encode(child, selected) for key, child in value.items()}
    if type(value) is list:
        return [_encode(child, selected) for child in value]
    return value


def _compact(payload, domain, format_name):
    _validate_response(payload, domain)
    selected, pool = {}, {}
    while True:
        counts, values = {}, {}
        _candidates(payload, selected, counts, values)
        reference = f"value-{len(pool) + 1}"
        reference_size = len(_json_text({"shared_value_ref": reference}))
        best, best_saving = None, 0
        for text, count in counts.items():
            if count < 2:
                continue
            saving = ((len(text) - reference_size) * count
                      - len(_json_text(reference)) - 1 - len(text) - 1)
            if saving > best_saving:
                best, best_saving = text, saving
        if best is None:
            break
        selected[best] = reference
        pool[reference] = copy.deepcopy(values[best])
    return {
        "format": format_name,
        "notice": NOTICE,
        "payload": _encode(payload, selected),
        "shared_values": pool,
    }


def _expand(packet, domain, format_name):
    _validate_json(packet)
    _require(type(packet) is dict
             and set(packet) == {"format", "notice", "payload", "shared_values"},
             "Expected a complete shared-values envelope")
    _require(packet["format"] == format_name, f"Expected format {format_name}")
    _require(packet["notice"] == NOTICE, "Unsupported shared-values instructions")
    pool = packet["shared_values"]
    _require(type(pool) is dict, "shared_values must be an object")
    _require(all(type(value) in (str, dict, list) for value in pool.values()),
             "shared_values entries must be literal strings, objects or arrays")
    used = set()

    def expand(value):
        if type(value) is dict:
            if set(value) == {"shared_value_ref"}:
                reference = value["shared_value_ref"]
                _require(type(reference) is str and reference in pool,
                         "Invalid or dangling shared_value_ref")
                used.add(reference)
                return copy.deepcopy(pool[reference])
            if set(value) == {"literal_value"}:
                _require(_reserved(value["literal_value"]), "Invalid literal_value escape")
                return copy.deepcopy(value["literal_value"])
            return {key: expand(child) for key, child in value.items()}
        if type(value) is list:
            return [expand(child) for child in value]
        return value

    payload = expand(packet["payload"])
    _require(used == set(pool), "Unreferenced shared_values entries")
    _validate_response(payload, domain)
    return payload


def compact_coverage(payload):
    """Project a complete live coverage v2/v3 response; never truncate it."""
    return _compact(payload, "coverage", COVERAGE_FORMAT)


def expand_coverage(packet):
    """Reconstruct the full coverage response, rejecting malformed projections."""
    return _expand(packet, "coverage", COVERAGE_FORMAT)


def compact_catalog(payload):
    """Project a complete catalog query, including any history and pagination."""
    return _compact(payload, "catalog", CATALOG_FORMAT)


def expand_catalog(packet):
    """Reconstruct the full catalog response, rejecting malformed projections."""
    return _expand(packet, "catalog", CATALOG_FORMAT)
