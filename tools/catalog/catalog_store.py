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
import threading
import time
import sys
import unicodedata
import uuid

from tools import backups as snapshots, policy
from tools.file_identity import file_identity
from tools.catalog.contracts import (
    AUDIENCES, COVERAGES, EXTRACTIONS, KEYS, KINDS, PRESENCES, ROLES,
    CatalogError, ConflictingWrite, StaleState, WriterBusy, canonical, choice, exact, fingerprint, hash_value, identifier,
    now, path_value, refs, require, safe_relative, text, validate_graph, GraphValidator,
    validate_record, validate_records, validate_delta, _references,
)

AUTHORITY = "catalog-metadata-only"
HOME = ".local/catalog"
DB_NAME = "catalog.sqlite3"
VERSION = 1
APPLICATION_ID = 0x4C434154
SUPERSEDED_APPLICATION_ID = 0x46434154
VALIDATOR_FILES = (Path(__file__), Path(__file__).with_name("contracts.py"))


def validator_sha256():
    digest = hashlib.sha256()
    for path in VALIDATOR_FILES:
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


VALIDATOR_SHA256 = validator_sha256()
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

# Derived lookup structures (Kata 1h7h). They replace the substring scans that referrer and path-claim
# checks used, hold nothing that is not already in ``versions`` and are verified against it on every
# replay. A catalog either has none of them (``legacy``, scans) or all of them exactly as written here.
LOOKUP_FIELDS = ("path", "receipt_id", "parent_id", "artifact_id", "from_id", "to_id")
REFERENCE_FIELDS = ("receipt_id", "parent_id", "artifact_id", "from_id", "to_id")


def _lookup_expression(field):
    return f"json_extract(payload,'$.data.{field}')"


LOOKUP_OBJECTS = {
    **{f"versions_lookup_{field}": (
        "index", f"CREATE INDEX versions_lookup_{field} ON versions({_lookup_expression(field)}) "
                 f"WHERE {_lookup_expression(field)} IS NOT NULL") for field in LOOKUP_FIELDS},
    "refs": ("table", "CREATE TABLE refs(ref TEXT NOT NULL, revision INTEGER NOT NULL, ordinal INTEGER NOT NULL, "
                      "PRIMARY KEY(ref,revision,ordinal)) WITHOUT ROWID"),
    "versions_lookup_refs": ("trigger", "CREATE TRIGGER versions_lookup_refs AFTER INSERT ON versions "
                             "WHEN json_extract(NEW.payload,'$.kind')='receipt' BEGIN "
                             "INSERT OR IGNORE INTO refs SELECT j.value,NEW.revision,NEW.ordinal "
                             "FROM json_each(NEW.payload,'$.data.source_ids') j; END"),
}
REFS_FROM_VERSIONS = ("INSERT OR IGNORE INTO refs SELECT j.value,v.revision,v.ordinal FROM versions v, "
                      "json_each(v.payload,'$.data.source_ids') j WHERE json_extract(v.payload,'$.kind')='receipt'")



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


def locations(root, *, allow_missing=False):
    root = safe_path(root)
    require(root.is_dir(), "Corpus root must exist")
    try:
        rel_home = policy.validate_instance_roots(
            root, allow_missing=("catalog", "review-event") if allow_missing else ()
        )["catalog_home"]
    except ValueError as error:
        raise CatalogError(str(error)) from error
    home = safe_path(root / rel_home)
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


# Seconds a writer waits for another process's catalog lock before WriterBusy.
WRITER_WAIT_SECONDS = 10.0
# Seconds a SQLite reader waits for a writer's in-progress commit.
BUSY_TIMEOUT_SECONDS = 10.0
_held = threading.local()


@contextmanager
def writer_lock(home, wait=None):
    import fcntl
    private_directory(home)
    key = str(safe_path(home))
    held = getattr(_held, "homes", None)
    if held is None:
        held = _held.homes = set()
    # Re-entry from this thread can never succeed by waiting, so it fails at once.
    wait = 0 if key in held else WRITER_WAIT_SECONDS if wait is None else wait
    descriptor = os.open(safe_path(home / "writer.lock"),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
                "Writer lock must be a private regular file")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise WriterBusy("Another catalog writer is active; retry later") from error
                time.sleep(0.05)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
    finally:
        os.close(descriptor)


def _writer_active(home):
    """Whether another holder has the catalog writer lock (never this thread)."""
    import fcntl
    if str(safe_path(home)) in getattr(_held, "homes", ()):
        return False
    try:
        descriptor = os.open(safe_path(home / "writer.lock"), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)


def _await_journal(path):
    """Let a reader wait out a supported writer's rollback journal, boundedly.

    A journal left without a writer holding the lock is abandoned and still
    needs explicit recovery.
    """
    journal = Path(str(path) + "-journal")
    deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
    while journal.exists() and _writer_active(path.parent):
        if time.monotonic() >= deadline:
            raise WriterBusy("Another catalog writer is active; retry later")
        time.sleep(0.02)


