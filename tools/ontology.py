"""Declared relation vocabulary: the catalog's domain model is data, not code.

Algebraic properties declared here (`symmetric`, `transitive`) are **recorded,
not applied**. Nothing in this module or its callers computes an entailment.
They exist so that a future derived projection can compute labelled,
premise-carrying conclusions outside any authority store, under the conditions
in docs/decisions/0009-declared-ontology-and-entailment.md.
"""

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "config/ontology.json"
LOCAL = ROOT / "config/ontology.local.json"
RELATION_FIELDS = {
    "description", "from_kinds", "to_kinds", "irreflexive", "symmetric", "transitive",
}
TOP_FIELDS = {
    "schema_version", "vocabulary_version", "notice", "endpoint_kinds",
    "relations", "disjoint",
}
# Evidentiary grade is not configurable. A deployment may extend the relation
# vocabulary; it may never widen what an authority store will accept, because
# that is the invariant decision 0009 rests on. Entailed edges are computed in a
# derived projection and are refused here regardless of local configuration.
AUTHORITY_BASES = frozenset({"reported"})
RESERVED_BASES = frozenset({"entailed"})


class OntologyError(ValueError):
    """The declared vocabulary is unusable; no catalog operation may proceed."""


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise OntologyError("Duplicate vocabulary field")
        result[key] = value
    return result


def read(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise OntologyError("Vocabulary symlinks are not permitted")
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise OntologyError("Vocabulary exceeds 64 KiB")
    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except ValueError as error:
        raise OntologyError("Invalid vocabulary JSON") from error
    if not isinstance(value, dict):
        raise OntologyError("Vocabulary must be an object")
    return value


def names(value, label):
    if not isinstance(value, list) or not value:
        raise OntologyError(f"{label} must be a nonempty list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise OntologyError(f"{label} must contain nonempty strings")
    if len(set(value)) != len(value):
        raise OntologyError(f"{label} must not repeat a term")
    return value


def check_relation(name, body, endpoint_kinds):
    if not isinstance(name, str) or not name.strip():
        raise OntologyError("Relation term must be a nonempty string")
    if not isinstance(body, dict) or set(body) != RELATION_FIELDS:
        raise OntologyError(f"Relation {name} must declare exactly {sorted(RELATION_FIELDS)}")
    if not isinstance(body["description"], str) or not body["description"].strip():
        raise OntologyError(f"Relation {name} requires a description")
    for side in ("from_kinds", "to_kinds"):
        for kind in names(body[side], f"Relation {name} {side}"):
            if kind not in endpoint_kinds:
                raise OntologyError(f"Relation {name} {side} names an undeclared endpoint kind: {kind}")
    for flag in ("irreflexive", "symmetric", "transitive"):
        if type(body[flag]) is not bool:
            raise OntologyError(f"Relation {name} {flag} must be a boolean")


def load():
    value = read(BASE)
    if LOCAL.exists() or LOCAL.is_symlink():
        override = read(LOCAL)
        if set(override) - set(value):
            raise OntologyError("Unknown local vocabulary field")
        value.update(override)
    if set(value) != TOP_FIELDS:
        raise OntologyError("Unsupported vocabulary schema")
    for key in ("schema_version", "vocabulary_version"):
        if type(value[key]) is not int or value[key] < 1:
            raise OntologyError(f"Vocabulary {key} must be a positive integer")
    if value["schema_version"] != 1:
        raise OntologyError("Unsupported vocabulary schema version")
    if not isinstance(value["notice"], str) or not value["notice"].strip():
        raise OntologyError("Vocabulary requires a notice")
    endpoint_kinds = names(value["endpoint_kinds"], "Vocabulary endpoint_kinds")
    relations = value["relations"]
    if not isinstance(relations, dict) or not relations:
        raise OntologyError("Vocabulary must declare at least one relation")
    for name, body in relations.items():
        check_relation(name, body, endpoint_kinds)
    for name, body in relations.items():
        if not body["symmetric"]:
            continue
        for side, other in (("from_kinds", "to_kinds"), ("to_kinds", "from_kinds")):
            if set(body[side]) != set(body[other]):
                raise OntologyError(
                    f"Symmetric relation {name} must accept the same endpoint kinds on both sides")
    disjoint = value["disjoint"]
    if not isinstance(disjoint, list):
        raise OntologyError("Vocabulary disjoint must be a list")
    seen = set()
    for entry in disjoint:
        if not isinstance(entry, list) or len(entry) != 2:
            raise OntologyError("Each disjoint entry must be a pair of relation terms")
        first, second = entry
        for term in entry:
            if term not in relations:
                raise OntologyError(f"Disjoint entry names an undeclared relation: {term}")
        if first == second:
            raise OntologyError("A relation cannot be disjoint with itself")
        key = frozenset(entry)
        if key in seen:
            raise OntologyError("Duplicate disjoint entry")
        seen.add(key)
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return value, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


SETTINGS, HASH = load()
RELATIONS = SETTINGS["relations"]
BASES = AUTHORITY_BASES
DISJOINT = frozenset(frozenset(pair) for pair in SETTINGS["disjoint"])


def fingerprint():
    return hashlib.sha256(Path(__file__).read_bytes() + load()[1].encode()).hexdigest()


def declared(relation):
    body = RELATIONS.get(relation)
    if body is None:
        raise OntologyError(f"Undeclared relation: {relation}")
    return body


def accepts(relation, side, kind):
    return kind in declared(relation)[side]


def conflicts(relation, other):
    return frozenset((relation, other)) in DISJOINT
