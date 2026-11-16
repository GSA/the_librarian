#!/usr/bin/env python3
"""Local authoritative review-event persistence; never an intake or operational acceptance store."""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
import uuid

from tools import backups as snapshots, durable
from tools.durable import fsync_directory
from tools.file_identity import KeyedConnection, file_key
from tools.review_events import event_history, review_events as contract
from tools.review_events.event_history import canonical, fingerprint, require


DB_NAME = "events.sqlite3"
BACKUPS = "backups"
NOTICE = ("Agent-maintained review records, not source verification, intake disposition or operational acceptance. "
          "Authorized local use only; source restrictions still apply.")


def file_hash(path):
    return durable.sha256_file(contract.safe_path(path))


def now():
    return datetime.now(timezone.utc).isoformat()


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


def locations(root, *, allow_missing=False):
    root = contract.safe_path(root)
    require(root.is_dir(), "Corpus root must exist")
    try:
        rel_home = contract.policy.validate_instance_roots(
            root, allow_missing=("catalog", "review-event") if allow_missing else ()
        )["review_events_home"]
    except ValueError as error:
        raise contract.ReviewError(str(error)) from error
    home = contract.safe_path(root / rel_home)
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
    before = file_key(path)
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, timeout=0, isolation_level=None, factory=KeyedConnection)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        if write:
            connection.execute("PRAGMA synchronous=FULL")
            require(connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete",
                    "Unsupported journal mode; do not change storage pragmas")
        # The statements above opened the file; the path must still be the same file.
        require(file_key(path) == before, "Review-event file replaced while opening; retry")
        connection.opened = before
    except (contract.ReviewError, OSError, sqlite3.Error):
        connection.close()
        raise
    return connection


def reserve(path):
    durable.reserve(contract.safe_path(path))


def seal(path):
    durable.fsync_file(path)
    fsync_directory(path.parent)


def snapshot(connection, backups, reason, root=None):
    state, _ = event_history.inspect(connection, root)
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
            copied, _ = event_history.inspect(target, root)
        require(copied == state, "Backup does not match committed store")
        seal(temporary)
        digest = file_hash(temporary)
        record = {"backup_version": 1, "created_at": snapshots.utcnow().isoformat(), "reason": reason,
                  "database": final.name,
                  "sha256": digest, "bytes": temporary.stat().st_size, "state": state}
        os.replace(temporary, final)
        fsync_directory(backups)
        durable.write_new_private(receipt, (canonical(record) + "\n").encode("utf-8"))
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
                connection.executescript(event_history.schema_sql())
                metadata = {"store_id": str(uuid.uuid4()), "created_at": now(),
                            "contract_hash": fingerprint(event_history.store_schema(root)),
                            "authority": event_history.AUTHORITY}
                connection.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
                connection.row_factory = sqlite3.Row
                state, _ = event_history.inspect(connection, root)
                initial = snapshot(connection, backups, "init", root)
            seal(temporary)
            os.replace(temporary, db)
            fsync_directory(home)
            return {"command": "init", **state, "backup": initial}
        finally:
            if temporary.exists():
                temporary.unlink()


def prepare_submission(value, root):
    schema = event_history.store_schema(root)
    contract.schema_validate(value, schema, root=root)
    require(all(e["provenance"]["origin"] == "agent_review" for e in value["events"]),
            "Only native review submissions are supported")
    return value["events"]


def append(root, value, actor, request_id, expected_revision, expected_state):
    event_history.validate_actor(actor)
    event_history.validate_request(request_id)
    require(type(expected_revision) is int and expected_revision >= 0, "Expected revision must be nonnegative")
    incoming = prepare_submission(value, root)
    require(all(e["reviewer"] == actor for e in incoming), "Native reviewer must match the submitting agent")
    input_hash = fingerprint(incoming)
    home, db, backups = locations(root)
    with writer_lock(home), closing(connect(db, write=True)) as connection:
        state, existing, _, history = event_history.view(connection, root, reuse=True, materialize=False)
        prior = connection.execute("SELECT * FROM batches WHERE request_id=?", (request_id,)).fetchone()
        if prior is not None:
            require(prior["actor"] == actor and prior["input_hash"] == input_hash,
                    "Request ID reused with a different actor or payload")
            return {"command": "append", "idempotent": True, **state, "committed_revision": prior["revision"],
                    "backup": None, "retention": None}
        require(state["revision"] == expected_revision and state["state_token"] == expected_state,
                "Stale revision or state token; re-read state before writing")
        # With the memoized history only the incoming events are checked against it; without one
        # (an unbindable file) the whole history is validated, as before.
        known = (lambda event_id: history.event(event_id, state["events"]) is not None) if history else (
            lambda event_id, ids={e["event_id"] for e in existing}: event_id in ids)
        ids = set()
        for event in incoming:
            require(event["event_id"] not in ids and not known(event["event_id"]),
                    "Event ID already exists; use original request ID for retries")
            ids.add(event["event_id"])
        if history:
            event_history.validate_delta(history, incoming, root, state["events"])
        else:
            event_history.validate_history(existing + incoming, root)
        taken, retention = snapshots.daily_snapshot(
            backups, store_id=state["store_id"], take=lambda: snapshot(connection, backups, "daily", root),
            verify=lambda path: verified_snapshot(path, root), errors=(contract.ReviewError,),
            days=contract.policy.backup_retention_days(root))
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
                       "origin_key": None, "payload": canonical(event),
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
        except (OSError, sqlite3.Error, contract.ReviewError) as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if committed:
                raise contract.ReviewError(
                    f"Write committed at revision {revision}, but post-commit durability/backup failed. "
                    "Do not resubmit as a new request; retry the same request or run backup."
                ) from error
            raise
        state = event_history.view(connection, root, reuse=True, materialize=False)[0]
        return {"command": "append", "idempotent": False, **state, "backup": taken, "retention": retention}