def _journal_state(path):
    """Classify the rollback journal as none, live (a writer holds the lock) or abandoned.

    Holding the lock shared while looking keeps a writer from starting between
    the writer check and the journal check, and a finishing writer deletes its
    journal before releasing the lock, so neither is mistaken for abandoned.
    """
    import fcntl
    journal = safe_path(str(path) + "-journal")
    home = path.parent
    if str(safe_path(home)) in getattr(_held, "homes", ()):
        return "abandoned" if journal.exists() else "none"
    try:
        descriptor = os.open(safe_path(home / "writer.lock"), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return "abandoned" if journal.exists() else "none"
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "live" if journal.exists() else "none"
        return "abandoned" if journal.exists() else "none"
    finally:
        os.close(descriptor)


class _Connection(sqlite3.Connection):
    """Remembers the (device, inode) it opened, so a validation is never bound to a different
    file that a concurrent restore moved into the path afterwards."""
    opened = None


def _file_key(path):
    info = os.stat(path)
    return (info.st_dev, info.st_ino)


def connect(path, write=False):
    path = private_file(path)
    if not write:
        _await_journal(path)
    deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
    while True:
        for suffix in ("-wal", "-shm"):
            require(not safe_path(str(path) + suffix).exists(), "SQLite sidecar present; explicit recovery required")
        state = _journal_state(path)
        if state == "none":
            break
        # A writer can journal between _await_journal and here: a live writer's
        # journal is waited out; only one with no writer is abandoned.
        require(state == "live" and not write, "SQLite sidecar present; explicit recovery required")
        if time.monotonic() >= deadline:
            raise WriterBusy("Another catalog writer is active; retry later")
        time.sleep(0.02)
    before = _file_key(path)
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None,
                                 factory=_Connection)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        require(connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete",
                "Unsupported SQLite journal mode")
        if write:
            connection.execute("PRAGMA synchronous=FULL")
        # The pragmas above opened the file; the path must still be the same file.
        require(_file_key(path) == before, "Catalog file replaced while opening; retry")
        connection.opened = before
    except (CatalogError, OSError, sqlite3.Error):
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
    lookup = "\n".join(sql + ";" for _, sql in LOOKUP_OBJECTS.values())
    return (f"PRAGMA application_id={APPLICATION_ID};\nPRAGMA user_version={VERSION};\n{SQL}\n{triggers}\n"
            f"{lookup}\n")


LOOKUP_REFUSAL = ("Catalog lookup structures are incomplete or differ from the revision history; "
                  "run migrate-indexes --rebuild")


def lookup_shape(connection):
    """``legacy`` (no lookup structures), ``indexed`` (all, exactly as written) or ``partial`` (anything else)."""
    names = list(LOOKUP_OBJECTS)
    found = {row[0]: (row[1], row[2]) for row in connection.execute(
        f"SELECT name,type,sql FROM sqlite_master WHERE name IN ({','.join('?' * len(names))})", names)}
    if not found:
        return "legacy"
    return "indexed" if found == {name: LOOKUP_OBJECTS[name] for name in names} else "partial"


def drop_lookup(connection):
    """Remove every lookup structure (a catalog without them is the ``legacy`` shape)."""
    for name, (kind, _) in reversed(list(LOOKUP_OBJECTS.items())):
        connection.execute(f"DROP {kind.upper()} IF EXISTS {name}")


def create_lookup(connection):
    """Create the lookup structures and fill ``refs`` from ``versions``."""
    for name, (kind, sql) in LOOKUP_OBJECTS.items():
        if kind != "trigger":
            connection.execute(sql)
    connection.execute(REFS_FROM_VERSIONS)
    connection.execute(LOOKUP_OBJECTS["versions_lookup_refs"][1])


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


# Payloads proven valid by validate_record. Validation is a pure function of the
# exact payload text, so re-inspecting immutable history need not repeat it. Keyed by the
# SHA-256 of the payload text (32 bytes each, not the text), so the memo stays small.
_VALID_PAYLOADS = set()
_VALID_PAYLOADS_LIMIT = 500_000


def _member_record(member):
    """Decode and check one stored payload; returns (record, payload text).

    The fast path decodes once, validates, encodes the canonical text once and compares both the
    text and its digest with what is stored. Any failure at all falls back to the original
    step-by-step checks, which raise the refusal for that input.
    """
    payload = member["payload"]
    try:
        record = json.loads(payload)
        digest = hashlib.sha256(payload.encode("utf-8")).digest()
        if digest not in _VALID_PAYLOADS:
            validate_record(record)
        if canonical(record) == payload and member["payload_hash"] == digest.hex():
            if len(_VALID_PAYLOADS) >= _VALID_PAYLOADS_LIMIT:
                _VALID_PAYLOADS.clear()
            _VALID_PAYLOADS.add(digest)
            return record, payload
    except (ValueError, TypeError, RecursionError, KeyError, AttributeError, UnicodeError):
        pass
    record = decode(payload)
    validate_record(record)
    require(payload == canonical(record) and member["payload_hash"] == fingerprint(record),
            "Payload integrity mismatch")
    return record, payload


