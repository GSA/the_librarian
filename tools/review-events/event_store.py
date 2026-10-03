#!/usr/bin/env python3
"""Local authoritative review-event persistence; never an intake or operational acceptance store."""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid

import review_events as contract


VERSION = 1
APPLICATION_ID = 0x46524556
AUTHORITY = "review-events-only"
HOME = ".local/review-events"
DB_NAME = "events.sqlite3"
BACKUPS = "backups"
NOTICE = ("Agent-maintained review records, not source verification, intake disposition or operational acceptance. "
          "Authorized local use only; source restrictions still apply.")
SQL = """
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE batches(
 revision INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, actor TEXT NOT NULL,
 committed_at TEXT NOT NULL, input_hash TEXT NOT NULL, event_count INTEGER NOT NULL CHECK(event_count>0),
 previous_hash TEXT NOT NULL, batch_hash TEXT NOT NULL);
CREATE TABLE events(
 sequence INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
 revision INTEGER NOT NULL REFERENCES batches(revision),
 origin_key TEXT UNIQUE, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
 previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL);
CREATE TABLE corrections(
 predecessor TEXT PRIMARY KEY REFERENCES events(event_id),
 successor TEXT NOT NULL UNIQUE REFERENCES events(event_id));
CREATE INDEX events_revision ON events(revision);
"""


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def fingerprint(value):
    return sha(canonical(value).encode())


def file_hash(path):
    digest = hashlib.sha256()
    with contract.safe_path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def require(condition, message):
    contract.require(condition, message)


def store_schema():
    schema, _ = contract.read_json(contract.SCHEMA)
    schema["properties"]["audience"] = {"const": contract.policy.SETTINGS["audience"]}
    return schema


def bundle(events):
    return {"schema_version": VERSION, "authority": AUTHORITY,
            "audience": contract.policy.SETTINGS["audience"], "events": events}


def validate_history(events):
    if events:
        schema = store_schema()
        schema["properties"]["events"]["maxItems"] = max(256, len(events))
        contract.validate(bundle(events), schema=schema, stored=True)


def origin_key(event):
    return None


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_directory(path):
    path = contract.safe_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(path.is_dir() and path.stat().st_mode & 0o077 == 0,
            "Store and backup directories must be private (0700); refusing permission changes")
    return path


def private_file(path):
    path = contract.safe_path(path)
    require(path.is_file() and path.stat().st_mode & 0o077 == 0,
            "Database or snapshot must be a private regular file (0600)")
    return path


def locations(root):
    root = contract.safe_path(root)
    require(root.is_dir(), "Corpus root must exist")
    home = contract.safe_path(root / HOME)
    return home, home / DB_NAME, home / BACKUPS


