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
import threading
import uuid

from tools import backups as snapshots
from tools.file_identity import file_identity
from tools.review_events import review_events as contract


VERSION = 1
APPLICATION_ID = 0x4C524556
SUPERSEDED_APPLICATION_ID = 0x46524556
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


def store_schema(root=None):
    schema, _ = contract.read_json(contract.SCHEMA)
    audience = contract.policy.for_root(root)[0]["audience"]
    schema["properties"]["audience"] = {"const": audience}
    return schema


def bundle(events, root=None):
    audience = contract.policy.for_root(root)[0]["audience"]
    return {"schema_version": VERSION, "authority": AUTHORITY,
            "audience": audience, "events": events}


def validate_history(events, root=None):
    if events:
        schema = store_schema(root)
        schema["properties"]["events"]["maxItems"] = max(256, len(events))
        contract.validate(bundle(events, root), root if root is not None else contract.ROOT, schema=schema, stored=True)


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


class _Connection(sqlite3.Connection):
    """Remembers the (device, inode) it opened, so a memoized validation is never bound to a
    different file that was moved into the path afterwards."""
    opened = None


# Seconds a connection waits for another process's commit or reader before SQLite reports BUSY.
# It was 0, so any momentary overlap (a progress reader's short SHARED hold, or a writer's commit
# under a reader) failed as ``sqlite3.OperationalError`` and, in intake, a retryable
# ``sqlite-operational`` stage failure (Kata xeb8). Same bound as the catalog's.
BUSY_TIMEOUT_SECONDS = 10.0


def _file_key(path):
    info = os.stat(path)
    return (info.st_dev, info.st_ino)


def connect(path, write=False):
    private_file(path)
    before = _file_key(path)
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None,
                                 factory=_Connection)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        if write:
            connection.execute("PRAGMA synchronous=FULL")
            require(connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete",
                    "Unsupported journal mode; do not change storage pragmas")
        # The statements above opened the file; the path must still be the same file.
        require(_file_key(path) == before, "Review-event file replaced while opening; retry")
        connection.opened = before
    except (contract.ReviewError, OSError, sqlite3.Error):
        connection.close()
        raise
    return connection