def inspect(connection, detail=True):
    """Verify physical integrity, every historical payload/graph, chain, and current projection.

    Returns ``(state, current, histories, batches, committed_states)``. History is streamed, so
    with ``detail=False`` nothing is retained beyond the checks and only ``state`` is filled in
    (the other four are None); callers that need the records or revisions ask for the detail.
    """
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
    shape = lookup_shape(connection)
    require(shape != "partial", LOOKUP_REFUSAL)
    require({row[0] for row in connection.execute("SELECT DISTINCT revision FROM versions")}
            == {row[0] for row in connection.execute("SELECT revision FROM revisions")},
            "Invalid revision membership")
    expected_refs = set()  # what the payloads imply for the lookup table, when the catalog has one
    batches = [] if detail else None
    current, projection, histories, committed_states = ({}, {}, {}, {}) if detail else (None, {}, None, None)
    graph = GraphValidator()  # same refusals as validate_graph(current), without re-walking history
    previous, revision = "", 0
    members_cursor = connection.execute("SELECT * FROM versions ORDER BY revision,ordinal")
    ahead = members_cursor.fetchone()
    for revision, batch in enumerate(connection.execute("SELECT * FROM revisions ORDER BY revision"), 1):
        batch = dict(batch)
        require(batch["revision"] == revision and batch["previous_hash"] == previous, "Broken revision chain")
        text(batch["actor"], "Actor", 200)
        text(batch["request_id"], "Request ID", 200)
        text(batch["reason"], "Revision reason")
        timestamp(batch["committed_at"])
        members = []
        while ahead is not None and ahead["revision"] == revision:
            members.append(ahead)
            ahead = members_cursor.fetchone()
        require(batch["record_count"] == len(members) and members, "Revision count mismatch")
        records, payloads = [], []
        for ordinal, member in enumerate(members):
            require(member["ordinal"] == ordinal, "Broken revision ordinal")
            record, payload = _member_record(member)
            require(member["record_id"] == record["id"], "Record identity mismatch")
            records.append(record)
            payloads.append(payload)
            projection[record["id"]] = (revision, ordinal)
            if shape == "indexed" and record["kind"] == "receipt":
                expected_refs.update((source_id, revision, ordinal) for source_id in record["data"]["source_ids"])
            if detail:
                current[record["id"]] = record
                histories.setdefault(record["id"], []).append({
                    "revision": revision, "request_id": batch["request_id"], "actor": batch["actor"],
                    "reason": batch["reason"], "committed_at": batch["committed_at"], "record": record,
                })
        # Each record was validated above; only validate_records' list checks remain.
        require(bool(records), "records must be a nonempty list")
        require(len({record["id"] for record in records}) == len(records), "Duplicate IDs in submission")
        graph.apply(records)
        # The canonical text of {"actor","reason","records"} with the (verified canonical) payloads in place.
        require(batch["input_hash"] == hashlib.sha256((
            f'{{"actor":{canonical(batch["actor"])},"reason":{canonical(batch["reason"])},'
            f'"records":[{",".join(payloads)}]}}').encode("utf-8")).hexdigest(),
            "Revision input hash mismatch")
        require(batch["revision_hash"] == fingerprint({k: v for k, v in batch.items() if k != "revision_hash"}),
                "Revision chain hash mismatch")
        previous = batch["revision_hash"]
        if detail:
            batches.append(batch)
            committed_states[revision] = make_state(metadata, revision, previous, len(projection))
    count = len(projection)
    rows = 0
    for row in connection.execute("SELECT * FROM current_records"):
        rows += 1
        require(projection.get(row["record_id"]) == (row["revision"], row["ordinal"]),
                "Current records differ from immutable revision history")
    require(rows == count, "Current records differ from immutable revision history")
    if shape == "indexed":
        require({tuple(row) for row in connection.execute("SELECT ref,revision,ordinal FROM refs")} == expected_refs,
                LOOKUP_REFUSAL)
    state = make_state(metadata, revision, previous, count)
    return state, current, histories, batches, committed_states