@contextmanager
def writer_lock(home):
    try:
        import fcntl
    except ImportError as error:
        raise contract.ReviewError("Store writes require a POSIX host with flock support") from error
    private_directory(home)
    path = contract.safe_path(home / "writer.lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        require(os.fstat(descriptor).st_mode & 0o077 == 0, "Writer lock must be private")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise contract.ReviewError("Another store writer or restore is active; retry later") from error
        yield
    finally:
        os.close(descriptor)


def connect(path, write=False):
    private_file(path)
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, timeout=0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    if write:
        connection.execute("PRAGMA synchronous=FULL")
        require(connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete",
                "Unsupported journal mode; do not change storage pragmas")
    return connection


def reserve(path):
    descriptor = os.open(contract.safe_path(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)


def seal(path):
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    fsync_directory(path.parent)


def inspect(connection):
    require(connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID,
            "Not a review-event store")
    require(connection.execute("PRAGMA user_version").fetchone()[0] == VERSION, "Unsupported store version")
    require([r[0] for r in connection.execute("PRAGMA integrity_check")] == ["ok"],
            "SQLite integrity check failed")
    require(not list(connection.execute("PRAGMA foreign_key_check")), "Foreign-key integrity check failed")
    metadata = dict(connection.execute("SELECT key,value FROM metadata"))
    base_keys = {"store_id", "created_at", "contract_hash", "authority"}
    require(set(metadata) == base_keys, "Invalid store metadata")
    require(metadata["authority"] == AUTHORITY and
            metadata["contract_hash"] == fingerprint(store_schema()),
            "Stored contract differs from installed contract; explicit migration required")
    require(str(uuid.UUID(metadata["store_id"])) == metadata["store_id"], "Invalid store identity")
    contract.timestamp(metadata["created_at"])
    for table in ("metadata", "batches", "events", "corrections"):
        for action in ("update", "delete"):
            name = f"immutable_{table}_{action}"
            row = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
            require(row is not None and row[0] == trigger_sql(table, action), "Append-only trigger missing or changed")
    batches = [dict(r) for r in connection.execute("SELECT * FROM batches ORDER BY revision")]
    rows = [dict(r) for r in connection.execute("SELECT * FROM events ORDER BY sequence")]
    by_revision = {}
    events = []
    previous = ""
    for sequence, row in enumerate(rows, 1):
        require(row["sequence"] == sequence and row["previous_hash"] == previous, "Broken event sequence/hash chain")
        event = json.loads(row["payload"], object_pairs_hook=contract.pairs)
        require(row["payload"] == canonical(event) and row["payload_hash"] == fingerprint(event),
                "Event payload hash mismatch")
        require(row["event_id"] == event["event_id"] and row["origin_key"] == origin_key(event),
                "Stored identity/provenance key mismatch")
        expected = fingerprint({k: v for k, v in row.items() if k != "entry_hash"})
        require(expected == row["entry_hash"], "Event chain integrity mismatch")
        previous = row["entry_hash"]
        events.append(event)
        by_revision.setdefault(row["revision"], []).append(row)
    previous_batch = ""
    for revision, row in enumerate(batches, 1):
        require(row["revision"] == revision and row["previous_hash"] == previous_batch, "Broken batch chain")
        members = by_revision.get(revision, [])
        require(len(members) == row["event_count"] and members, "Batch event count mismatch")
        require(row["input_hash"] == fingerprint([json.loads(e["payload"]) for e in members]), "Batch input mismatch")
        require(row["batch_hash"] == fingerprint({k: v for k, v in row.items() if k != "batch_hash"}),
                "Batch hash mismatch")
        contract.timestamp(row["committed_at"])
        validate_actor(row["actor"])
        validate_request(row["request_id"])
        require(all(e["revision"] == revision for e in members), "Batch membership mismatch")
        for member in members:
            event = json.loads(member["payload"])
            if event["provenance"]["origin"] == "agent_review":
                require(event["reviewer"] == row["actor"], "Native reviewer differs from batch attribution")
        previous_batch = row["batch_hash"]
    require(all(1 <= r["revision"] <= len(batches) for r in rows), "Event references invalid batch")
    validate_history(events)
    expected_corrections = {(target, event["event_id"]) for event in events for target in event["supersedes"]}
    actual_corrections = {tuple(r) for r in connection.execute("SELECT predecessor,successor FROM corrections")}
    require(actual_corrections == expected_corrections, "Correction index differs from preserved history")
    state = {"store_id": metadata["store_id"], "revision": len(batches), "events": len(events),
             "active_events": len(events) - len(actual_corrections), "head_hash": previous,
             "batch_head_hash": previous_batch, "authority": AUTHORITY}
    state["contract_hash"] = metadata["contract_hash"]
    state["state_token"] = fingerprint(state)
    return state, events




def trigger_sql(table, action):
    return (f"CREATE TRIGGER immutable_{table}_{action} BEFORE {action.upper()} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'append-only store'); END")


def schema_sql():
    """Complete storage structure without records, identities or filesystem operations."""
    triggers = "\n".join(
        trigger_sql(table, action) + ";"
        for table in ("metadata", "batches", "events", "corrections")
        for action in ("update", "delete")
    )
    return f"PRAGMA application_id={APPLICATION_ID};\nPRAGMA user_version={VERSION};\n{SQL}\n{triggers}\n"


def snapshot(connection, backups, reason):
    state, _ = inspect(connection)
    private_directory(backups)
    name = f"{state['store_id']}-r{state['revision']:08d}-{reason}-{uuid.uuid4().hex}"
    final = backups / (name + ".sqlite3")
    temporary = backups / (name + ".partial")
    receipt = backups / (name + ".json")
    reserve(temporary)
    try:
        with closing(sqlite3.connect(temporary)) as target:
            connection.backup(target)
        with closing(connect(temporary)) as target:
            copied, _ = inspect(target)
        require(copied == state, "Backup does not match committed store")
        seal(temporary)
        digest = file_hash(temporary)
        record = {"backup_version": 1, "created_at": now(), "reason": reason, "database": final.name,
                  "sha256": digest, "bytes": temporary.stat().st_size, "state": state}
        os.replace(temporary, final)
        fsync_directory(backups)
        descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(backups)
        return {"snapshot": str(final), "receipt": str(receipt), "revision": state["revision"]}
    finally:
        if temporary.exists():
            temporary.unlink()


def initialize(root):
    home, db, backups = locations(root)
    with writer_lock(home):
        require(not db.exists(), "Store already exists; init never overwrites history")
        temporary = home / ("init-" + uuid.uuid4().hex + ".partial")
        reserve(temporary)
        try:
            with closing(sqlite3.connect(temporary, isolation_level=None)) as connection:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(schema_sql())
                metadata = {"store_id": str(uuid.uuid4()), "created_at": now(),
                            "contract_hash": fingerprint(store_schema()), "authority": AUTHORITY}
                connection.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
                connection.row_factory = sqlite3.Row
                state, _ = inspect(connection)
                initial = snapshot(connection, backups, "init")
            seal(temporary)
            os.replace(temporary, db)
            fsync_directory(home)
            return {"command": "init", **state, "backup": initial}
        finally:
            if temporary.exists():
                temporary.unlink()


def validate_actor(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", value) is not None,
            "Actor must be a nonempty stable agent identifier (no secrets)")


def validate_request(value):
    require(isinstance(value, str) and re.fullmatch(r"req-[a-z0-9-]{1,100}", value) is not None,
            "request_id must start req- and contain lowercase letters, numbers or hyphens")


def prepare_submission(value, root):
    schema = store_schema()
    contract.schema_validate(value, schema)
    require(all(e["provenance"]["origin"] == "agent_review" for e in value["events"]),
            "Only native review submissions are supported")
    return value["events"]


def append(root, value, actor, request_id, expected_revision, expected_state):
    validate_actor(actor)
    validate_request(request_id)
    require(type(expected_revision) is int and expected_revision >= 0, "Expected revision must be nonnegative")
    incoming = prepare_submission(value, root)
    require(all(e["reviewer"] == actor for e in incoming), "Native reviewer must match the submitting agent")
    input_hash = fingerprint(incoming)
    home, db, backups = locations(root)
    with writer_lock(home), closing(connect(db, write=True)) as connection:
        state, existing = inspect(connection)
        prior = connection.execute("SELECT * FROM batches WHERE request_id=?", (request_id,)).fetchone()
        if prior is not None:
            require(prior["actor"] == actor and prior["input_hash"] == input_hash,
                    "Request ID reused with a different actor or payload")
            return {"command": "append", "idempotent": True, **state, "committed_revision": prior["revision"],
                    "backup": snapshot(connection, backups, "retry")}
        require(state["revision"] == expected_revision and state["state_token"] == expected_state,
                "Stale revision or state token; re-read state before writing")
        ids = {e["event_id"] for e in existing}
        origins = {origin_key(e) for e in existing if origin_key(e) is not None}
        for event in incoming:
            require(event["event_id"] not in ids, "Event ID already exists; use original request ID for retries")
            ids.add(event["event_id"])
            key = origin_key(event)
            require(key is None or key not in origins, "Historical review already imported")
            if key is not None:
                origins.add(key)
        validate_history(existing + incoming)
        pre = snapshot(connection, backups, "pre-write")
        committed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            require(connection.execute("SELECT count(*) FROM batches").fetchone()[0] == state["revision"],
                    "Store changed outside the writer lock")
            revision = state["revision"] + 1
            batch = {"revision": revision, "request_id": request_id, "actor": actor, "committed_at": now(),
                     "input_hash": input_hash, "event_count": len(incoming), "previous_hash": state["batch_head_hash"]}
            batch["batch_hash"] = fingerprint(batch)
            connection.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?,?)", tuple(batch.values()))
            previous = state["head_hash"]
            for sequence, event in enumerate(incoming, state["events"] + 1):
                row = {"sequence": sequence, "event_id": event["event_id"], "revision": revision,
                       "origin_key": origin_key(event), "payload": canonical(event),
                       "payload_hash": fingerprint(event), "previous_hash": previous}
                row["entry_hash"] = fingerprint(row)
                connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", tuple(row.values()))
                previous = row["entry_hash"]
            for event in incoming:
                for target in event["supersedes"]:
                    connection.execute("INSERT INTO corrections VALUES (?,?)", (target, event["event_id"]))
            connection.execute("COMMIT")
            committed = True
            seal(db)
            post = snapshot(connection, backups, "post-write")
        except (OSError, sqlite3.Error, contract.ReviewError) as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if committed:
                raise contract.ReviewError(
                    f"Write committed at revision {revision}, but post-commit durability/backup failed. "
                    "Do not resubmit as a new request; retry the same request or run backup."
                ) from error
            raise
        return {"command": "append", "idempotent": False, **inspect(connection)[0],
                "pre_backup": pre, "backup": post}


def backup(root):
    home, db, backups = locations(root)
    with writer_lock(home), closing(connect(db)) as connection:
        return {"command": "backup", **snapshot(connection, backups, "manual")}


def verified_snapshot(path):
    path = private_file(path)
    require(path.suffix == ".sqlite3", "Snapshot must be a SQLite file")
    receipt_path = private_file(path.with_suffix(".json"))
    record, _ = contract.read_json(receipt_path)
    require(isinstance(record, dict) and set(record) ==
            {"backup_version", "created_at", "reason", "database", "sha256", "bytes", "state"},
            "Invalid backup receipt")
    require(type(record["backup_version"]) is int and record["backup_version"] == 1
            and record["database"] == path.name, "Unsupported or mismatched backup receipt")
    contract.timestamp(record["created_at"])
    require(path.stat().st_size == record["bytes"] and file_hash(path) == record["sha256"],
            "Snapshot bytes differ from backup receipt")
    with closing(connect(path)) as connection:
        state, _ = inspect(connection)
    require(state == record["state"], "Snapshot state differs from receipt")
    return state


def restore(root, source, expected_state=None):
    source = private_file(source)
    source_state = verified_snapshot(source)
    home, db, backups = locations(root)
    require(source != db, "Restore source cannot be the live store")
    with writer_lock(home):
        safety = None
        if db.exists():
            with closing(connect(db)) as connection:
                state, _ = inspect(connection)
                require(expected_state == state["state_token"],
                        "Replacing a store requires its current --expected-state")
                require(source_state["store_id"] == state["store_id"], "Cannot replace a different store lineage")
                safety = snapshot(connection, backups, "pre-restore")
        else:
            require(expected_state is None,
                    "Absent store must not have replacement expectations")
        temporary = home / ("restore-" + uuid.uuid4().hex + ".partial")
        reserve(temporary)
        try:
            with closing(connect(source)) as original, closing(sqlite3.connect(temporary)) as target:
                original.backup(target)
            with closing(connect(temporary)) as restored:
                require(inspect(restored)[0] == source_state,
                        "Restored state differs from snapshot")
                require(verified_snapshot(source) == source_state, "Snapshot changed during restore")
                recovery = snapshot(restored, backups, "restored")
            seal(temporary)
            # DELETE journaling ensures no WAL from an old lineage survives replacement.
            for suffix in ("-wal", "-shm", "-journal"):
                require(not Path(str(db) + suffix).exists(), "SQLite sidecar present; stop and recover explicitly")
            os.replace(temporary, db)
            fsync_directory(home)
            return {"command": "restore", **source_state, "safety_backup": safety, "backup": recovery,
                    "migration_required": "contract_hash" not in source_state}
        finally:
            if temporary.exists():
                temporary.unlink()






def status(root, event_id=None):
    _, db, _ = locations(root)
    with closing(connect(db)) as connection:
        connection.execute("BEGIN")
        state, events = inspect(connection)
        if event_id is None:
            return {"command": "status", **state}
        matches = [e for e in events if e["event_id"] == event_id]
        require(len(matches) == 1, "Unknown event ID")
        successors = {p: e["event_id"] for e in events for p in e["supersedes"]}
        return {"command": "get", **state, "event": matches[0], "superseded_by": successors.get(event_id)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=contract.ROOT)
    parser.add_argument("--max-chars", type=int, default=60000)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "audit", "backup", "schema"):
        sub.add_parser(name)
    get = sub.add_parser("get")
    get.add_argument("event_id")
    for name in ("append",):
        command = sub.add_parser(name)
        command.add_argument("input", type=Path)
        command.add_argument("--actor", required=True)
        command.add_argument("--request-id", required=True)
        command.add_argument("--expected-revision", required=True, type=int)
        command.add_argument("--expected-state", required=True)
    command = sub.add_parser("restore")
    command.add_argument("snapshot", type=Path)
    command.add_argument("--expected-state")
    command = sub.add_parser("verify-backup")
    command.add_argument("snapshot", type=Path)
    args = parser.parse_args(argv)
    try:
        require(1 <= args.max_chars <= 1000000, "Output budget must be 1-1000000 characters")
        # Mutation receipts must never be hidden by a user-selected tiny output budget.
        if args.command in ("init", "append", "backup", "restore"):
            require(args.max_chars >= 60000, "Mutations require a receipt budget of at least 60000")
        if args.command == "schema":
            result = store_schema()
        elif args.command == "init":
            result = initialize(args.root)
        elif args.command in ("status", "audit"):
            result = status(args.root)
            result["command"] = args.command
            if args.command == "audit":
                result["boundary"] = "Integrity/contract audit only; not independent semantic review."
        elif args.command == "get":
            result = status(args.root, args.event_id)
        elif args.command == "backup":
            result = backup(args.root)
        elif args.command == "verify-backup":
            result = {"command": args.command, **verified_snapshot(args.snapshot)}
        elif args.command == "restore":
            result = restore(args.root, args.snapshot, args.expected_state)
        else:
            value, _ = contract.read_json(args.input)
            result = append(args.root, value, args.actor, args.request_id, args.expected_revision, args.expected_state)
        output = canonical({"notice": NOTICE, **result}) + "\n"
        require(len(output) <= args.max_chars, "Complete output exceeds budget; no fields were truncated")
        sys.stdout.write(output)
        return 0
    except (contract.ReviewError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, RecursionError) as error:
        message = str(error) if isinstance(error, contract.ReviewError) else type(error).__name__
        print("event-store: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
