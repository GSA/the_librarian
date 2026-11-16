"""The catalog database file: location and permissions, the writer lock, guarded connections,
schema and lookup structures, and the full inspection that verifies everything stored in it."""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
import uuid

from tools import durable, policy
from tools.file_identity import KeyedConnection, file_key
from tools.catalog.contracts import (
    CatalogError, GraphValidator, WriterBusy, canonical, exact, fingerprint, require, text, validate_record,
)

AUTHORITY = "catalog-metadata-only"
DB_NAME = "catalog.sqlite3"
VERSION = 1
APPLICATION_ID = 0x4C434154
SUPERSEDED_APPLICATION_ID = 0x46434154


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
    durable.fsync_directory(safe_path(path))


def seal(path):
    durable.fsync_file(safe_path(path))
    fsync_directory(path.parent)


def reserve(path):
    durable.reserve(safe_path(path))


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
    before = file_key(path)
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None,
                                 factory=KeyedConnection)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        require(connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete",
                "Unsupported SQLite journal mode")
        if write:
            connection.execute("PRAGMA synchronous=FULL")
        # The pragmas above opened the file; the path must still be the same file.
        require(file_key(path) == before, "Catalog file replaced while opening; retry")
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
    return durable.sha256_file(private_file(path))

def _head_state(connection):
    """State derived from the metadata, newest revision and current-record count alone."""
    metadata = dict(connection.execute("SELECT key,value FROM metadata"))
    latest = connection.execute(
        "SELECT revision,revision_hash FROM revisions ORDER BY revision DESC LIMIT 1").fetchone()
    count = connection.execute("SELECT count(*) FROM current_records").fetchone()[0]
    return make_state(metadata, latest["revision"] if latest else 0,
                      latest["revision_hash"] if latest else "", count)

