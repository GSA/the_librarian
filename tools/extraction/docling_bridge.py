#!/usr/bin/env python3
"""Installation seam for Docling source policy and catalog registration."""

from copy import deepcopy
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "catalog"))
import catalog_store as catalog
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import policy


MAX_SOURCE = 64 * 1024 * 1024
LOADED_CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
POLICY_CODE_SHA256 = hashlib.sha256(Path(policy.__file__).read_bytes()).hexdigest()


class BridgeError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise BridgeError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _allowed(name, settings):
    path = PurePosixPath(name)
    if (path.is_absolute() or "\\" in name
            or any(part in ("", ".", "..") for part in name.split("/"))):
        return False
    if any(part in settings["excluded_directory_names"] for part in path.parts):
        return False
    if any(re.search(pattern, path.name) for pattern in settings["excluded_name_patterns"]):
        return False
    return any(name.startswith(prefix + "/") for prefix in settings["original_roots"])


def _policy_binding():
    require(digest(Path(policy.__file__).read_bytes()) == POLICY_CODE_SHA256,
            "Loaded policy code changed; start a fresh process")
    settings, policy_hash = policy.load()
    return settings, {
        "policy_sha256": policy_hash,
        "policy_code_sha256": POLICY_CODE_SHA256,
    }


def binding():
    """Fresh policy and installation-code binding; never use cached eligibility."""
    _, result = _policy_binding()
    here = Path(__file__)
    adapter = here.with_name("docling_adapter.py")
    coordinator = sys.modules.get("docling_adapter")
    require(coordinator is not None, "Adapter must be loaded before installation binding")
    require(digest(here.read_bytes()) == LOADED_CODE_SHA256,
            "Loaded bridge code changed; start a fresh process")
    require(digest(adapter.read_bytes()) == coordinator.LOADED_CODE_SHA256,
            "Loaded adapter code changed; start a fresh process")
    result.update({
        "bridge_sha256": LOADED_CODE_SHA256,
        "adapter_sha256": coordinator.LOADED_CODE_SHA256,
    })
    return result


def _read_source(path):
    path = catalog.safe_path(path)
    before = path.stat(follow_symlinks=False)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        require(stat.S_ISREG(opened.st_mode), "Source must be a regular file")
        require((before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino),
                "Source changed while being opened")
        raw = stream.read(MAX_SOURCE + 1)
        after = os.fstat(stream.fileno())
    require(len(raw) <= MAX_SOURCE, "Source exceeds 64 MiB limit")
    require((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
             opened.st_ctime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
             after.st_ctime_ns), "Source changed while being read")
    final = path.stat(follow_symlinks=False)
    require((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
             after.st_ctime_ns) ==
            (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns,
             final.st_ctime_ns), "Source path changed while being read")
    return raw


def source(root: Path, receipt: str, format_name: str):
    """Return catalog state, original record, source identity and one byte snapshot."""
    require(format_name in ("pdf", "image"), "Unsupported Docling format")
    settings, initial_policy = _policy_binding()
    snapshot = catalog.receipt_metadata(root, [receipt])
    registration = snapshot["registrations"][receipt]
    original = registration["artifact"]
    data = original["data"]
    require(data["role"] == "original" and data["presence"] == "present"
            and data["coverage"] != "excluded",
            "Receipt original is absent, excluded or not an original")
    relative = Path(data["path"])
    require(_allowed(relative.as_posix(), settings),
            "Original path is outside configured extraction roots or excluded by policy")
    suffixes = {"pdf": {".pdf"}, "image": {".png", ".jpg", ".jpeg"}}
    require(relative.suffix.lower() in suffixes[format_name],
            "Selected format disagrees with source extension")
    raw = _read_source(catalog.safe_path(root / relative))
    observed = digest(raw)
    require(data["sha256"] is None or data["sha256"] == observed,
            "Source differs from catalog hash; reconcile its identity/version")
    require(data["bytes"] is None or data["bytes"] == len(raw),
            "Source differs from catalog byte count; reconcile its identity/version")
    require(_policy_binding()[1] == initial_policy,
            "Policy changed during source validation; retry")
    identity = {
        "receipt_id": receipt, "artifact_id": original["id"],
        "source_ids": registration["receipt"]["data"]["source_ids"],
        "path": data["path"], "sha256": observed, "bytes": len(raw),
        "audience": data["audience"],
        "handling_refs": deepcopy(data["handling_refs"]),
    }
    return snapshot["state"], original, identity, raw


def _reference(path, sha):
    return {"path": path, "sha256": sha, "line_start": None, "line_end": None}


def registration_records(root: Path, original: dict, path: Path, value: dict):
    relative = path.relative_to(root).as_posix()
    rendition_sha = digest(value["canonical_bytes"])
    ref = _reference(relative, rendition_sha)
    parent = deepcopy(original)
    data = parent["data"]
    data.update(sha256=value["source"]["sha256"], hash_basis="observed",
                bytes=value["source"]["bytes"], extraction="existing-rendition")
    data["reason"] = ("A machine OCR/layout rendition exists; source coverage is unchanged. "
                      "Review, indexing and retrieval selection remain separate.")
    if ref not in data["provenance"]:
        data["provenance"].append(ref)
    identifier = "A-docling-" + value["run_id"]
    child = {"id": identifier, "kind": "artifact", "data": {
        "path": relative, "parent_id": original["id"],
        "receipt_id": value["source"]["receipt_id"],
        "role": "generated-rendition", "audience": data["audience"],
        "coverage": "deferred",
        "reason": "Machine OCR/layout rendition; not reviewed, indexed or independent evidence.",
        "presence": "present", "sha256": rendition_sha, "hash_basis": "observed",
        "bytes": len(value["canonical_bytes"]), "extraction": "not-required",
        "handling_refs": deepcopy(data["handling_refs"]),
        "provenance": [_reference(value["source"]["path"], value["source"]["sha256"]), ref],
    }}
    relation = {"id": "R-docling-" + value["run_id"], "kind": "relationship", "data": {
        "from_id": identifier, "to_id": original["id"], "relation": "rendition-of",
        "scope": ("Machine OCR/layout/text rendition; not visual equivalence, independent "
                  "evidence, review, indexing or acceptance."),
        "basis": "reported", "provenance": [ref],
    }}
    return [parent, child, relation]
