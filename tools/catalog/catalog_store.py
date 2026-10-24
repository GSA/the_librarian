#!/usr/bin/env python3
"""Local metadata-only catalog: immutable revisions, explicit provenance, no document bodies."""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import unicodedata
import uuid

class CatalogError(ValueError):
    """A catalog contract or integrity violation."""


AUTHORITY = "catalog-metadata-only"
HOME = ".local/catalog"
DB_NAME = "catalog.sqlite3"
VERSION = 1
APPLICATION_ID = 0x4C434154
SUPERSEDED_APPLICATION_ID = 0x46434154
VALIDATOR_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
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
SQL = """
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE revisions(
 revision INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, actor TEXT NOT NULL,
 reason TEXT NOT NULL, committed_at TEXT NOT NULL, input_hash TEXT NOT NULL,
 record_count INTEGER NOT NULL CHECK(record_count>0), previous_hash TEXT NOT NULL,
 revision_hash TEXT NOT NULL);
CREATE TABLE versions(
 revision INTEGER NOT NULL REFERENCES revisions(revision), ordinal INTEGER NOT NULL,
 record_id TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
 PRIMARY KEY(revision,ordinal), UNIQUE(revision,record_id));
CREATE TABLE current_records(
 record_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
 ordinal INTEGER NOT NULL,
 FOREIGN KEY(revision,ordinal) REFERENCES versions(revision,ordinal));
CREATE INDEX versions_record ON versions(record_id,revision);
"""


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


def safe_path(path):
    path = Path(path)
    require(".." not in path.parts, "Filesystem traversal is forbidden")
    path = path.absolute()
    for component in reversed((path, *path.parents)):
        require(not component.is_symlink(), "Symlink paths are forbidden")
    return path


def private_directory(path):
    path = safe_path(path)
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(path.is_dir() and stat.S_IMODE(path.stat().st_mode) == 0o700,
            "Catalog directories must have mode 0700")
    for created in reversed(missing):
        fsync_directory(created.parent)
    return path


def private_file(path):
    path = safe_path(path)
    require(path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600,
            "Database/snapshot must exist as a private regular file (0600)")
    return path


def locations(root):
    root = safe_path(root)
    require(root.is_dir(), "Corpus root must exist")
    home = safe_path(root / HOME)
    return home, home / DB_NAME, home / "backups"


