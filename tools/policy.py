"""Deployment policy is data, not organization-specific code."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import uuid


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "config/policy.json"
LOCAL = ROOT / "config/policy.local.json"
INSTANCE_FILE = ".local/instance.json"
MIGRATION_FILE = ".local/migration.json"
DEFAULT_STORAGE_ROOTS = {
    "catalog_home": ".local/catalog",
    "review_events_home": ".local/review-events",
    "context_index_db": "tools/context_index/.local/index.sqlite3",
    "intake_home": ".local/intake",
    "extractions_home": "data/extractions/.local",
    "diagnostics_log": ".local/diagnostics/retrieval.jsonl",
    "selection_manifest": "config/selection.json",
}


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate policy field")
        result[key] = value
    return result


def _no_symlinks(path):
    path = Path(path)
    if ".." in path.parts:
        raise ValueError("Policy path traversal is not permitted")
    path = path.absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Policy symlinks are not permitted")
    return path


def _validate_relative_path(name, label="Policy path"):
    if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
        raise ValueError(f"{label} must be a canonical relative path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in ("", ".", "..") for p in name.split("/")):
        raise ValueError(f"{label} must be a canonical relative path")
    return name


def _validate_storage_roots(raw_roots, markdown_roots, original_roots):
    if not isinstance(raw_roots, dict):
        raise ValueError("Policy storage_roots must be an object")
    if set(raw_roots) - set(DEFAULT_STORAGE_ROOTS):
        raise ValueError("Unknown storage_roots field")
    merged = dict(DEFAULT_STORAGE_ROOTS)
    for key, value in raw_roots.items():
        merged[key] = _validate_relative_path(value, f"storage_roots.{key}")
    if not merged["context_index_db"].endswith(".sqlite3"):
        raise ValueError("storage_roots.context_index_db must end with .sqlite3")
    if not merged["diagnostics_log"].endswith(".jsonl"):
        raise ValueError("storage_roots.diagnostics_log must end with .jsonl")
    if not merged["selection_manifest"].endswith(".json"):
        raise ValueError("storage_roots.selection_manifest must end with .json")
    if ".local" not in PurePosixPath(merged["extractions_home"]).parts:
        raise ValueError("storage_roots.extractions_home must stay within a .local directory")
    store_keys = (
        "catalog_home", "review_events_home", "intake_home", "extractions_home",
        "context_index_db", "diagnostics_log", "selection_manifest",
    )
    paths = [PurePosixPath(merged[key]) for key in store_keys]
    if len(set(paths)) != len(paths):
        raise ValueError("storage_roots paths must be distinct")
    dir_keys = ("catalog_home", "review_events_home", "intake_home", "extractions_home")
    for key1 in dir_keys:
        path1 = PurePosixPath(merged[key1])
        for key2 in store_keys:
            if key1 == key2:
                continue
            path2 = PurePosixPath(merged[key2])
            if path2 == path1 or path1 in path2.parents or path2 in path1.parents:
                raise ValueError(f"Overlapping storage_roots are forbidden: {key1} and {key2}")
    content_roots = [PurePosixPath(root_name) for root_name in markdown_roots + original_roots]
    for key in ("catalog_home", "review_events_home", "intake_home", "context_index_db", "diagnostics_log"):
        path = PurePosixPath(merged[key])
        if any(path == c or c in path.parents or path in c.parents for c in content_roots):
            raise ValueError(f"Operational storage root {key} cannot overlap content roots")
    return merged


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


def load(root=None):
    if root is None:
        base_path, local_path = BASE, LOCAL
    else:
        instance_root = _no_symlinks(root)
        candidate_base = instance_root / "config/policy.json"
        base_path = candidate_base if (candidate_base.exists() or candidate_base.is_symlink()) else BASE
        local_path = instance_root / "config/policy.local.json"
    value, raw = read(base_path)
    binding = [raw]
    if local_path.exists() or local_path.is_symlink():
        override, local_raw = read(local_path)
        allowed_keys = set(value) | {"storage_roots"}
        if set(override) - allowed_keys:
            raise ValueError("Unknown local policy field")
        if ("storage_roots" in override
                and isinstance(value.get("storage_roots"), dict)
                and isinstance(override["storage_roots"], dict)):
            merged_roots = dict(value["storage_roots"])
            merged_roots.update(override["storage_roots"])
            value = dict(value)
            value.update(override)
            value["storage_roots"] = merged_roots
        else:
            value.update(override)
        binding.append(local_raw)
    expected = {
        "schema_version", "audience", "handling", "markdown_roots", "original_roots",
        "excluded_directory_names", "excluded_name_patterns", "transcript",
    }
    optional = {"storage_roots"}
    if (not expected <= set(value) or set(value) - expected - optional
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
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
    if "storage_roots" in value:
        _validate_storage_roots(value["storage_roots"], value["markdown_roots"], value["original_roots"])
    return value, hashlib.sha256(b"\0".join(binding)).hexdigest()


_IMPORT_SETTINGS, _IMPORT_HASH = load()
SETTINGS, HASH = deepcopy(_IMPORT_SETTINGS), _IMPORT_HASH


def _has_instance_policy(root):
    if root is None:
        return False
    instance_root = _no_symlinks(root)
    return any(
        (instance_root / rel).exists() or (instance_root / rel).is_symlink()
        for rel in ("config/policy.json", "config/policy.local.json")
    )


def for_root(root=None):
    if _has_instance_policy(root):
        return load(root)
    disk_settings, disk_hash = load()
    if disk_hash == _IMPORT_HASH and SETTINGS != _IMPORT_SETTINGS:
        return (
            SETTINGS,
            hashlib.sha256(
                json.dumps(SETTINGS, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )
    return disk_settings, disk_hash


def fingerprint(root=None):
    policy_hash = for_root(root)[1]
    return hashlib.sha256(Path(__file__).read_bytes() + policy_hash.encode()).hexdigest()


def allowed(name, category, root=None):
    settings = for_root(root)[0]
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")):
        return False
    if any(part in settings["excluded_directory_names"] for part in path.parts):
        return False
    if any(re.search(pattern, path.name) for pattern in settings["excluded_name_patterns"]):
        return False
    return any(name.startswith(prefix + "/") for prefix in settings[category])


def storage_roots(root=None):
    settings = for_root(root)[0]
    return _validate_storage_roots(
        settings.get("storage_roots", {}),
        settings["markdown_roots"],
        settings["original_roots"],
    )


def selection_path(root, explicit=None):
    instance_root = _no_symlinks(root)
    if explicit is not None:
        candidate = Path(explicit)
        return candidate if candidate.is_absolute() else instance_root / candidate
    local_selection = instance_root / "config/selection.local.json"
    if local_selection.exists() or local_selection.is_symlink():
        return _no_symlinks(local_selection)
    return _no_symlinks(instance_root / storage_roots(instance_root)["selection_manifest"])


def _read_private_json(path, label):
    path = _no_symlinks(path)
    if not path.is_file() or (path.stat().st_mode & 0o077):
        raise ValueError(f"{label} must be a private regular file (0600)")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private_json(path, payload):
    path = _no_symlinks(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _probe_store_id(db_path, expected_app_id):
    import sqlite3
    db_path = _no_symlinks(db_path)
    if not db_path.exists():
        return None, None
    if not db_path.is_file():
        raise ValueError(f"Store path is not a regular file: {db_path}")
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0)) as conn:
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            if app_id != expected_app_id:
                return app_id, None
            row = conn.execute("SELECT value FROM metadata WHERE key='store_id'").fetchone()
            if row is None or not row[0]:
                return app_id, ""
            return app_id, str(uuid.UUID(row[0]))
    except (sqlite3.Error, ValueError) as error:
        raise ValueError(f"Invalid store identity at {db_path}: {error}") from error


def read_instance_binding(root):
    instance_root = _no_symlinks(root)
    marker = instance_root / INSTANCE_FILE
    if not marker.exists() and not marker.is_symlink():
        return None
    value = _read_private_json(marker, "Instance binding")
    expected = {"schema_version", "instance_id", "catalog_store_id", "review_store_id", "storage_roots"}
    if set(value) != expected or value["schema_version"] != 1:
        raise ValueError("Invalid instance binding schema")
    str(uuid.UUID(value["instance_id"]))
    for key in ("catalog_store_id", "review_store_id"):
        if value[key] is not None:
            str(uuid.UUID(value[key]))
    settings = for_root(instance_root)[0]
    value["storage_roots"] = _validate_storage_roots(
        value["storage_roots"], settings["markdown_roots"], settings["original_roots"],
    )
    return value


def validate_instance_roots(root, *, allow_missing=(), ignore_migration_lock=False):
    instance_root = _no_symlinks(root)
    migration = instance_root / MIGRATION_FILE
    if not ignore_migration_lock and (migration.exists() or migration.is_symlink()):
        raise ValueError("Interrupted store migration detected; run initialize --recover-migration")
    roots = storage_roots(instance_root)
    for key, rel in roots.items():
        _no_symlinks(instance_root / rel)
    store_artifacts = (
        ("catalog_home", "catalog.sqlite3"),
        ("review_events_home", "events.sqlite3"),
        ("intake_home", "intake.sqlite3"),
        ("context_index_db", None),
        ("diagnostics_log", None),
        ("selection_manifest", None),
    )
    for key, filename in store_artifacts:
        configured_rel = roots[key]
        default_rel = DEFAULT_STORAGE_ROOTS[key]
        if configured_rel != default_rel:
            configured_target = (instance_root / configured_rel / filename
                                 if filename else instance_root / configured_rel)
            default_target = (instance_root / default_rel / filename
                              if filename else instance_root / default_rel)
            if configured_target.exists() and default_target.exists():
                raise ValueError(
                    f"Ambiguous or partially moved storage root for {key}; "
                    "reconcile or run initialize --migrate-storage"
                )
    instance = read_instance_binding(instance_root)
    if instance is not None:
        if instance["storage_roots"] != roots:
            raise ValueError(
                "Configured storage_roots differ from initialized instance layout; "
                "run initialize --migrate-storage"
            )
        cat_db = instance_root / roots["catalog_home"] / "catalog.sqlite3"
        rev_db = instance_root / roots["review_events_home"] / "events.sqlite3"
        for label, db_path, expected_app, recorded_id in (
            ("catalog", cat_db, 0x4C434154, instance["catalog_store_id"]),
            ("review-event", rev_db, 0x4C524556, instance["review_store_id"]),
        ):
            if recorded_id is not None:
                if not db_path.exists():
                    if label in allow_missing:
                        continue
                    raise ValueError(f"Initialized instance is missing its {label} authority store")
                app_id, actual_id = _probe_store_id(db_path, expected_app)
                if app_id == expected_app and actual_id != recorded_id:
                    raise ValueError(f"Cross-instance or substituted {label} store detected")
    return roots


def record_instance_binding(root):
    instance_root = _no_symlinks(root)
    roots = validate_instance_roots(instance_root)
    existing = read_instance_binding(instance_root)
    cat_db = instance_root / roots["catalog_home"] / "catalog.sqlite3"
    rev_db = instance_root / roots["review_events_home"] / "events.sqlite3"
    _, cat_id = _probe_store_id(cat_db, 0x4C434154)
    _, rev_id = _probe_store_id(rev_db, 0x4C524556)
    if not cat_id or not rev_id:
        raise ValueError("Cannot record instance binding before catalog and review-event stores are initialized")
    if existing is not None:
        if existing["catalog_store_id"] != cat_id or existing["review_store_id"] != rev_id:
            raise ValueError("Cross-instance or substituted authority store detected")
        instance_id = existing["instance_id"]
    else:
        instance_id = str(uuid.uuid4())
    payload = {
        "schema_version": 1,
        "instance_id": instance_id,
        "catalog_store_id": cat_id,
        "review_store_id": rev_id,
        "storage_roots": roots,
    }
    _write_private_json(instance_root / INSTANCE_FILE, payload)
    return payload


def logical_corpus_root(root):
    instance_root = _no_symlinks(root)
    roots = validate_instance_roots(instance_root)
    instance = read_instance_binding(instance_root)
    cat_db = instance_root / roots["catalog_home"] / "catalog.sqlite3"
    rev_db = instance_root / roots["review_events_home"] / "events.sqlite3"
    _, cat_id = _probe_store_id(cat_db, 0x4C434154)
    _, rev_id = _probe_store_id(rev_db, 0x4C524556)
    return json.dumps({
        "instance_id": instance["instance_id"] if instance is not None else None,
        "catalog_store_id": cat_id,
        "review_store_id": rev_id,
        "storage_roots": roots,
    }, sort_keys=True, separators=(",", ":"))


def migrate_storage(root, *, interrupt_after=None, interrupt_before_journal=None):
    """Transactionally relocate configured storage roots within an instance."""
    from tools.catalog import catalog_store as catalog
    from tools.review_events import event_store as events

    instance_root = _no_symlinks(root)
    migration_path = instance_root / MIGRATION_FILE
    if migration_path.exists() or migration_path.is_symlink():
        raise ValueError("Interrupted store migration detected; run initialize --recover-migration")
    instance = read_instance_binding(instance_root)
    if instance is None:
        raise ValueError("Instance is not initialized; run initialize before --migrate-storage")
    from_roots = instance["storage_roots"]
    to_roots = storage_roots(instance_root)
    if from_roots == to_roots:
        return {"migrated": False, "instance_id": instance["instance_id"], "storage_roots": to_roots}
    if from_roots["extractions_home"] != to_roots["extractions_home"]:
        ext_src = _no_symlinks(instance_root / from_roots["extractions_home"])
        if ext_src.exists() and any(ext_src.rglob("extraction.json")):
            raise ValueError(
                "Cannot migrate a populated extractions_home automatically because registered "
                "catalog provenance and rendition locators bind the extraction path; reconcile "
                "rendition registrations explicitly"
            )
    cat_db = instance_root / from_roots["catalog_home"] / "catalog.sqlite3"
    rev_db = instance_root / from_roots["review_events_home"] / "events.sqlite3"
    with closing(catalog.connect(cat_db)) as conn:
        cat_state = catalog.inspect(conn)[0]
    with closing(events.connect(rev_db)) as conn:
        rev_state = events.inspect(conn, instance_root)[0]
    if (cat_state["store_id"] != instance["catalog_store_id"]
            or rev_state["store_id"] != instance["review_store_id"]):
        raise ValueError("Cross-instance or substituted authority store detected before migration")
    move_keys = (
        "catalog_home", "review_events_home", "intake_home", "extractions_home",
        "context_index_db", "diagnostics_log", "selection_manifest",
    )
    planned = []
    for key in move_keys:
        if from_roots[key] != to_roots[key]:
            src = _no_symlinks(instance_root / from_roots[key])
            dst = _no_symlinks(instance_root / to_roots[key])
            if dst.exists():
                raise ValueError(f"Target storage root already exists for {key}: {to_roots[key]}")
            if src.exists():
                planned.append({"key": key, "from": from_roots[key], "to": to_roots[key]})
    journal = {
        "schema_version": 1,
        "instance_id": instance["instance_id"],
        "from_roots": from_roots,
        "to_roots": to_roots,
        "catalog_state_token": cat_state["state_token"],
        "review_state_token": rev_state["state_token"],
        "planned_moves": planned,
        "completed_moves": [],
    }
    _write_private_json(migration_path, journal)
    for count, item in enumerate(planned, 1):
        key, src_rel, dst_rel = item["key"], item["from"], item["to"]
        src = instance_root / src_rel
        dst = instance_root / dst_rel
        dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        if key in ("context_index_db", "diagnostics_log"):
            src_lock = src.with_name(src.name + ".lock")
            dst_lock = dst.with_name(dst.name + ".lock")
            if src_lock.exists():
                shutil.move(str(src_lock), str(dst_lock))
        _fsync_dir(src.parent)
        _fsync_dir(dst.parent)
        if interrupt_before_journal is not None and count >= interrupt_before_journal:
            raise RuntimeError("Simulated interruption before journal update during storage migration")
        journal["completed_moves"].append(item)
        _write_private_json(migration_path, journal)
        if interrupt_after is not None and count >= interrupt_after:
            raise RuntimeError("Simulated interruption during storage migration")
    updated_instance = dict(instance)
    updated_instance["storage_roots"] = to_roots
    _write_private_json(instance_root / INSTANCE_FILE, updated_instance)
    migration_path.unlink()
    _fsync_dir(migration_path.parent)
    if catalog.status(instance_root)["state_token"] != cat_state["state_token"]:
        raise ValueError("Catalog state token changed unexpectedly across storage migration")
    if events.status(instance_root)["state_token"] != rev_state["state_token"]:
        raise ValueError("Review-event state token changed unexpectedly across storage migration")
    return {
        "migrated": True,
        "instance_id": instance["instance_id"],
        "storage_roots": to_roots,
        "moved": [item["key"] for item in journal["completed_moves"]],
    }


def recover_migration(root):
    """Roll back an interrupted storage migration and verify authority store lineage."""
    from tools.catalog import catalog_store as catalog
    from tools.review_events import event_store as events

    instance_root = _no_symlinks(root)
    migration_path = instance_root / MIGRATION_FILE
    if not migration_path.exists() and not migration_path.is_symlink():
        raise ValueError("No interrupted store migration to recover")
    journal = _read_private_json(migration_path, "Migration journal")
    moves = journal.get("planned_moves") or journal.get("completed_moves", [])
    rolled_back = []
    for move in reversed(moves):
        src = _no_symlinks(instance_root / move["from"])
        dst = _no_symlinks(instance_root / move["to"])
        if dst.exists() and src.exists():
            raise ValueError(f"Ambiguous rollback state for {move['key']}: both source and destination exist")
        if dst.exists():
            src.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            if move["key"] in ("context_index_db", "diagnostics_log"):
                src_lock = src.with_name(src.name + ".lock")
                dst_lock = dst.with_name(dst.name + ".lock")
                if dst_lock.exists():
                    shutil.move(str(dst_lock), str(src_lock))
            _fsync_dir(src.parent)
            _fsync_dir(dst.parent)
            rolled_back.append(move["key"])
    instance = read_instance_binding(instance_root)
    if instance is not None:
        instance["storage_roots"] = journal["from_roots"]
        _write_private_json(instance_root / INSTANCE_FILE, instance)
    cat_db = instance_root / journal["from_roots"]["catalog_home"] / "catalog.sqlite3"
    rev_db = instance_root / journal["from_roots"]["review_events_home"] / "events.sqlite3"
    with closing(catalog.connect(cat_db)) as conn:
        cat_state = catalog.inspect(conn)[0]
    with closing(events.connect(rev_db)) as conn:
        rev_state = events.inspect(conn, instance_root)[0]
    if (cat_state["state_token"] != journal["catalog_state_token"]
            or rev_state["state_token"] != journal["review_state_token"]):
        raise ValueError("Recovered store state does not match pre-migration journal")
    migration_path.unlink()
    _fsync_dir(migration_path.parent)
    return {
        "recovered": True,
        "instance_id": journal["instance_id"],
        "restored_roots": journal["from_roots"],
        "rolled_back": rolled_back,
    }