def backup(root):
    home, db, backups = locations(root)
    with writer_lock(home), closing(connect(db)) as connection:
        return {"command": "backup", **snapshot(connection, backups, "manual", root)}


def backup_inventory(root):
    return snapshots.inventory(locations(root)[2])


def prune(root, *, apply=False):
    """Operator prune: newest verified snapshot per UTC day inside the window; dry run by default."""
    home, db, backups = locations(root)
    with writer_lock(home), closing(connect(db)) as connection:
        store_id = event_history.inspect(connection, root)[0]["store_id"]
        # Hold the writer lock so prune cannot overlap a write's retention or a restore.
        return snapshots.prune(backups, store_id=store_id, verify=lambda path: verified_snapshot(path, root),
                               errors=(contract.ReviewError,), days=contract.policy.backup_retention_days(root),
                               apply=apply, thin=True)


def verified_snapshot(path, root=None):
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
        state, _ = event_history.inspect(connection, root)
    require(state == record["state"], "Snapshot state differs from receipt")
    return state


def restore(root, source, expected_state=None):
    source = private_file(source)
    source_state = verified_snapshot(source, root)
    home, db, backups = locations(root, allow_missing=True)
    require(source != db, "Restore source cannot be the live store")
    with writer_lock(home):
        safety = None
        if db.exists():
            with closing(connect(db)) as connection:
                state, _ = event_history.inspect(connection, root)
                require(expected_state == state["state_token"],
                        "Replacing a store requires its current --expected-state")
                require(source_state["store_id"] == state["store_id"], "Cannot replace a different store lineage")
                safety = snapshot(connection, backups, "pre-restore", root)
        else:
            require(expected_state is None,
                    "Absent store must not have replacement expectations")
            instance = contract.policy.read_instance_binding(root)
            if instance is not None and instance.get("review_store_id") is not None:
                require(source_state["store_id"] == instance["review_store_id"],
                        "Cannot replace a different store lineage")
        temporary = home / ("restore-" + uuid.uuid4().hex + ".partial")
        reserve(temporary)
        try:
            with closing(connect(source)) as original, closing(sqlite3.connect(temporary)) as target:
                original.backup(target)
            with closing(connect(temporary)) as restored:
                require(event_history.inspect(restored, root)[0] == source_state,
                        "Restored state differs from snapshot")
                require(verified_snapshot(source, root) == source_state, "Snapshot changed during restore")
                recovery = snapshot(restored, backups, "restored", root)
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


def status(root, event_id=None, audit=False):
    """Validated state; ``audit`` always replays the whole history and forgets what was remembered."""
    _, db, _ = locations(root)
    with closing(connect(db)) as connection:
        connection.execute("BEGIN")
        if audit:
            event_history.forget(str(db))
        state, events, successors, history = event_history.view(connection, root, reuse=not audit,
                                                                materialize=False)
        if event_id is None:
            return {"command": "status", **state}
        if history is not None:
            found = history.event(event_id, state["events"])
            successor = event_history.successor(history, event_id, state["events"])
        else:
            found = next((e for e in events if e["event_id"] == event_id), None)
            successor = successors.get(event_id)
        require(found is not None, "Unknown event ID")
        return {"command": "get", **state, "event": found, "superseded_by": successor}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", type=Path, default=contract.ROOT)
    parser.add_argument("--max-chars", type=int, default=60000)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "audit", "backup", "schema"):
        sub.add_parser(name, allow_abbrev=False)
    get = sub.add_parser("get", allow_abbrev=False)
    get.add_argument("event_id")
    command = sub.add_parser("append", allow_abbrev=False)
    command.add_argument("input", type=Path)
    command.add_argument("--actor", required=True)
    command.add_argument("--request-id", required=True)
    command.add_argument("--expected-revision", required=True, type=int)
    command.add_argument("--expected-state", required=True)
    command = sub.add_parser("restore", allow_abbrev=False)
    command.add_argument("snapshot", type=Path)
    command.add_argument("--expected-state")
    command = sub.add_parser("verify-backup", allow_abbrev=False)
    command.add_argument("snapshot", type=Path)
    args = parser.parse_args(argv)
    try:
        require(1 <= args.max_chars <= 1000000, "Output budget must be 1-1000000 characters")
        # Mutation receipts must never be hidden by a user-selected tiny output budget.
        if args.command in ("init", "append", "backup", "restore"):
            require(args.max_chars >= 60000, "Mutations require a receipt budget of at least 60000")
        if args.command == "schema":
            result = event_history.store_schema()
        elif args.command == "init":
            result = initialize(args.root)
        elif args.command in ("status", "audit"):
            result = status(args.root, audit=args.command == "audit")
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