def reserve(path):
    descriptor = os.open(contract.safe_path(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)


def seal(path):
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    fsync_directory(path.parent)


class History:
    """The validated, append-only review history behind a memoized store (Kata m0tg).

    Shared read-only between callers. Positions are 1-based store sequences, so a reader holding
    ``n`` events looks an event up with ``upto=n`` and never sees one appended after it read.
    """

    def __init__(self, events=()):
        self.events, self.by_id, self.by_locator = [], {}, {}
        self.successors, self.origins = {}, set()
        for event in events:
            self.add(event)

    def add(self, event):
        self.events.append(event)
        sequence = len(self.events)
        self.by_id[event["event_id"]] = sequence
        self.by_locator.setdefault(event["source"]["locator"], []).append(sequence)
        for target in event["supersedes"]:
            self.successors[target] = event["event_id"]
        key = origin_key(event)
        if key is not None:
            self.origins.add(key)

    def event(self, event_id, upto=None):
        sequence = self.by_id.get(event_id)
        if sequence is None or upto is not None and sequence > upto:
            return None
        return self.events[sequence - 1]


# Validated results kept in memory, one per database file (Kata r71k, m0tg). The trust model is that
# of the catalog's read receipt (docs/design/current-state.md, "What a catalog write validates")
# without a file: a result is reused only for the
# identical file (device, inode, size, mtime, ctime and SQLite change counter), the same contract
# and the same UTC day, after the chain heads re-derived from the file still equal the remembered
# state; it is stored only when the identity is the same before and after the pass; it is never
# written to disk, and a failed or partial validation is never stored. Callers get copies.
#
# When only SQLite's own commit moved the identity (same file, larger, a new change counter) and the
# remembered chain heads are still in the file, only the rows past them are verified: their hash
# chain from the remembered head, batch chain, payload hashes, contract and correction links, and
# that nothing else changed in the correction index. Rows before the remembered head are not read
# again until the next full check: an audit, a backup, a publication (``full=True``), the first
# check of a new UTC day, a different contract, a swapped file, or any identity change SQLite did not
# make (an out-of-band write leaves the change counter alone). Any failure of the delta falls back
# to the full pass, which refuses with its exact message.
_MEMO = {}
_MEMO_LOCK = threading.RLock()
_MEMO_LIMIT = 8


def clear_memo():
    """Forget every remembered validation (tests, and any caller that wants a full pass next)."""
    with _MEMO_LOCK:
        _MEMO.clear()


def forget(path):
    with _MEMO_LOCK:
        _MEMO.pop(path, None)


def _today():
    return snapshots.utcnow().strftime("%Y-%m-%d")


def _heads(connection):
    """Chain heads and counts straight from the file: two index probes and the corrections count."""
    event = connection.execute("SELECT sequence,entry_hash FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
    batch = connection.execute("SELECT revision,batch_hash FROM batches ORDER BY revision DESC LIMIT 1").fetchone()
    return (event[0] if event else 0, event[1] if event else "", batch[0] if batch else 0,
            batch[1] if batch else "", connection.execute("SELECT count(*) FROM corrections").fetchone()[0])


def _state_heads(state):
    return (state["events"], state["head_hash"], state["revision"], state["batch_head_hash"],
            state["events"] - state["active_events"])


def _binding(connection):
    """``(path, identity)`` the memo may be keyed by, or None when this connection cannot be bound."""
    path = connection.execute("PRAGMA database_list").fetchone()["file"]
    if not path:
        return None
    try:
        identity = file_identity(path, connection)
    except OSError:
        return None
    opened = getattr(connection, "opened", None)
    if identity is None or opened is None or opened != (identity[0], identity[1]):
        return None
    return path, identity


def _grew(old, new):
    """True when ``new`` is what SQLite's own commits do to ``old``: same file, no smaller, and a
    change counter that moved (a write that bypassed SQLite leaves the counter where it was)."""
    return old[:2] == new[:2] and new[2] >= old[2] and old[5] != new[5]


def _served(state, history, materialize=True):
    """Fresh containers for exactly the events ``state`` describes, even if the history has grown.

    Without ``materialize`` only ``state`` and the shared ``history`` are returned (events and
    successors are None): a caller that needs the state or indexed lookups pays nothing per event."""
    if not materialize:
        return dict(state), None, None, history
    count = state["events"]
    corrections = count - state["active_events"]
    successors = history.successors
    successors = (dict(successors) if len(successors) == corrections
                  else {t: s for t, s in successors.items() if history.by_id[s] <= count})
    return dict(state), history.events[:count], successors, history


def view(connection, root=None, *, reuse=False, full=False, materialize=True, release=False):
    """``(state, events, successors, history)`` of the validated store.

    ``reuse`` serves an identical file from memory and verifies only a delta after SQLite's own
    commits (see above); ``full`` skips both and replays the whole history, then remembers it.
    ``history`` is the shared read-only index when the result was memoized and None otherwise.
    ``materialize=False`` leaves ``events`` and ``successors`` None when ``history`` is returned, so
    state-only and indexed-lookup callers do no work proportional to the history.
    ``release`` lets a full pass end the caller's read transaction before it validates (see ``_inspect``).
    """
    if not reuse:
        state, events = _inspect(connection, root, release)
        return state, events, _successors(events), None
    own = not connection.in_transaction
    if own:
        connection.execute("BEGIN")
    try:
        # Take the read snapshot before looking at the file; no commit can land inside it.
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        binding = _binding(connection)
        if binding is None:
            state, events = _inspect(connection, root, release)
            return state, events, _successors(events), None
        path, identity = binding
        key = (identity, fingerprint(store_schema(root)), _today())
        with _MEMO_LOCK:
            entry = None if full else _MEMO.get(path)
            if entry is not None and entry[0] == key:
                _, state, history = entry
                if (connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
                        and connection.execute("PRAGMA user_version").fetchone()[0] == VERSION
                        and _heads(connection) == _state_heads(state)):
                    return _served(state, history, materialize)
                forget(path)
            elif entry is not None and entry[0][1:] == key[1:] and _grew(entry[0][0], identity):
                try:
                    state, added = _extend(connection, root, entry[1], entry[2])
                except (contract.ReviewError, sqlite3.Error, ValueError, KeyError, TypeError):
                    state = None
                if state is not None and _binding(connection) == binding:
                    history = entry[2]
                    for event in added:
                        history.add(event)
                    _MEMO[path] = (key, state, history)
                    return _served(state, history, materialize)
                _MEMO.pop(path, None)
        state, events = _inspect(connection, root, release)
        history = History(events)
        if _binding(connection) == binding:
            with _MEMO_LOCK:
                if path not in _MEMO and len(_MEMO) >= _MEMO_LIMIT:
                    _MEMO.clear()
                _MEMO[path] = (key, dict(state), history)
        return _served(state, history, materialize) if _binding(connection) == binding else (
            state, events, _successors(events), None)
    finally:
        if own and connection.in_transaction:
            connection.execute("COMMIT")


def _successors(events):
    return {target: event["event_id"] for event in events for target in event["supersedes"]}


def inspect(connection, root=None, *, reuse=False, full=False):
    """Validate the whole history; with ``reuse`` an identical, unchanged file is served from memory.

    ``reuse`` applies only to connections opened by ``connect``; any other connection, a journal
    beside the file or a build without ``sqlite_dbpage`` validates in full and stores nothing.
    The returned state and event list are fresh containers; the event dictionaries are shared with
    the memo and are read-only by contract (no caller changes a validated event).
    """
    state, events, _, _ = view(connection, root, reuse=reuse, full=full)
    return state, events


def _header(connection, root, deep):
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    require(application_id != SUPERSEDED_APPLICATION_ID,
            "Superseded FREV review-event identity; explicit migration or re-initialization is required")
    require(application_id == APPLICATION_ID, "Not a review-event store")
    require(connection.execute("PRAGMA user_version").fetchone()[0] == VERSION, "Unsupported store version")
    if deep:
        require([r[0] for r in connection.execute("PRAGMA integrity_check")] == ["ok"],
                "SQLite integrity check failed")
        require(not list(connection.execute("PRAGMA foreign_key_check")), "Foreign-key integrity check failed")
    metadata = dict(connection.execute("SELECT key,value FROM metadata"))
    base_keys = {"store_id", "created_at", "contract_hash", "authority"}
    require(set(metadata) == base_keys, "Invalid store metadata")
    require(metadata["authority"] == AUTHORITY and
            metadata["contract_hash"] == fingerprint(store_schema(root)),
            "Stored contract differs from installed contract; explicit migration required")
    require(str(uuid.UUID(metadata["store_id"])) == metadata["store_id"], "Invalid store identity")
    contract.timestamp(metadata["created_at"])
    for table in ("metadata", "batches", "events", "corrections"):
        for action in ("update", "delete"):
            name = f"immutable_{table}_{action}"
            row = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
            require(row is not None and row[0] == trigger_sql(table, action), "Append-only trigger missing or changed")
    return metadata


def _chain(rows, previous, first):
    """Decode ``rows`` (consecutive from sequence ``first``) and check their hash chain from ``previous``."""
    by_revision, events = {}, []
    for sequence, row in enumerate(rows, first):
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
    return events, by_revision, previous


def _batches(batches, by_revision, previous_batch, first):
    """Check ``batches`` (consecutive from revision ``first``) against the rows that cite them."""
    for revision, row in enumerate(batches, first):
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
    return previous_batch


def _inspect(connection, root=None, release=False):
    """Validate the whole history. With ``release`` the read transaction ends once every row is in
    memory, so the O(history) checks run holding no SQLite lock: this store's connections wait for
    no one (``timeout=0``), so a reader holding SHARED through the replay made a writer's commit and
    every new reader fail at once (Kata xeb8). The state is still one snapshot: all rows were read
    inside it, and callers memoize it only if the file identity is unchanged afterwards."""
    metadata = _header(connection, root, deep=True)
    batches = [dict(r) for r in connection.execute("SELECT * FROM batches ORDER BY revision")]
    rows = [dict(r) for r in connection.execute("SELECT * FROM events ORDER BY sequence")]
    stored_corrections = {tuple(r) for r in connection.execute("SELECT predecessor,successor FROM corrections")}
    if release and connection.in_transaction:
        connection.execute("COMMIT")
    events, by_revision, previous = _chain(rows, "", 1)
    previous_batch = _batches(batches, by_revision, "", 1)
    require(all(1 <= r["revision"] <= len(batches) for r in rows), "Event references invalid batch")
    validate_history(events, root)
    expected_corrections = {(target, event["event_id"]) for event in events for target in event["supersedes"]}
    actual_corrections = stored_corrections
    require(actual_corrections == expected_corrections, "Correction index differs from preserved history")
    state = {"store_id": metadata["store_id"], "revision": len(batches), "events": len(events),
             "active_events": len(events) - len(actual_corrections), "head_hash": previous,
             "batch_head_hash": previous_batch, "authority": AUTHORITY}
    state["contract_hash"] = metadata["contract_hash"]
    state["state_token"] = fingerprint(state)
    return state, events


def validate_delta(history, events, root=None, upto=None):
    """Validate ``events`` as the next ones after the first ``upto`` of the validated ``history``."""
    if events:
        schema = store_schema(root)
        schema["properties"]["events"]["maxItems"] = max(256, len(events))
        contract.validate(bundle(events, root), root if root is not None else contract.ROOT, schema=schema,
                          stored=True, known=lambda event_id: history.event(event_id, upto),
                          taken=lambda target: _successor(history, target, upto))


def _successor(history, target, upto):
    successor = history.successors.get(target)
    return successor if successor is not None and history.event(successor, upto) is not None else None


def _extend(connection, root, state, history):
    """``(state, events)`` after verifying only what the file holds past the remembered ``state``."""
    metadata = _header(connection, root, deep=False)
    count, head, revision, batch_head, corrections = _state_heads(state)
    require(metadata["store_id"] == state["store_id"], "Store identity changed")
    last = connection.execute("SELECT entry_hash FROM events WHERE sequence=?", (count,)).fetchone() if count else None
    require((last[0] if last else "") == head, "Remembered chain head is not in the file")
    last = connection.execute("SELECT batch_hash FROM batches WHERE revision=?", (revision,)).fetchone() if revision else None
    require((last[0] if last else "") == batch_head, "Remembered batch head is not in the file")
    rows = [dict(r) for r in connection.execute("SELECT * FROM events WHERE sequence>? ORDER BY sequence", (count,))]
    batches = [dict(r) for r in connection.execute("SELECT * FROM batches WHERE revision>? ORDER BY revision", (revision,))]
    events, by_revision, previous = _chain(rows, head, count + 1)
    previous_batch = _batches(batches, by_revision, batch_head, revision + 1)
    require(set(by_revision) <= {b["revision"] for b in batches}
            and connection.execute("SELECT count(*) FROM events WHERE revision>?", (revision,)).fetchone()[0] == len(rows),
            "Event references invalid batch")
    validate_delta(history, events, root, count)
    expected = {(target, event["event_id"]) for event in events for target in event["supersedes"]}
    total = connection.execute("SELECT count(*) FROM corrections").fetchone()[0]
    require(total == corrections + len(expected)
            and all(connection.execute("SELECT 1 FROM corrections WHERE predecessor=? AND successor=?", pair).fetchone()
                    for pair in expected),
            "Correction index differs from preserved history")
    fresh = {"store_id": state["store_id"], "revision": revision + len(batches), "events": count + len(events),
             "active_events": count + len(events) - total, "head_hash": previous,
             "batch_head_hash": previous_batch, "authority": AUTHORITY, "contract_hash": state["contract_hash"]}
    fresh["state_token"] = fingerprint({k: v for k, v in fresh.items() if k != "state_token"})
    return fresh, events


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


def snapshot(connection, backups, reason, root=None):
    state, _ = inspect(connection, root)
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
            copied, _ = inspect(target, root)
        require(copied == state, "Backup does not match committed store")
        seal(temporary)
        digest = file_hash(temporary)
        record = {"backup_version": 1, "created_at": snapshots.utcnow().isoformat(), "reason": reason,
                  "database": final.name,
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
                            "contract_hash": fingerprint(store_schema(root)), "authority": AUTHORITY}
                connection.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
                connection.row_factory = sqlite3.Row
                state, _ = inspect(connection, root)
                initial = snapshot(connection, backups, "init", root)
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
    schema = store_schema(root)
    contract.schema_validate(value, schema, root=root)
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
        state, existing, _, history = view(connection, root, reuse=True, materialize=False)
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
        origins = history.origins if history else {origin_key(e) for e in existing if origin_key(e) is not None}
        origins = set(origins)
        ids = set()
        for event in incoming:
            require(event["event_id"] not in ids and not known(event["event_id"]),
                    "Event ID already exists; use original request ID for retries")
            ids.add(event["event_id"])
            key = origin_key(event)
            require(key is None or key not in origins, "Historical review already imported")
            if key is not None:
                origins.add(key)
        if history:
            validate_delta(history, incoming, root, state["events"])
        else:
            validate_history(existing + incoming, root)
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
        except (OSError, sqlite3.Error, contract.ReviewError) as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if committed:
                raise contract.ReviewError(
                    f"Write committed at revision {revision}, but post-commit durability/backup failed. "
                    "Do not resubmit as a new request; retry the same request or run backup."
                ) from error
            raise
        return {"command": "append", "idempotent": False, **view(connection, root, reuse=True, materialize=False)[0],
                "backup": taken, "retention": retention}


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
        store_id = inspect(connection, root)[0]["store_id"]
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
        state, _ = inspect(connection, root)
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
                state, _ = inspect(connection, root)
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
                require(inspect(restored, root)[0] == source_state,
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
            forget(str(db))
        state, events, successors, history = view(connection, root, reuse=not audit,
                                                  materialize=False)
        if event_id is None:
            return {"command": "status", **state}
        if history is not None:
            found = history.event(event_id, state["events"])
            successor = _successor(history, event_id, state["events"])
        else:
            found = next((e for e in events if e["event_id"] == event_id), None)
            successor = successors.get(event_id)
        require(found is not None, "Unknown event ID")
        return {"command": "get", **state, "event": found, "superseded_by": successor}


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
