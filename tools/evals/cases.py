"""The ``retrieval-eval-cases-v1`` file: strict validation, loading and private writes."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import uuid

from tools import durable
from tools.context_index.context_index import LOW_CONFIDENCE, query_terms

SCHEMA = "retrieval-eval-cases-v1"
DEFAULT_CASES = "config/evals.local.json"
ORIGINS = ("authored", "generated")
LEVELS = ("normal", "low")
ABSENCE = ("absent-corpus", "absent-partial", "present-uncombined", "low-confidence-excluded")
METHODS = ("known-item-v1", "absence-v1")
FIELDS = {"id", "origin", "query", "k"}
OPTIONAL = {"tags", "path", "low_confidence", "expect_paths", "expect_locators", "must_not_paths",
            "expect_confidence", "expect_absent", "expect_absence", "generator"}
CASE_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,99}")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/=-]{0,99}")
MAX_CASES = 10000
MAX_BYTES = 16 * 1024 * 1024
MAX_TARGETS = 50


class EvalError(Exception):
    """An explicit evaluation input, freshness or storage failure."""


def require(condition, message):
    if not condition:
        raise EvalError(message)


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def no_symlinks(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        require(not part.is_symlink(), f"Symlinks are not allowed: {part}")
    return path


def relative_path(value, label):
    require(isinstance(value, str) and value and "\\" not in value and "\0" not in value
            and not PurePosixPath(value).is_absolute()
            and not any(part in ("", ".", "..") for part in value.split("#", 1)[0].split("/")),
            f"{label} must be a root-relative path")
    return value


def path_list(case, key, label):
    values = case[key]
    require(isinstance(values, list) and 1 <= len(values) <= MAX_TARGETS,
            f"Case {case['id']}: {key} must list 1..{MAX_TARGETS} entries")
    for value in values:
        relative_path(value, f"Case {case['id']}: {label}")
        require(key == "expect_locators" or "#" not in value,
                f"Case {case['id']}: {key} holds document paths, not locators")
    require(len(set(values)) == len(values), f"Case {case['id']}: {key} has duplicates")


def validate_case(case):
    require(isinstance(case, dict), "Each case must be an object")
    require(isinstance(case.get("id"), str) and CASE_ID.fullmatch(case["id"]),
            "Each case needs an id of lowercase letters, digits and . _ : - (1..100)")
    name = case["id"]
    missing, unknown = FIELDS - case.keys(), case.keys() - FIELDS - OPTIONAL
    require(not missing, f"Case {name}: missing fields: {', '.join(sorted(missing))}")
    require(not unknown, f"Case {name}: unknown fields: {', '.join(sorted(unknown))}")
    require(case["origin"] in ORIGINS, f"Case {name}: origin must be one of {', '.join(ORIGINS)}")
    require(isinstance(case["query"], str) and case["query"].strip(), f"Case {name}: query must be text")
    try:
        query_terms(case["query"])
    except Exception as error:
        raise EvalError(f"Case {name}: {error}") from error
    require(type(case["k"]) is int and 1 <= case["k"] <= 50, f"Case {name}: k must be an integer 1..50")
    if "tags" in case:
        tags = case["tags"]
        require(isinstance(tags, list) and len(tags) <= 20 and len(set(map(str, tags))) == len(tags)
                and all(isinstance(t, str) and TAG.fullmatch(t) for t in tags),
                f"Case {name}: tags must be up to 20 unique short labels")
    if "path" in case:
        relative_path(case["path"], f"Case {name}: path")
        require("#" not in case["path"], f"Case {name}: path filter is a document path")
    if "low_confidence" in case:
        require(case["low_confidence"] in LOW_CONFIDENCE,
                f"Case {name}: low_confidence must be one of {', '.join(LOW_CONFIDENCE)}")
    for key, label in (("expect_paths", "expected path"), ("expect_locators", "expected locator"),
                       ("must_not_paths", "must-not path")):
        if key in case:
            path_list(case, key, label)
    targets = len(case.get("expect_paths", [])) + len(case.get("expect_locators", []))
    if "expect_absent" in case:
        require(case["expect_absent"] is True, f"Case {name}: expect_absent may only be true")
        require(not targets and "expect_confidence" not in case,
                f"Case {name}: an absence case cannot also expect hits or a confidence level")
    if "expect_absence" in case:
        require(case.get("expect_absent") is True, f"Case {name}: expect_absence requires expect_absent")
        require(case["expect_absence"] in ABSENCE,
                f"Case {name}: expect_absence must be one of {', '.join(ABSENCE)}")
    if "expect_confidence" in case:
        require(case["expect_confidence"] in LEVELS, f"Case {name}: expect_confidence must be normal or low")
        require(targets, f"Case {name}: expect_confidence needs expect_paths or expect_locators")
    require(targets or "must_not_paths" in case or "expect_absent" in case,
            f"Case {name}: expects nothing; add expect_paths, expect_locators, must_not_paths "
            "or expect_absent")
    if case["origin"] == "generated":
        generator = case.get("generator")
        require(isinstance(generator, dict) and set(generator) == {"method", "seed"}
                and generator["method"] in METHODS and type(generator["seed"]) is int,
                f"Case {name}: a generated case needs generator {{method, seed}}")
    else:
        require("generator" not in case, f"Case {name}: only a generated case carries a generator")
    return case


def validate(document):
    require(isinstance(document, dict) and set(document) == {"schema", "cases"},
            "Cases file must contain exactly: schema, cases")
    require(document["schema"] == SCHEMA, f"Unsupported cases schema; expected {SCHEMA}")
    cases = document["cases"]
    require(isinstance(cases, list) and len(cases) <= MAX_CASES, f"cases must be a list of at most {MAX_CASES}")
    seen = set()
    for case in cases:
        validate_case(case)
        require(case["id"] not in seen, f"Duplicate case id: {case['id']}")
        seen.add(case["id"])
    return document


def cases_path(root, value=None):
    path = Path(value) if value is not None else Path(DEFAULT_CASES)
    return no_symlinks(path if path.is_absolute() else Path(root) / path)


def read(path):
    """``(document, sha256)`` for a validated cases file."""
    path = no_symlinks(path)
    require(path.is_file(), f"No cases file at {path}; run generate or author one")
    raw = path.read_bytes()
    require(len(raw) <= MAX_BYTES, "Cases file exceeds 16 MiB")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=strict_object)
    except (UnicodeError, ValueError) as error:
        raise EvalError(f"Unreadable cases file: {error}") from error
    return validate(document), hashlib.sha256(raw).hexdigest()


def private_directory(path, stop):
    """Create ``path`` and its missing parents below ``stop`` as 0700 directories."""
    path, stop = no_symlinks(path), no_symlinks(stop)
    missing = []
    current = path
    while not current.exists() and current != stop:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        durable.fsync_directory(directory.parent)
    require(path.is_dir(), f"Not a directory: {path}")
    return path


def write_private(path, data, *, replace):
    """Write ``data`` to ``path`` as a fsynced 0600 file; ``replace`` allows an existing file."""
    path = no_symlinks(path)
    require(replace or not path.exists(), f"Refusing to overwrite {path}")
    require(not path.exists() or path.is_file(), f"Not a regular file: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        durable.write_new_private(temporary, data)
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        durable.fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def render(document):
    return (json.dumps(validate(document), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