def fsync_directory(path):
    descriptor = os.open(safe_path(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def seal(path):
    descriptor = os.open(safe_path(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def reserve(path):
    descriptor = os.open(safe_path(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)


@contextmanager
def writer_lock(home):
    import fcntl
    private_directory(home)
    descriptor = os.open(safe_path(home / "writer.lock"),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
                "Writer lock must be a private regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise CatalogError("Another catalog writer is active; retry later") from error
        yield
    finally:
        os.close(descriptor)


def connect(path, write=False):
    path = private_file(path)
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = safe_path(str(path) + suffix)
        require(not sidecar.exists(), "SQLite sidecar present; explicit recovery required")
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, timeout=0, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        require(connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete",
                "Unsupported SQLite journal mode")
        if write:
            connection.execute("PRAGMA synchronous=FULL")
    except (CatalogError, sqlite3.Error):
        connection.close()
        raise
    return connection


def trigger_sql(table, action):
    return (f"CREATE TRIGGER immutable_{table}_{action} BEFORE {action.upper()} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'immutable catalog history'); END")


def schema_sql():
    """Complete storage structure without records, identities or filesystem operations."""
    triggers = "\n".join(
        trigger_sql(table, action) + ";"
        for table in ("metadata", "revisions", "versions")
        for action in ("update", "delete")
    )
    return f"PRAGMA application_id={APPLICATION_ID};\nPRAGMA user_version={VERSION};\n{SQL}\n{triggers}\n"


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def decode(value):
    try:
        return json.loads(value, object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(CatalogError("Nonfinite JSON")))
    except (ValueError, TypeError, RecursionError) as error:
        raise CatalogError("Invalid JSON") from error


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "Timestamp requires timezone")
    except (TypeError, ValueError) as error:
        raise CatalogError("Invalid timestamp") from error


def make_state(metadata, revision, head, count):
    state = {"authority": AUTHORITY, "store_id": metadata["store_id"], "revision": revision,
             "records": count, "head_hash": head}
    state["state_token"] = fingerprint(state)
    return state


def inspect(connection):
    """Verify physical integrity, every historical payload/graph, chain, and current projection."""
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    require(application_id != SUPERSEDED_APPLICATION_ID,
            "Superseded FCAT catalog identity; explicit migration or re-initialization is required")
    require(application_id == APPLICATION_ID, "Not a catalog store")
    require(connection.execute("PRAGMA user_version").fetchone()[0] == VERSION, "Unsupported catalog version")
    require([row[0] for row in connection.execute("PRAGMA integrity_check")] == ["ok"],
            "SQLite integrity check failed")
    require(not list(connection.execute("PRAGMA foreign_key_check")), "Foreign-key integrity check failed")
    metadata = dict(connection.execute("SELECT key,value FROM metadata"))
    exact(metadata, {"store_id", "created_at", "authority", "contract_version"}, "store metadata")
    require(metadata["authority"] == AUTHORITY and metadata["contract_version"] == str(VERSION),
            "Catalog contract mismatch")
    try:
        require(str(uuid.UUID(metadata["store_id"])) == metadata["store_id"], "Invalid store identity")
    except ValueError as error:
        raise CatalogError("Invalid store identity") from error
    timestamp(metadata["created_at"])
    for table in ("metadata", "revisions", "versions"):
        for action in ("update", "delete"):
            row = connection.execute("SELECT sql FROM sqlite_master WHERE name=? AND type='trigger'",
                                     (f"immutable_{table}_{action}",)).fetchone()
            require(row is not None and row[0] == trigger_sql(table, action), "Immutable trigger changed")
    batches = [dict(row) for row in connection.execute("SELECT * FROM revisions ORDER BY revision")]
    by_revision = {}
    for row in connection.execute("SELECT * FROM versions ORDER BY revision,ordinal"):
        by_revision.setdefault(row["revision"], []).append(dict(row))
    require(set(by_revision) == {batch["revision"] for batch in batches}, "Invalid revision membership")
    current, projection, histories, committed_states = {}, {}, {}, {}
    previous = ""
    for revision, batch in enumerate(batches, 1):
        require(batch["revision"] == revision and batch["previous_hash"] == previous, "Broken revision chain")
        text(batch["actor"], "Actor", 200)
        text(batch["request_id"], "Request ID", 200)
        text(batch["reason"], "Revision reason")
        timestamp(batch["committed_at"])
        members = by_revision[revision]
        require(batch["record_count"] == len(members) and members, "Revision count mismatch")
        records = []
        for ordinal, member in enumerate(members):
            require(member["ordinal"] == ordinal, "Broken revision ordinal")
            record = decode(member["payload"])
            validate_record(record)
            require(member["payload"] == canonical(record) and member["payload_hash"] == fingerprint(record),
                    "Payload integrity mismatch")
            require(member["record_id"] == record["id"], "Record identity mismatch")
            records.append(record)
            current[record["id"]] = record
            projection[record["id"]] = (revision, ordinal)
            histories.setdefault(record["id"], []).append({
                "revision": revision, "request_id": batch["request_id"], "actor": batch["actor"],
                "reason": batch["reason"], "committed_at": batch["committed_at"], "record": record,
            })
        validate_records(records)
        validate_graph(current)
        require(batch["input_hash"] == fingerprint({
            "actor": batch["actor"], "reason": batch["reason"], "records": records,
        }), "Revision input hash mismatch")
        require(batch["revision_hash"] == fingerprint({k: v for k, v in batch.items() if k != "revision_hash"}),
                "Revision chain hash mismatch")
        previous = batch["revision_hash"]
        committed_states[revision] = make_state(metadata, revision, previous, len(current))
    actual = {row["record_id"]: (row["revision"], row["ordinal"])
              for row in connection.execute("SELECT * FROM current_records")}
    require(actual == projection, "Current records differ from immutable revision history")
    state = make_state(metadata, len(batches), previous, len(current))
    return state, current, histories, batches, committed_states


def file_hash(path):
    digest = hashlib.sha256()
    descriptor = os.open(private_file(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(connection, backups, reason):
    owned_transaction = not connection.in_transaction
    if owned_transaction:
        connection.execute("BEGIN")
    try:
        return _snapshot_in_transaction(connection, backups, reason)
    finally:
        if owned_transaction and connection.in_transaction:
            connection.execute("ROLLBACK")


def _snapshot_in_transaction(connection, backups, reason):
    state = inspect(connection)[0]
    private_directory(backups)
    name = f"{state['store_id']}-r{state['revision']:08d}-{reason}-{uuid.uuid4().hex}"
    partial, final, receipt = (backups / (name + suffix) for suffix in (".partial", ".sqlite3", ".json"))
    reserve(partial)
    try:
        with closing(sqlite3.connect(partial)) as target:
            connection.backup(target)
        with closing(connect(partial)) as target:
            require(inspect(target)[0] == state, "Backup state mismatch")
        seal(partial)
        record = {"backup_version": VERSION, "created_at": now(), "reason": reason, "database": final.name,
                  "sha256": file_hash(partial), "bytes": partial.stat().st_size, "state": state}
        os.link(partial, final, follow_symlinks=False)
        partial.unlink()
        fsync_directory(backups)
        descriptor = os.open(safe_path(receipt), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(backups)
        return {"snapshot": str(final), "receipt": str(receipt), "sha256": record["sha256"], "state": state}
    finally:
        if partial.exists():
            partial.unlink()


def initialize(root):
    home, db, backups = locations(root)
    with writer_lock(home):
        require(not db.exists(), "Catalog already exists; init never overwrites")
        for suffix in ("-journal", "-wal", "-shm"):
            require(not safe_path(str(db) + suffix).exists(), "SQLite sidecar present; explicit recovery required")
        partial = home / ("init-" + uuid.uuid4().hex + ".partial")
        reserve(partial)
        published = False
        try:
            with closing(sqlite3.connect(partial, isolation_level=None)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(schema_sql())
                metadata = {"store_id": str(uuid.uuid4()), "created_at": now(),
                            "authority": AUTHORITY, "contract_version": str(VERSION)}
                connection.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
                state = inspect(connection)[0]
                initial = _snapshot(connection, backups, "init")
            seal(partial)
            os.link(partial, db, follow_symlinks=False)
            published = True
            partial.unlink()
            fsync_directory(home)
            return {"command": "init", **state, "state": state, "backup": initial}
        except (OSError, sqlite3.Error, CatalogError) as error:
            if published:
                raise CatalogError("Initialization committed, but durability failed; inspect status, do not reinitialize") from error
            raise
        finally:
            if partial.exists():
                partial.unlink()


def validated_state(connection, home, *, audit=False):
    """Reuse full inspection only for identical SQLite snapshot and validator bytes."""
    require(connection.in_transaction, "Validated reads require a transaction")
    require(hasattr(connection, "serialize"), "Validated reads require SQLite serialize support")
    require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == VALIDATOR_SHA256,
            "Catalog validator changed after import; restart the reader")
    # Acquire the read snapshot before hashing; a concurrent commit cannot change it.
    connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    binding = {
        "version": 1,
        "snapshot_sha256": hashlib.sha256(connection.serialize()).hexdigest(),
        "validator_sha256": VALIDATOR_SHA256,
        "runtime": [sys.version, sqlite3.sqlite_version, unicodedata.unidata_version],
    }
    cache_home = safe_path(home / ".local")
    receipt = safe_path(cache_home / "read-validation.json")
    if cache_home.exists():
        require(cache_home.is_dir() and stat.S_IMODE(cache_home.stat().st_mode) == 0o700,
                "Read-validation directory must be private (0700)")
    if receipt.exists() and not audit:
        require(receipt.stat().st_size <= 4096, "Invalid read-validation receipt; run audit")
        with private_file(receipt).open("rb") as stream:
            saved = decode(stream.read())
        exact(saved, {"binding", "state", "checksum"}, "read-validation receipt; run audit")
        require(saved["checksum"] == fingerprint({"binding": saved["binding"], "state": saved["state"]}),
                "Read-validation receipt checksum mismatch; run audit")
        if saved["binding"] == binding:
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            latest = connection.execute(
                "SELECT revision,revision_hash FROM revisions ORDER BY revision DESC LIMIT 1").fetchone()
            count = connection.execute("SELECT count(*) FROM current_records").fetchone()[0]
            state = make_state(metadata, latest["revision"] if latest else 0,
                               latest["revision_hash"] if latest else "", count)
            require(state == saved["state"], "Read-validation state mismatch; run audit")
            return state
    state = inspect(connection)[0]
    record = {"binding": binding, "state": state}
    record["checksum"] = fingerprint(record)
    private_directory(cache_home)
    partial = cache_home / (uuid.uuid4().hex + ".partial")
    reserve(partial)
    try:
        with partial.open("w", encoding="utf-8") as stream:
            stream.write(canonical(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, receipt)
        fsync_directory(cache_home)
    finally:
        if partial.exists():
            partial.unlink()
    return state


def status(root, *, audit=False):
    home, db, _ = locations(root)
    require(home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700, "Catalog home missing or not private")
    with closing(connect(db)) as connection:
        connection.execute("BEGIN")
        return validated_state(connection, home, audit=audit)


def submit(root, records, *, actor, request_id, expected_state, reason,
           precommit_check=None):
    """Submit metadata; an optional local guard runs inside the final transaction.

    Guards must raise CatalogError on failure and must not write this catalog.
    They are invocation-only checks, never part of a persisted request recipe.
    """
    text(actor, "Actor", 200)
    text(request_id, "Request ID", 200)
    text(reason, "Revision reason")
    # Validate and detach caller-owned dictionaries before hashing or persistence.
    validate_records(records)
    incoming = decode(canonical(records))
    validate_records(incoming)
    input_hash = fingerprint({"actor": actor, "reason": reason, "records": incoming})
    home, db, backups = locations(root)
    private_file(db)
    with writer_lock(home), closing(connect(db, write=True)) as connection:
        connection.execute("BEGIN")
        state, current, _, batches, committed_states = inspect(connection)
        prior = next((batch for batch in batches if batch["request_id"] == request_id), None)
        if prior is not None:
            require(prior["input_hash"] == input_hash, "Request ID reused with different actor, reason, or records")
            try:
                recovery = _snapshot(connection, backups, "retry")
            except (OSError, sqlite3.Error, CatalogError) as error:
                raise CatalogError("Request already committed; retry the same request: backup failed") from error
            if precommit_check is not None:
                precommit_check()
            return {"command": "submit", "idempotent": True, **state, "state": state,
                    "request_id": request_id, "committed_revision": prior["revision"],
                    "committed_at": prior["committed_at"], "committed_state": committed_states[prior["revision"]],
                    "backup": recovery}
        require(expected_state == state["state_token"], "Stale expected state; re-read before writing")
        resulting = dict(current)
        resulting.update((record["id"], record) for record in incoming)
        validate_graph(resulting)
        pre = _snapshot(connection, backups, "pre-write")
        connection.execute("COMMIT")
        committed = False
        revision = state["revision"] + 1
        try:
            connection.execute("BEGIN IMMEDIATE")
            require(inspect(connection)[0] == state, "Store changed outside the writer lock")
            batch = {"revision": revision, "request_id": request_id, "actor": actor, "reason": reason,
                     "committed_at": now(), "input_hash": input_hash, "record_count": len(incoming),
                     "previous_hash": state["head_hash"]}
            batch["revision_hash"] = fingerprint(batch)
            connection.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?)", tuple(batch.values()))
            for ordinal, record in enumerate(incoming):
                connection.execute("INSERT INTO versions VALUES (?,?,?,?,?)",
                                   (revision, ordinal, record["id"], canonical(record), fingerprint(record)))
                connection.execute(
                    "INSERT INTO current_records VALUES (?,?,?) ON CONFLICT(record_id) DO UPDATE "
                    "SET revision=excluded.revision, ordinal=excluded.ordinal", (record["id"], revision, ordinal))
            if precommit_check is not None:
                precommit_check()
            connection.execute("COMMIT")
            committed = True
            seal(db)
            post = _snapshot(connection, backups, "post-write")
            written = post["state"]
        except (OSError, sqlite3.Error, CatalogError) as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if committed:
                raise CatalogError(
                    f"Write committed at revision {revision}, but durability/backup failed; retry the same request"
                ) from error
            raise
        return {"command": "submit", "idempotent": False, **written, "state": written,
                "request_id": request_id, "committed_revision": revision, "committed_at": batch["committed_at"],
                "committed_state": written, "pre_backup": pre, "backup": post}


def read_records(root, *, kind=None, receipt=None, source=None, path=None, coverage=None,
                 audience=None, after=None, limit=25, expected_state=None, include_history=False):
    require(type(limit) is int and limit > 0, "limit must be a positive integer")
    require(type(include_history) is bool, "include_history must be boolean")
    for value, choices, name in ((kind, KINDS, "kind"), (coverage, COVERAGES, "coverage"),
                                 (audience, AUDIENCES, "audience")):
        if value is not None:
            choice(value, choices, name)
    if receipt is not None:
        identifier(receipt, "receipt")
    if source is not None:
        identifier(source, "source")
    if path is not None:
        path = safe_relative(path)
    if after is not None:
        text(after, "Paging cursor", 200)
        require(expected_state is not None, "Paging after requires a pinned expected_state")
    home, db, _ = locations(root)
    require(home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700, "Catalog home missing or not private")
    with closing(connect(db)) as connection:
        connection.execute("BEGIN")
        state = validated_state(connection, home)
        require(expected_state is None or expected_state == state["state_token"], "Stale query state")
        current_view(connection)
        scopes = []
        parameters = {}
        for label, value in (("receipt", receipt), ("source", source)):
            if value is None:
                continue
            parameters[label] = value
            receipts = ("SELECT id FROM catalog_current WHERE kind='receipt' AND id=:receipt"
                        if label == "receipt" else
                        "SELECT id FROM catalog_current WHERE kind='receipt' AND EXISTS "
                        "(SELECT 1 FROM json_each(payload,'$.data.source_ids') WHERE value=:source)")
            sources = ("SELECT value FROM catalog_current,json_each(payload,'$.data.source_ids') "
                       "WHERE catalog_current.id IN (SELECT id FROM linked_receipts)" if label == "receipt"
                       else "SELECT :source")
            scopes.append(f"""id IN (
                WITH linked_receipts AS ({receipts}), members AS (
                    SELECT id FROM linked_receipts UNION {sources}
                    UNION SELECT id FROM catalog_current WHERE kind='artifact'
                    AND json_extract(payload,'$.data.receipt_id') IN (SELECT id FROM linked_receipts)
                ), endpoints AS (
                    SELECT id FROM catalog_current WHERE kind IN ('artifact','source')
                    AND id IN (SELECT * FROM members)
                )
                SELECT * FROM members UNION SELECT id FROM catalog_current WHERE kind='relationship'
                AND (json_extract(payload,'$.data.from_id') IN (SELECT id FROM endpoints)
                  OR json_extract(payload,'$.data.to_id') IN (SELECT id FROM endpoints))
            )""")
        if kind is not None:
            scopes.append("kind=:kind")
            parameters["kind"] = kind
        for key, value in (("path", path), ("coverage", coverage), ("audience", audience)):
            if value is not None:
                scopes.append(f"(kind='artifact' AND json_extract(payload,'$.data.{key}')=:{key})")
                parameters[key] = value
        where = " AND ".join(scopes) or "1"
        query = "SELECT id FROM catalog_current WHERE " + where
        if after is not None:
            require(connection.execute(query + " AND id=:after", {**parameters, "after": after}).fetchone(),
                    "Paging cursor is not a member of the filtered result")
        page = connection.execute(
            "WITH matches AS (" + query + "), counted AS (SELECT id,count(*) OVER () AS total FROM matches) "
            "SELECT * FROM counted WHERE :after IS NULL OR id>:after ORDER BY id LIMIT :limit",
            {**parameters, "after": after, "limit": min(limit + 1, sys.maxsize)}).fetchall()
        total = page[0]["total"] if page else connection.execute(
            "SELECT count(*) FROM (" + query + ")", parameters).fetchone()[0]
        ids = [row["id"] for row in page[:limit]]
        result = []
        for record_id in ids:
            record = current_record(connection, record_id)
            if include_history:
                record["versions"] = [{
                    "revision": row["revision"], "request_id": row["request_id"], "actor": row["actor"],
                    "reason": row["reason"], "committed_at": row["committed_at"], "record": decode(row["payload"]),
                } for row in connection.execute(
                    "SELECT r.*,v.payload FROM versions v JOIN revisions r USING(revision) "
                    "WHERE v.record_id=? ORDER BY revision", (record_id,))]
            result.append(record)
        return {"authority": AUTHORITY, "state": state, "records": result, "count": len(result),
                "total_matching": total,
                "next_after": ids[-1] if len(page) > limit else None}


def current_view(connection):
    connection.execute("""CREATE TEMP VIEW catalog_current AS
        SELECT c.record_id AS id,json_extract(v.payload,'$.kind') AS kind,v.payload
        FROM current_records c JOIN versions v ON v.revision=c.revision AND v.ordinal=c.ordinal""")


def current_record(connection, record_id):
    row = connection.execute("SELECT payload FROM catalog_current WHERE id=?", (record_id,)).fetchone()
    require(row is not None, "Record missing from authoritative catalog: " + record_id)
    return decode(row["payload"])


def backup(root):
    home, db, backups = locations(root)
    private_file(db)
    with writer_lock(home), closing(connect(db)) as connection:
        connection.execute("BEGIN")
        return {"command": "backup", **_snapshot(connection, backups, "manual")}


def receipt_metadata(root, receipt_ids):
    """Read selected registrations in one validated snapshot, without paging child inventories."""
    for receipt_id in receipt_ids:
        identifier(receipt_id, "receipt")
    home, db, _ = locations(root)
    require(home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700, "Catalog home missing or not private")
    with closing(connect(db)) as connection:
        connection.execute("BEGIN")
        state = validated_state(connection, home)
        current_view(connection)
        selected = {}
        for receipt_id in receipt_ids:
            receipt = current_record(connection, receipt_id)
            require(receipt["kind"] == "receipt",
                    "Receipt missing from authoritative catalog: " + receipt_id)
            selected[receipt_id] = {
                "receipt": receipt, "artifact": current_record(connection, receipt["data"]["artifact_id"]),
                "sources": [current_record(connection, sid) for sid in receipt["data"]["source_ids"]],
            }
        return {"state": state, "registrations": selected}


def verify_backup(path):
    path = private_file(path)
    require(path.suffix == ".sqlite3", "Snapshot must be a .sqlite3 file")
    receipt = private_file(path.with_suffix(".json"))
    record = decode(receipt.read_text(encoding="utf-8"))
    exact(record, {"backup_version", "created_at", "reason", "database", "sha256", "bytes", "state"},
          "backup receipt")
    require(type(record["backup_version"]) is int and record["backup_version"] == VERSION
            and record["database"] == path.name, "Mismatched snapshot receipt")
    timestamp(record["created_at"])
    text(record["reason"], "Backup reason")
    hash_value(record["sha256"])
    require(type(record["bytes"]) is int and record["bytes"] >= 0, "Invalid snapshot size")
    require(path.stat().st_size == record["bytes"] and file_hash(path) == record["sha256"],
            "Snapshot bytes differ from receipt")
    with closing(connect(path)) as connection:
        connection.execute("BEGIN")
        state = inspect(connection)[0]
    require(state == record["state"], "Snapshot state differs from receipt")
    require(file_hash(path) == record["sha256"], "Snapshot changed while verifying")
    return state


def restore(root, snapshot, expected_state=None):
    source = private_file(snapshot)
    source_state = verify_backup(source)
    home, db, backups = locations(root)
    require(source != db, "Restore source cannot be the live database")
    with writer_lock(home):
        safety = None
        if db.exists():
            with closing(connect(db)) as connection:
                connection.execute("BEGIN")
                current = inspect(connection)[0]
                require(expected_state == current["state_token"], "Restore requires current expected_state")
                require(source_state["store_id"] == current["store_id"], "Cannot replace a different store lineage")
                safety = _snapshot(connection, backups, "pre-restore")
        else:
            require(expected_state is None, "Missing store cannot have replacement expectations")
        partial = home / ("restore-" + uuid.uuid4().hex + ".partial")
        reserve(partial)
        published = False
        try:
            with closing(connect(source)) as original, closing(sqlite3.connect(partial)) as target:
                original.backup(target)
            with closing(connect(partial)) as restored:
                require(inspect(restored)[0] == source_state, "Restored state mismatch")
            require(verify_backup(source) == source_state, "Restore source changed")
            seal(partial)
            for suffix in ("-journal", "-wal", "-shm"):
                require(not safe_path(str(db) + suffix).exists(), "SQLite sidecar present; explicit recovery required")
            if safety is None:
                os.link(partial, db, follow_symlinks=False)
                published = True
                partial.unlink()
            else:
                # Check immediately before replacement, rather than treating a corrupt live DB as disposable.
                require(status(root) == current, "Live catalog changed during restore")
                os.replace(partial, db)
                published = True
            fsync_directory(home)
            with closing(connect(db)) as restored:
                restored.execute("BEGIN")
                require(inspect(restored)[0] == source_state, "Published restore state mismatch")
                recovery = _snapshot(restored, backups, "post-restore")
            return {"command": "restore", **source_state, "state": source_state,
                    "pre_backup": safety, "backup": recovery}
        except (OSError, sqlite3.Error, CatalogError) as error:
            if published:
                raise CatalogError("Restore committed, but verification/durability/backup failed; inspect status and run backup") from error
            raise
        finally:
            if partial.exists():
                partial.unlink()


def render_export(page, filters):
    payload = json.dumps({"generated_at": now(), "filters": filters, **page},
                         ensure_ascii=True, sort_keys=True, indent=2)
    longest = max((len(match[0]) for match in re.finditer(r"`+", payload)), default=0)
    fence = "`" * max(3, longest + 1)
    return ("# Catalog metadata query export\n\n"
            "One-off view; metadata only, not source verification or readiness acceptance.\n\n"
            f"{fence}json\n{payload}\n{fence}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--max-chars", type=int, default=60000)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "audit", "backup"):
        commands.add_parser(name)
    verify = commands.add_parser("verify-backup")
    verify.add_argument("snapshot", type=Path)
    recover = commands.add_parser("restore")
    recover.add_argument("snapshot", type=Path)
    recover.add_argument("--expected-state")
    write = commands.add_parser("submit")
    write.add_argument("input", type=Path)
    for option in ("actor", "request-id", "expected-state", "reason"):
        write.add_argument("--" + option, required=True)
    for name in ("query", "export"):
        query = commands.add_parser(name)
        for option in ("kind", "receipt", "source", "path", "coverage", "audience", "after", "expected-state"):
            query.add_argument("--" + option)
        query.add_argument("--limit", type=int, default=25)
        query.add_argument("--include-history", action="store_true")
    args = parser.parse_args(argv)
    try:
        require(type(args.max_chars) is int and args.max_chars > 0, "Output budget must be positive")
        if args.command in {"init", "submit", "backup", "restore"}:
            require(args.max_chars >= 60000, "Mutations require max-chars >=60000")
        if args.command == "init":
            result = initialize(args.root)
        elif args.command == "status":
            result = status(args.root)
        elif args.command == "audit":
            result = status(args.root, audit=True)
        elif args.command == "backup":
            result = backup(args.root)
        elif args.command == "verify-backup":
            result = verify_backup(args.snapshot)
        elif args.command == "restore":
            result = restore(args.root, args.snapshot, args.expected_state)
        elif args.command == "submit":
            value = decode(safe_path(args.input).read_text(encoding="utf-8"))
            exact(value, {"records"}, "submission")
            result = submit(args.root, value["records"], actor=args.actor, request_id=args.request_id,
                            expected_state=args.expected_state, reason=args.reason)
        else:
            filters = {key: getattr(args, key) for key in
                       ("kind", "receipt", "source", "path", "coverage", "audience",
                        "after", "limit", "expected_state", "include_history")}
            result = read_records(args.root, **filters)
        output = render_export(result, filters) if args.command == "export" else canonical(result) + "\n"
        require(len(output) <= args.max_chars, "Complete output exceeds budget; no fields were truncated")
        sys.stdout.write(output)
        return 0
    except (CatalogError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError) as error:
        message = str(error) if isinstance(error, CatalogError) else type(error).__name__
        print("catalog-store: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
