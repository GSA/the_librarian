"""Catalog snapshots: publish a verified copy of the database, verify one, prune and list them."""

from contextlib import closing
import hashlib
import os
import sqlite3
import uuid

from tools import backups as snapshots, durable, policy
from tools.catalog.contracts import CatalogError, canonical, exact, hash_value, require, text
from tools.catalog.storage import (
    VERSION, connect, decode, file_hash, fsync_directory, inspect, locations, private_directory, private_file,
    reserve, safe_path, seal, timestamp, writer_lock, _head_state,
)


def _daily_snapshot(root, connection, backups, state, inspected=False):
    """Verified snapshot before the write, only when the UTC day has none; then bounded retention.

    The snapshot is the serialized image of the transaction that is fully inspected here (or was
    just inspected by the caller, ``inspected``), so the history is replayed once for the day; the
    published bytes are then bound to that image by hash instead of being replayed again.

    That is a head check, not a replay (Kata 91h5), so retention does not count this snapshot as
    verified (``replayed=False``): it replays snapshots only when one is old enough to delete and
    deletes nothing unless a replayed one remains in the window (Kata 6jwh).
    """
    return snapshots.daily_snapshot(
        backups, store_id=state["store_id"],
        take=lambda: _snapshot_in_transaction(connection, backups, "daily", image=True, inspected=inspected),
        verify=verify_backup, verify_fresh=lambda path: verify_backup(path, replay=False),
        errors=(CatalogError,), days=policy.backup_retention_days(root), replayed=False)


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
        durable.write_new_private(safe_path(receipt), (canonical(record) + "\n").encode("utf-8"))
        fsync_directory(backups)
        return {"snapshot": str(final), "receipt": str(receipt), "sha256": record["sha256"], "state": state}
    finally:
        if partial.exists():
            partial.unlink()


def backup(root):
    home, db, backups = locations(root)
    private_file(db)
    with writer_lock(home), closing(connect(db)) as connection:
        connection.execute("BEGIN")
        return {"command": "backup", **_snapshot(connection, backups, "manual")}

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