def file_hash(path):
    digest = hashlib.sha256()
    descriptor = os.open(private_file(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _daily_snapshot(root, connection, backups, state, inspected=False):
    """Verified snapshot before the write, only when the UTC day has none; then bounded retention.

    The snapshot is the serialized image of the transaction that is fully inspected here (or was
    just inspected by the caller, ``inspected``), so the history is replayed once for the day; the
    published bytes are then bound to that image by hash instead of being replayed again.
    """
    return snapshots.daily_snapshot(
        backups, store_id=state["store_id"],
        take=lambda: _snapshot_in_transaction(connection, backups, "daily", image=True, inspected=inspected),
        verify=verify_backup, verify_fresh=lambda path: verify_backup(path, replay=False),
        errors=(CatalogError,), days=policy.backup_retention_days(root))


def backup_inventory(root):
    return snapshots.inventory(locations(root)[2])


def prune(root, *, apply=False):
    """Operator prune: newest verified snapshot per UTC day inside the window; dry run by default."""
    home, db, backups = locations(root)
    private_file(db)
    with writer_lock(home), closing(connect(db)) as connection:
        connection.execute("BEGIN")
        store_id = inspect(connection, detail=False)[0]["store_id"]
        # Hold the writer lock so prune cannot overlap a write's retention or a restore.
        return snapshots.prune(backups, store_id=store_id, verify=verify_backup, errors=(CatalogError,),
                               days=policy.backup_retention_days(root), apply=apply, thin=True)


def _snapshot(connection, backups, reason):
    owned_transaction = not connection.in_transaction
    if owned_transaction:
        connection.execute("BEGIN")
    try:
        return _snapshot_in_transaction(connection, backups, reason)
    finally:
        if owned_transaction and connection.in_transaction:
            connection.execute("ROLLBACK")


def _partial_hash(path):
    return file_hash(path)


def _snapshot_in_transaction(connection, backups, reason, image=False, inspected=False):
    """Publish a snapshot of the open transaction.

    By default the SQLite backup API copies the database and the copy is fully inspected. With
    ``image`` the open transaction (inspected here, unless ``inspected``) is written as its
    serialized image and the written bytes must hash to that image; the copy is not replayed again.
    """
    state = inspect(connection, detail=False)[0] if not (image and inspected) else _head_state(connection)
    private_directory(backups)
    name = f"{state['store_id']}-r{state['revision']:08d}-{reason}-{uuid.uuid4().hex}"
    partial, final, receipt = (backups / (name + suffix) for suffix in (".partial", ".sqlite3", ".json"))
    reserve(partial)
    try:
        if image:
            data = connection.serialize()
            expected = hashlib.sha256(data).hexdigest()
            descriptor = os.open(safe_path(partial), os.O_WRONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            del data
            written = _partial_hash(partial)
            require(written == expected and partial.stat().st_size > 0, "Backup bytes mismatch")
        else:
            with closing(sqlite3.connect(partial)) as target:
                connection.backup(target)
            with closing(connect(partial)) as target:
                require(inspect(target, detail=False)[0] == state, "Backup state mismatch")
            seal(partial)
            written = _partial_hash(partial)
        record = {"backup_version": VERSION, "created_at": snapshots.utcnow().isoformat(), "reason": reason, "database": final.name,
                  "sha256": written, "bytes": partial.stat().st_size, "state": state}
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
                state = inspect(connection, detail=False)[0]
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


def migrate_indexes(root, *, rebuild=False, check=False):
    """Add the lookup structures to a catalog that lacks them (Kata 1h7h); idempotent and crash-safe.

    One writer transaction under the writer lock: fully inspect the catalog, publish a verified
    pre-migration snapshot, create the indexes and fill ``refs``, fully inspect again and require the
    identical state, then commit. Any failure rolls back (a killed process leaves SQLite's rollback
    journal, which restores the earlier file when next opened). Authority data, the schema version
    and every state token are unchanged. A catalog that already has the structures is only verified.
    ``rebuild`` first drops whatever lookup structures exist (damaged or incomplete) in the same
    transaction, so it succeeds only when everything else in the catalog verifies. ``check``
    reports the shape and verifies without writing.
    """
    home, db, backups = locations(root)
    private_file(db)
    if check:
        with closing(connect(db)) as connection:
            connection.execute("BEGIN")
            shape = lookup_shape(connection)
            state = None if shape == "partial" else inspect(connection, detail=False)[0]
            return {"command": "migrate-indexes", "migrated": False, "lookup": shape, "state": state}
    with writer_lock(home), closing(connect(db, write=True)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            shape = lookup_shape(connection)
            if shape == "partial" and not rebuild:
                raise CatalogError(LOOKUP_REFUSAL)
            if shape == "indexed" and not rebuild:
                state = inspect(connection, detail=False)[0]
                connection.execute("ROLLBACK")
                return {"command": "migrate-indexes", "migrated": False, "lookup": "indexed", "state": state,
                        "backup": None}
            if shape != "legacy":
                drop_lookup(connection)
            state = inspect(connection, detail=False)[0]
            taken = _snapshot_in_transaction(connection, backups, "pre-index-migration", image=True, inspected=True)
            verify_backup(Path(taken["snapshot"]), replay=False)
            create_lookup(connection)
            require(inspect(connection, detail=False)[0] == state, "Catalog changed during index migration")
            require(lookup_shape(connection) == "indexed", "Lookup structures were not created as specified")
            connection.execute("COMMIT")
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        seal(db)
        _attest_commit(connection, home, state)
        return {"command": "migrate-indexes", "migrated": True, "lookup": "indexed", "state": state,
                "backup": taken}


def _binding(connection):
    """What a validation covers: the database file, the validator bytes and the runtime.

    The file is bound by its identity (device, inode, size, mtime, ctime, SQLite change counter,
    decision 0017) when it is a plain rollback-journal file; otherwise by the SHA-256 of its exact
    bytes. Either way it is taken once this connection holds the read snapshot, so no commit can
    land between the check and the validation it vouches for.
    """
    require(connection.in_transaction, "Validated reads require a transaction")
    require(hasattr(connection, "serialize"), "Validated reads require SQLite serialize support")
    require(validator_sha256() == VALIDATOR_SHA256,
            "Catalog validator changed after import; restart the reader")
    # Acquire the read snapshot before looking at the file; a concurrent commit cannot change it.
    connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    runtime = [sys.version, sqlite3.sqlite_version, unicodedata.unidata_version]
    path = connection.execute("PRAGMA database_list").fetchone()["file"]
    identity = file_identity(path, connection)
    opened = getattr(connection, "opened", None)
    # A connection this module did not open, or whose file has since been replaced at the path,
    # cannot be described by the path's identity: bind the bytes it actually reads instead.
    if identity is not None and (opened is None or opened != (identity[0], identity[1])):
        identity = None
    if identity is not None:
        return {"version": 2, "identity": identity, "validator_sha256": VALIDATOR_SHA256, "runtime": runtime}
    return {"version": 1, "snapshot_sha256": hashlib.sha256(connection.serialize()).hexdigest(),
            "validator_sha256": VALIDATOR_SHA256, "runtime": runtime}


def _today():
    return snapshots.utcnow().strftime("%Y-%m-%d")


def _head_state(connection):
    """State derived from the metadata, newest revision and current-record count alone."""
    metadata = dict(connection.execute("SELECT key,value FROM metadata"))
    latest = connection.execute(
        "SELECT revision,revision_hash FROM revisions ORDER BY revision DESC LIMIT 1").fetchone()
    count = connection.execute("SELECT count(*) FROM current_records").fetchone()[0]
    return make_state(metadata, latest["revision"] if latest else 0,
                      latest["revision_hash"] if latest else "", count)


def _write_receipt(home, binding, state, validated_on=None):
    """``validated_on`` is the UTC date of the last full validation this receipt descends from."""
    cache_home = safe_path(home / ".local")
    receipt = safe_path(cache_home / "read-validation.json")
    record = {"binding": binding, "state": state, "validated_on": validated_on or _today()}
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


def _trusted_state(connection, home, binding):
    """``(state, validated_on)`` a receipt vouches for when it covers exactly this file, else None.

    An identity-bound receipt (0017) is trusted only on the UTC day of its last full validation,
    so a change the identity cannot see is found by the first full check of the next day. A
    receipt from before 0017 carries no date and is a miss, never a refusal.
    """
    cache_home = safe_path(home / ".local")
    receipt = safe_path(cache_home / "read-validation.json")
    if cache_home.exists():
        require(cache_home.is_dir() and stat.S_IMODE(cache_home.stat().st_mode) == 0o700,
                "Read-validation directory must be private (0700)")
    if not receipt.exists():
        return None
    require(receipt.stat().st_size <= 4096, "Invalid read-validation receipt; run audit")
    with private_file(receipt).open("rb") as stream:
        saved = decode(stream.read())
    legacy = isinstance(saved, dict) and set(saved) == {"binding", "state", "checksum"}
    exact(saved, {"binding", "state", "checksum"} if legacy else {"binding", "state", "checksum", "validated_on"},
          "read-validation receipt; run audit")
    signed = {key: saved[key] for key in saved if key != "checksum"}
    require(saved["checksum"] == fingerprint(signed), "Read-validation receipt checksum mismatch; run audit")
    if legacy or saved["binding"] != binding:
        return None
    if binding["version"] == 2 and saved["validated_on"] != _today():
        return None
    state = _head_state(connection)
    require(state == saved["state"], "Read-validation state mismatch; run audit")
    return state, saved["validated_on"]


def validated_state(connection, home, *, audit=False):
    """Reuse full inspection only for the identical database file and validator bytes."""
    binding = _binding(connection)
    trusted = None if audit else _trusted_state(connection, home, binding)
    if trusted is not None:
        return trusted[0]
    state = inspect(connection, detail=False)[0]
    _write_receipt(home, binding, state)
    return state


def status(root, *, audit=False):
    home, db, _ = locations(root)
    require(home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700, "Catalog home missing or not private")
    with closing(connect(db)) as connection:
        connection.execute("BEGIN")
        return validated_state(connection, home, audit=audit)


def submit(root, records, *, actor, request_id, expected_state, reason,
           precommit_check=None, rebase=False):
    """Submit metadata; an optional local guard runs inside the final transaction.

    With ``rebase``, an outdated ``expected_state`` is accepted when no later
    revision wrote any record this request writes; the full graph is still
    validated against the current state. Overlap raises ConflictingWrite.

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
        binding = _binding(connection)
        # A receipt for these exact bytes (a prior full inspection, or this writer's own
        # validated commit) stands in for the O(history) replay; any other bytes get the full one.
        try:
            trusted = _trusted_state(connection, home, binding)
        except (CatalogError, OSError):
            trusted = None  # an unusable receipt only costs the full replay; it never blocks a write
        validated_on = None  # a full replay below (or the daily snapshot's) starts a new day
        if trusted is None:
            state, current, _, _, _ = inspect(connection)
        else:
            (state, validated_on), current = trusted, None
        committed = _CommittedStates(connection, state)
        prior = committed.prior(request_id)
        if prior is not None:
            require(prior["input_hash"] == input_hash, "Request ID reused with different actor, reason, or records")
            if trusted is None:
                # The replay above fully validated this file; keep the next retry or read from repeating it.
                try:
                    _write_receipt(home, binding, state)
                except (OSError, CatalogError):
                    pass
            if precommit_check is not None:
                precommit_check()
            return {"command": "submit", "idempotent": True, **state, "state": state,
                    "request_id": request_id, "committed_revision": prior["revision"],
                    "committed_at": prior["committed_at"], "committed_state": committed.state(prior["revision"]),
                    "backup": None, "retention": None}
        rebased_from = None
        if expected_state != state["state_token"]:
            if not rebase:
                raise StaleState("Stale expected state; re-read before writing")
            rebased_from = committed.revision_of(expected_state)
            if rebased_from is None:
                raise ConflictingWrite("Pinned state is not in this catalog's history")
            if committed.written_after(rebased_from, [record["id"] for record in incoming]):
                raise ConflictingWrite("A later revision rewrote a record in this request")
        old = _CurrentRecords(connection)
        added = sum(1 for record in incoming if old.get(record["id"]) is None)
        if current is not None:
            resulting = dict(current)
            resulting.update((record["id"], record) for record in incoming)
            validate_graph(resulting)
        else:
            validate_delta(incoming, get=old.get, referrers=old.referrers, claimants=old.claimants)
        taken, retention = _daily_snapshot(root, connection, backups, state, trusted is None)
        connection.execute("COMMIT")
        committed_flag = False
        revision = state["revision"] + 1
        try:
            connection.execute("BEGIN IMMEDIATE")
            require(_binding(connection) == binding, "Store changed outside the writer lock")
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
            committed_flag = True
            seal(db)
            written = make_state(state, revision, batch["revision_hash"], state["records"] + added)
        except (OSError, sqlite3.Error, CatalogError) as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if committed_flag:
                raise CatalogError(
                    f"Write committed at revision {revision}, but durability/backup failed; retry the same request"
                ) from error
            raise
        # A snapshot verification replays everything, so only then does the full-check date advance.
        _attest_commit(connection, home, written, None if taken is not None else validated_on)
        return {"command": "submit", "idempotent": False, **written, "state": written,
                "request_id": request_id, "committed_revision": revision, "committed_at": batch["committed_at"],
                "committed_state": written, "backup": taken, "retention": retention,
                "rebased_from": rebased_from}


def _attest_commit(connection, home, written, validated_on=None):
    """Vouch for the bytes this writer just committed after validating only its delta.

    The receipt binds the new file's identity to the state derived from the database itself, so
    the next writer or reader skips the full replay only for exactly this file, and the date of the
    last full validation is carried forward, never refreshed by a delta. Best effort:
    without a receipt the next reader or writer simply validates in full.
    """
    try:
        connection.execute("BEGIN")
        try:
            binding = _binding(connection)
            if _head_state(connection) == written:
                _write_receipt(home, binding, written, validated_on)
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
    except (OSError, sqlite3.Error, CatalogError):
        pass


class _CurrentRecords:
    """Indexed lookups into the committed current records, without replaying history.

    A catalog with lookup structures (init creates them; ``migrate-indexes`` adds them to an older
    one) answers referrer and path-claim probes from indexes. One without them answers the same
    questions with substring scans, slower and identically; one with only some of them is refused.
    """

    SQL = ("SELECT v.payload FROM current_records c JOIN versions v "
           "ON v.revision=c.revision AND v.ordinal=c.ordinal WHERE ")
    # The EXISTS form (not a JOIN) lets SQLite drive the probe from the lookup index.
    CURRENT = (" AND EXISTS(SELECT 1 FROM current_records c WHERE c.record_id=v.record_id "
               "AND c.revision=v.revision AND c.ordinal=v.ordinal)")

    def __init__(self, connection):
        self.connection = connection
        self.shape = lookup_shape(connection)
        require(self.shape != "partial", LOOKUP_REFUSAL)

    def get(self, record_id):
        row = self.connection.execute(self.SQL + "c.record_id=?", (record_id,)).fetchone()
        return None if row is None else decode(row[0])

    def _scan(self, needles, keep):
        # Canonical payloads quote every identifier and path exactly as json.dumps does, so a
        # substring probe finds a superset of the matches and the decoded record confirms them.
        needles = [canonical(value) for value in needles]
        found = []
        for start in range(0, len(needles), 200):
            chunk = needles[start:start + 200]
            rows = self.connection.execute(
                self.SQL + " OR ".join("instr(v.payload,?)>0" for _ in chunk), chunk)
            found += [record for record in (decode(row[0]) for row in rows) if keep(record)]
        return found

    def _probe(self, queries, values, keep):
        """Run each ``(sql, ...)`` probe over chunks of ``values``; decoded, confirmed, one per record."""
        found = {}
        for sql in queries:
            for start in range(0, len(values), 200):
                chunk = values[start:start + 200]
                for row in self.connection.execute(sql.format(",".join("?" * len(chunk))), chunk):
                    record = decode(row[0])
                    if keep(record):
                        found[record["id"]] = record
        return list(found.values())

    def referrers(self, ids):
        ids = set(ids)

        def keep(record):
            return bool(ids.intersection(_references(record)))
        if self.shape == "legacy":
            return self._scan(ids, keep)
        queries = [f"SELECT v.payload FROM versions v WHERE {_lookup_expression(field)} IN ({{}})" + self.CURRENT
                   for field in REFERENCE_FIELDS]
        queries.append("SELECT v.payload FROM refs r JOIN versions v ON v.revision=r.revision AND v.ordinal=r.ordinal "
                       "WHERE r.ref IN ({})" + self.CURRENT)
        return self._probe(queries, sorted(ids), keep)

    def claimants(self, paths):
        paths = set(paths)

        def keep(record):
            return record["kind"] == "artifact" and record["data"]["path"] in paths
        if self.shape == "legacy":
            return self._scan(paths, keep)
        return self._probe([f"SELECT v.payload FROM versions v WHERE {_lookup_expression('path')} IN ({{}})"
                            + self.CURRENT], sorted(paths), keep)


class _CommittedStates:
    """Revision-level facts the writer needs, read with SQL instead of a full replay.

    A state's record count is the number of distinct records first written at or before its
    revision, which is what ``inspect`` accumulates while replaying.
    """

    def __init__(self, connection, head):
        self.connection, self.head = connection, head
        self._added = None

    def prior(self, request_id):
        row = self.connection.execute("SELECT * FROM revisions WHERE request_id=?", (request_id,)).fetchone()
        return None if row is None else dict(row)

    def _first_writes(self):
        if self._added is None:
            self._added = dict(self.connection.execute(
                "SELECT first,count(*) FROM (SELECT min(revision) AS first FROM versions GROUP BY record_id) "
                "GROUP BY first"))
        return self._added

    def state(self, revision):
        row = self.connection.execute("SELECT revision_hash FROM revisions WHERE revision=?", (revision,)).fetchone()
        later = sum(count for number, count in self._first_writes().items() if number > revision)
        return make_state(self.head, revision, row[0], self.head["records"] - later)

    def revision_of(self, token):
        """The revision whose committed state carries ``token`` (0 for the empty catalog).

        Searches newest first, so the cost follows how far behind the pin is.
        """
        hashes = dict(self.connection.execute("SELECT revision,revision_hash FROM revisions"))
        records = self.head["records"]
        for number in range(self.head["revision"], 0, -1):
            if make_state(self.head, number, hashes[number], records)["state_token"] == token:
                return number
            records -= self._first_writes().get(number, 0)
        return 0 if token == make_state(self.head, 0, "", 0)["state_token"] else None

    def written_after(self, revision, ids):
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            if self.connection.execute(
                    f"SELECT 1 FROM versions WHERE revision>? AND record_id IN ({','.join('?' * len(chunk))}) LIMIT 1",
                    (revision, *chunk)).fetchone():
                return True
        return False


def rebase_index(root):
    """Map every committed state token to its revision and each record to its last writer.

    Lets a caller check offline, without a write, whether ``submit(..., rebase=True)``
    would accept a request pinned at an older state. The history is fully inspected first;
    the maps then come from indexed queries instead of materialized history.
    """
    home, db, _ = locations(root)
    with closing(connect(db)) as connection:
        connection.execute("BEGIN")
        state = inspect(connection, detail=False)[0]
        committed = _CommittedStates(connection, state)
        records = state["records"]
        tokens = {}
        for number, revision_hash in reversed(list(connection.execute(
                "SELECT revision,revision_hash FROM revisions ORDER BY revision"))):
            tokens[make_state(state, number, revision_hash, records)["state_token"]] = number
            records -= committed._first_writes().get(number, 0)
        last_writer = dict(connection.execute("SELECT record_id,revision FROM current_records"))
    tokens[make_state(state, 0, "", 0)["state_token"]] = 0
    return tokens, last_writer


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
        if expected_state is not None and expected_state != state["state_token"]:
            raise StaleState("Stale query state")
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


def summarize_records(root, *, expected_state=None, limit=25):
    """Aggregate counts over the current catalog from one validated snapshot; no record bodies."""
    require(type(limit) is int and limit > 0, "limit must be a positive integer")
    home, db, _ = locations(root)
    require(home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700, "Catalog home missing or not private")
    with closing(connect(db)) as connection:
        connection.execute("BEGIN")
        state = validated_state(connection, home)
        require(expected_state is None or expected_state == state["state_token"], "Stale query state")
        current_view(connection)

        def counts(field, kind):
            return {str(row[0]): row[1] for row in connection.execute(
                f"SELECT json_extract(payload,'$.data.{field}') AS k,count(*) FROM catalog_current "
                "WHERE kind=? GROUP BY k ORDER BY count(*) DESC,k", (kind,))}

        per_source = connection.execute(
            "SELECT s.value AS source,count(DISTINCT a.id) AS artifacts FROM catalog_current r "
            "JOIN json_each(r.payload,'$.data.source_ids') s "
            "JOIN catalog_current a ON a.kind='artifact' AND "
            "json_extract(a.payload,'$.data.receipt_id')=r.id "
            "WHERE r.kind='receipt' GROUP BY s.value ORDER BY artifacts DESC,source").fetchall()
        return {
            "authority": AUTHORITY, "state": state,
            "total": connection.execute("SELECT count(*) FROM catalog_current").fetchone()[0],
            "by_kind": {row[0]: row[1] for row in connection.execute(
                "SELECT kind,count(*) FROM catalog_current GROUP BY kind ORDER BY kind")},
            "artifacts": {key: counts(key, "artifact")
                          for key in ("coverage", "role", "audience", "presence")},
            "sources_by_category": counts("category", "source"),
            "artifacts_by_source": {row["source"]: row["artifacts"] for row in per_source[:limit]},
            "sources_with_artifacts": len(per_source),
            "artifacts_by_source_truncated": len(per_source) > limit,
        }


def current_view(connection):
    connection.execute("""CREATE TEMP VIEW catalog_current AS
        SELECT c.record_id AS id,json_extract(v.payload,'$.kind') AS kind,v.payload
        FROM current_records c JOIN versions v ON v.revision=c.revision AND v.ordinal=c.ordinal""")


def read_records_by_receipt(root, receipt_ids, *, limit=25, expected_state=None):
    """Read each receipt's scoped records from one validated snapshot.

    Each value equals ``read_records(root, receipt=id, limit=limit, expected_state=...)``, but the
    catalog is validated once and every scope is resolved with set-based joins, not per receipt.
    """
    require(type(limit) is int and limit > 0, "limit must be a positive integer")
    for receipt_id in receipt_ids:
        identifier(receipt_id, "receipt")
    home, db, _ = locations(root)
    require(home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700, "Catalog home missing or not private")
    with closing(connect(db)) as connection:
        connection.execute("BEGIN")
        state = validated_state(connection, home)
        if expected_state is not None and expected_state != state["state_token"]:
            raise StaleState("Stale query state")
        current_view(connection)
        connection.execute("CREATE TEMP TABLE current_rows AS SELECT * FROM catalog_current")
        connection.execute("CREATE UNIQUE INDEX temp.current_rows_id ON current_rows(id)")
        for name, field in (("receipt", "receipt_id"), ("from", "from_id"), ("to", "to_id")):
            connection.execute(f"CREATE INDEX temp.current_rows_{name} ON current_rows"
                               f"(json_extract(payload,'$.data.{field}'))")
        connection.execute("CREATE TEMP TABLE wanted(receipt TEXT PRIMARY KEY)")
        connection.executemany("INSERT OR IGNORE INTO wanted VALUES (?)", [(r,) for r in receipt_ids])
        # Same scope as read_records(receipt=...): the receipt, its sources, its artifacts and the
        # relationships touching those artifacts or sources.
        connection.execute("""CREATE TEMP TABLE members AS
            SELECT w.receipt, r.id FROM wanted w JOIN current_rows r ON r.id=w.receipt AND r.kind='receipt'
            UNION SELECT w.receipt, j.value FROM wanted w JOIN current_rows r
                ON r.id=w.receipt AND r.kind='receipt', json_each(r.payload,'$.data.source_ids') j
            UNION SELECT w.receipt, a.id FROM wanted w JOIN current_rows a
                ON a.kind='artifact' AND json_extract(a.payload,'$.data.receipt_id')=w.receipt""")
        connection.execute("""CREATE TEMP TABLE scope AS
            SELECT receipt, id FROM members
            UNION SELECT m.receipt, r.id FROM members m JOIN current_rows e
                ON e.id=m.id AND e.kind IN ('artifact','source')
                JOIN current_rows r ON r.kind='relationship'
                AND json_extract(r.payload,'$.data.from_id')=e.id
            UNION SELECT m.receipt, r.id FROM members m JOIN current_rows e
                ON e.id=m.id AND e.kind IN ('artifact','source')
                JOIN current_rows r ON r.kind='relationship'
                AND json_extract(r.payload,'$.data.to_id')=e.id""")
        ids = {receipt_id: [] for receipt_id in receipt_ids}
        for row in connection.execute("SELECT receipt,id FROM scope ORDER BY receipt,id"):
            ids[row["receipt"]].append(row["id"])
        result = {}
        for receipt_id, members in ids.items():
            page = members[:limit]
            result[receipt_id] = {
                "authority": AUTHORITY, "state": state,
                "records": [current_record(connection, record_id) for record_id in page],
                "count": len(page), "total_matching": len(members),
                "next_after": page[-1] if len(members) > limit else None}
        return result


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


def verify_backup(path, replay=True):
    """Verify a snapshot against its receipt and fully inspect it.

    ``replay=False`` skips only the inspection, for the snapshot the daily path has just written
    from a fully inspected image and bound by hash; restore, ``verify-backup``, retention of older
    snapshots and every other caller keep the default.
    """
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
    if replay:
        with closing(connect(path)) as connection:
            connection.execute("BEGIN")
            state = inspect(connection, detail=False)[0]
        require(state == record["state"], "Snapshot state differs from receipt")
        require(file_hash(path) == record["sha256"], "Snapshot changed while verifying")
    return record["state"]


def restore(root, snapshot, expected_state=None):
    source = private_file(snapshot)
    source_state = verify_backup(source)
    home, db, backups = locations(root, allow_missing=True)
    require(source != db, "Restore source cannot be the live database")
    with writer_lock(home):
        safety = None
        if db.exists():
            with closing(connect(db)) as connection:
                connection.execute("BEGIN")
                current = inspect(connection, detail=False)[0]
                require(expected_state == current["state_token"], "Restore requires current expected_state")
                require(source_state["store_id"] == current["store_id"], "Cannot replace a different store lineage")
                safety = _snapshot(connection, backups, "pre-restore")
        else:
            require(expected_state is None, "Missing store cannot have replacement expectations")
            instance = policy.read_instance_binding(root)
            if instance is not None and instance.get("catalog_store_id") is not None:
                require(source_state["store_id"] == instance["catalog_store_id"],
                        "Cannot replace a different store lineage")
        partial = home / ("restore-" + uuid.uuid4().hex + ".partial")
        reserve(partial)
        published = False
        try:
            with closing(connect(source)) as original, closing(sqlite3.connect(partial)) as target:
                original.backup(target)
            with closing(connect(partial)) as restored:
                require(inspect(restored, detail=False)[0] == source_state, "Restored state mismatch")
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
                require(inspect(restored, detail=False)[0] == source_state, "Published restore state mismatch")
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
    migrate = commands.add_parser("migrate-indexes", help="Add the lookup indexes to a catalog that lacks them")
    migrate.add_argument("--check", action="store_true", help="Report the lookup shape and verify; write nothing")
    migrate.add_argument("--rebuild", action="store_true", help="Drop and recreate damaged or incomplete lookup structures")
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
        if args.command in {"init", "submit", "backup", "restore", "migrate-indexes"}:
            require(args.max_chars >= 60000, "Mutations require max-chars >=60000")
        if args.command == "init":
            result = initialize(args.root)
        elif args.command == "status":
            result = status(args.root)
        elif args.command == "audit":
            result = status(args.root, audit=True)
        elif args.command == "backup":
            result = backup(args.root)
        elif args.command == "migrate-indexes":
            require(not (args.check and args.rebuild), "--check writes nothing, so it cannot rebuild")
            result = migrate_indexes(args.root, rebuild=args.rebuild, check=args.check)
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
