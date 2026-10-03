"""Deployment policy is data, not organization-specific code."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "config/policy.json"
LOCAL = ROOT / "config/policy.local.json"


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate policy field")
        result[key] = value
    return result


def read(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Policy symlinks are not permitted")
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Policy exceeds 64 KiB")
    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("Policy must be an object")
    return value, raw


def load():
    value, raw = read(BASE)
    binding = [raw]
    if LOCAL.exists() or LOCAL.is_symlink():
        override, local_raw = read(LOCAL)
        if set(override) - set(value):
            raise ValueError("Unknown local policy field")
        value.update(override)
        binding.append(local_raw)
    expected = {
        "schema_version", "audience", "handling", "markdown_roots", "original_roots",
        "excluded_directory_names", "excluded_name_patterns", "transcript",
    }
    if set(value) != expected or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Unsupported policy schema")
    for key in ("audience", "handling"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError("Policy audience and handling must be nonempty strings")
    for key in ("markdown_roots", "original_roots", "excluded_directory_names", "excluded_name_patterns"):
        if not isinstance(value[key], list) or not all(isinstance(v, str) and v for v in value[key]):
            raise ValueError("Policy collections must contain nonempty strings")
    for name in value["markdown_roots"] + value["original_roots"]:
        path = PurePosixPath(name)
        if path.is_absolute() or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")):
            raise ValueError("Policy roots must be canonical relative directories")
    for pattern in value["excluded_name_patterns"]:
        re.compile(pattern)
    transcript = value["transcript"]
    if not isinstance(transcript, dict) or set(transcript) != {
        "notes_headings", "transcript_heading", "end_pattern", "precedence",
    }:
        raise ValueError("Invalid transcript format policy")
    if not isinstance(transcript["notes_headings"], list) or not all(
        isinstance(v, str) and v for v in transcript["notes_headings"]
    ):
        raise ValueError("Transcript notes headings must be a list of strings")
    for key in ("transcript_heading", "end_pattern", "precedence"):
        if not isinstance(transcript[key], str) or not transcript[key]:
            raise ValueError("Transcript rules must be nonempty strings")
    re.compile(transcript["end_pattern"])
    return value, hashlib.sha256(b"\0".join(binding)).hexdigest()


SETTINGS, HASH = load()


def fingerprint():
    return hashlib.sha256(Path(__file__).read_bytes() + load()[1].encode()).hexdigest()


def allowed(name, category):
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")):
        return False
    if any(part in SETTINGS["excluded_directory_names"] for part in path.parts):
        return False
    if any(re.search(pattern, path.name) for pattern in SETTINGS["excluded_name_patterns"]):
        return False
    return any(name.startswith(prefix + "/") for prefix in SETTINGS[category])
