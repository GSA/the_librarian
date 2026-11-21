"""What a valid stored review-event history is, and the in-process memo of validated results."""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import threading
import uuid

from tools.file_identity import file_identity, file_key
from tools.review_events import review_events as contract


VERSION = 1
APPLICATION_ID = 0x4C524556
SUPERSEDED_APPLICATION_ID = 0x46524556
AUTHORITY = "review-events-only"
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


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


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


def validate_actor(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", value) is not None,
            "Actor must be a nonempty stable agent identifier (no secrets)")


def validate_request(value):
    require(isinstance(value, str) and re.fullmatch(r"req-[a-z0-9-]{1,100}", value) is not None,
            "request_id must start req- and contain lowercase letters, numbers or hyphens")


class History:
    """The validated, append-only review history behind a memoized store (Kata m0tg).

    Shared read-only between callers. Positions are 1-based store sequences, so a reader holding
    ``n`` events looks an event up with ``upto=n`` and never sees one appended after it read.
    """

    def __init__(self, events=()):
        self.events, self.by_id, self.by_locator = [], {}, {}
        self.successors = {}
        for event in events:
            self.add(event)

    def add(self, event):
        self.events.append(event)
        sequence = len(self.events)
        self.by_id[event["event_id"]] = sequence
        self.by_locator.setdefault(event["source"]["locator"], []).append(sequence)
        for target in event["supersedes"]:
            self.successors[target] = event["event_id"]

    def event(self, event_id, upto=None):
        sequence = self.by_id.get(event_id)
        if sequence is None or upto is not None and sequence > upto:
            return None
        return self.events[sequence - 1]

    def successor(self, target, upto=None):
        """The ID of the event that corrects ``target`` among the first ``upto`` events, or None."""
        successor = self.successors.get(target)
        return successor if successor is not None and self.event(successor, upto) is not None else None

    def at(self, locator, upto=None):
        """Events whose source locator is ``locator`` among the first ``upto`` events, in store order."""
        return [self.events[n - 1] for n in self.by_locator.get(locator, ())
                if upto is None or n <= upto]

    def close(self):
        pass


_ABANDONED = []  # connections inherited across a fork; kept referenced so the child never closes them


class StoredHistory:
    """The validated history of a store this process has not replayed: lookups go to the file.

    A new process that trusts a persisted read-validation receipt (or a remembered state extended by
    the rows past it) has no events in memory. ``event`` and ``successor`` are indexed queries on the
    store's own unique keys, bounded by the caller's ``upto`` so an event appended after the validated
    state is never seen; ``events``, ``successors``, ``by_id`` and ``by_locator`` load the rows once,
    on first use, only for the callers that really want every event (a query or an export). The
    connection is read-only, opened on the very file that was validated, holds no transaction between
    calls (so it never blocks a writer's commit) and is reopened in a forked child.
    """

    def __init__(self, path, opened):
        self.path, self._opened = path, opened
        self._lock = threading.RLock()
        self._connection, self._pid = None, None
        self._full, self._loaded_all = History(), False
        self._db()

    def _db(self):
        with self._lock:
            if file_key(self.path) != self._opened:
                raise contract.ReviewError("Review-event file replaced since it was validated; read it again")
            if self._connection is not None and self._pid == os.getpid():
                return self._connection
            if self._connection is not None:
                _ABANDONED.append(self._connection)
            connection = sqlite3.connect(Path(self.path).as_uri() + "?mode=ro", uri=True, timeout=10.0,
                                         isolation_level=None, check_same_thread=False)
            if file_key(self.path) != self._opened:
                connection.close()
                raise contract.ReviewError("Review-event file replaced since it was validated; read it again")
            connection.execute("PRAGMA trusted_schema=OFF")
            self._connection, self._pid = connection, os.getpid()
            return connection

    def close(self):
        with self._lock:
            if self._connection is not None and self._pid == os.getpid():
                self._connection.close()
            self._connection = None

    def add(self, event):
        """Called when a delta was verified past the remembered head. Before the rows are loaded there
        is nothing to keep (they are read from the file when needed); after, the event joins them,
        unless the load already read it."""
        with self._lock:
            if self._loaded_all and event["event_id"] not in self._full.by_id:
                self._full.add(event)

    def event(self, event_id, upto=None):
        with self._lock:
            rows = self._db().execute("SELECT sequence,payload FROM events WHERE event_id=?", (event_id,)).fetchall()
        row = rows[0] if rows else None
        if row is None or upto is not None and row[0] > upto:
            return None
        return json.loads(row[1], object_pairs_hook=contract.pairs)

    def successor(self, target, upto=None):
        with self._lock:
            rows = self._db().execute(
                "SELECT c.successor FROM corrections c JOIN events e ON e.event_id=c.successor "
                "WHERE c.predecessor=? AND (? IS NULL OR e.sequence<=?)", (target, upto, upto)).fetchall()
        return rows[0][0] if rows else None

    def at(self, locator, upto=None):
        # The events table has no index on the locator: the first call loads every row once, then it is an index.
        return self._loaded().at(locator, upto)

    def __del__(self):
        try:
            self.close()
        except Exception:  # interpreter shutdown
            pass

    def _loaded(self):
        """Every row, loaded once; afterwards only ``add`` grows it, so an iteration in progress over
        one of its indexes is never resized by another property access."""
        with self._lock:
            if not self._loaded_all:
                rows = self._db().execute("SELECT sequence,payload FROM events ORDER BY sequence").fetchall()
                for offset, (sequence, payload) in enumerate(rows, 1):
                    require(sequence == offset, "Broken event sequence")
                    self._full.add(json.loads(payload, object_pairs_hook=contract.pairs))
                self._loaded_all = True
            return self._full

    events = property(lambda self: self._loaded().events)
    successors = property(lambda self: self._loaded().successors)
    by_id = property(lambda self: self._loaded().by_id)
    by_locator = property(lambda self: self._loaded().by_locator)


# Validated results, one per database file (Kata r71k, m0tg, 91h5). The trust model is that of the
# catalog's read receipt (docs/design/current-state.md, "What a catalog write validates"): a result is
# reused only for the identical file (device, inode, size, mtime, ctime and SQLite change counter) and
# the same contract, validator code and runtime, after the chain heads re-derived from the file still
# equal the remembered state. It is stored only when the identity is the same before and after the
# pass; a failed or partial validation is never stored. It has no expiry: a change the identity cannot
# see is found by an audit (``event_store audit``, ``tools.store_audit``), not by the next day's read.
#
# Two places remember a result. This process's memory (``_MEMO``) holds the events themselves. The
# read-validation receipt (``<home>/.local/read-validation.json``, checksummed, rewritten after every
# full pass and delta) lets a new process, which has no memory, do the same without replaying anything:
# it trusts the receipt exactly as it trusts the memo, and answers lookups from the file
# (``StoredHistory``). A receipt that is missing, malformed, from other code or a different file is a
# miss, never a refusal: the full replay decides.
#
# When only SQLite's own commit moved the identity (same file, larger, a new change counter) and the
# remembered chain heads are still in the file, only the rows past them are verified: their hash
# chain from the remembered head, batch chain, payload hashes, contract and correction links, and
# that nothing else changed in the correction index. Rows before the remembered head are not read
# again until the next full check: an audit, a backup, a publication (``full=True``), a different
# contract, a swapped file, or any identity change SQLite did not make (an out-of-band write leaves
# the change counter alone). Any failure of the delta falls back to the full pass, which refuses
# with its exact message.
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


RECEIPT = "read-validation.json"
RECEIPT_LIMIT = 4096
VALIDATOR_FILES = tuple(Path(__file__).with_name(name) for name in
                        ("event_history.py", "review_events.py", "review-event-v1.schema.json"))
_VALIDATOR = []


def _validator_sha256():
    """Hash of the code and schema that decide what a receipt vouches for (computed once per process)."""
    if not _VALIDATOR:
        digest = hashlib.sha256()
        for path in VALIDATOR_FILES:
            data = path.read_bytes()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        _VALIDATOR.append(digest.hexdigest())
    return _VALIDATOR[0]


def _runtime():
    versions = []
    for name in ("jsonschema", "referencing"):
        try:
            versions.append(importlib.metadata.version(name))
        except importlib.metadata.PackageNotFoundError:
            versions.append(None)
    return [sys.version, sqlite3.sqlite_version, *versions]


def _receipt_binding(identity, schema_fingerprint):
    return {"version": 1, "identity": list(identity), "contract": schema_fingerprint,
            "validator_sha256": _validator_sha256(), "runtime": _runtime()}


def _receipt_path(path):
    return Path(path).parent / ".local" / RECEIPT


def _private_directory(directory):
    info = os.lstat(directory)
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def _load_receipt(path):
    """``(binding, state)`` of a well-formed receipt beside ``path``, else None; never raises."""
    receipt = _receipt_path(path)
    try:
        if not _private_directory(receipt.parent):
            return None
        info = os.lstat(receipt)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > RECEIPT_LIMIT:
            return None
        descriptor = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            saved = json.loads(stream.read(RECEIPT_LIMIT + 1))
        if (type(saved) is not dict or set(saved) != {"binding", "state", "checksum"}
                or saved["checksum"] != fingerprint({"binding": saved["binding"], "state": saved["state"]})):
            return None
        binding, state = saved["binding"], saved["state"]
        keys = {"store_id", "revision", "events", "active_events", "head_hash", "batch_head_hash",
                "authority", "contract_hash", "state_token"}
        if (type(binding) is not dict or type(state) is not dict or set(state) != keys
                or type(binding.get("identity")) is not list or len(binding["identity"]) != 6
                or state["state_token"] != fingerprint({k: v for k, v in state.items() if k != "state_token"})
                or not all(type(state[k]) is int for k in ("revision", "events", "active_events"))):
            return None
        return binding, state
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def _write_receipt(path, binding, state):
    """Replace the receipt beside ``path`` atomically; best effort, a cache that is never needed to be correct."""
    receipt = _receipt_path(path)
    partial = receipt.with_name(uuid.uuid4().hex + ".partial")
    try:
        receipt.parent.mkdir(mode=0o700, exist_ok=True)
        if not _private_directory(receipt.parent):
            return
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write((canonical({"binding": binding, "state": state,
                                     "checksum": fingerprint({"binding": binding, "state": state})}) + "\n").encode())
        os.replace(partial, receipt)
    except (OSError, ValueError, TypeError):
        pass
    finally:
        try:
            os.unlink(partial)
        except OSError:
            pass


def _remember(path, key, state, history):
    with _MEMO_LOCK:
        if path not in _MEMO and len(_MEMO) >= _MEMO_LIMIT:
            _MEMO.clear()
        _MEMO[path] = (key, state, history)


def _header_sound(connection, root):
    """Whether the refusal came from the stored history, not from the store's identity or contract.

    A wrong identity, version or contract already makes every receipt a miss (``_recall`` runs the
    same header check), so such a refusal leaves the instance exactly as it found it.
    """
    try:
        _header(connection, root, deep=False)
        return True
    except (contract.ReviewError, sqlite3.Error, ValueError, KeyError, TypeError):
        return False


def _drop_receipt(path):
    try:
        os.unlink(_receipt_path(path))
    except OSError:
        pass


def _recall(connection, root, path, identity, schema_fingerprint):
    """``(key, state, StoredHistory)`` a receipt beside ``path`` vouches for, or None.

    The receipt must have been written for this code, contract and runtime; whether it covers this
    file, or an earlier state of it that SQLite's commits only extended, is for the caller to decide.
    """
    saved = _load_receipt(path)
    if saved is None:
        return None
    binding, state = saved
    if (binding != _receipt_binding(binding["identity"], schema_fingerprint)
            or not (binding["identity"] == list(identity) or _grew(binding["identity"], list(identity)))):
        return None
    try:
        metadata = _header(connection, root, deep=False)
        if metadata["store_id"] != state["store_id"] or metadata["contract_hash"] != state["contract_hash"]:
            return None
        history = StoredHistory(path, connection.opened)
    except (contract.ReviewError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError):
        return None
    return (list(binding["identity"]), schema_fingerprint), state, history


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
        schema_fingerprint = fingerprint(store_schema(root))
        key = (identity, schema_fingerprint)
        with _MEMO_LOCK:
            entry = None if full else _MEMO.get(path)
            if entry is None and not full:
                entry = _recall(connection, root, path, identity, schema_fingerprint)
            if entry is not None and entry[0] == key:
                _, state, history = entry
                if (connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
                        and connection.execute("PRAGMA user_version").fetchone()[0] == VERSION
                        and _heads(connection) == _state_heads(state)):
                    _remember(path, key, state, history)
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
                    _remember(path, key, state, history)
                    _write_receipt(path, _receipt_binding(identity, schema_fingerprint), state)
                    return _served(state, history, materialize)
                _MEMO.pop(path, None)
        try:
            state, events = _inspect(connection, root, release)
        except (contract.ReviewError, sqlite3.Error, ValueError, KeyError, TypeError, RecursionError):
            # A full check that refuses (an audit, publication) means no later process may trust a receipt
            # for this file. An ordinary read that refuses changed nothing: its identity already misses.
            forget(path)
            if full and _header_sound(connection, root):
                _drop_receipt(path)
            raise
        history = History(events)
        if _binding(connection) == binding:
            _remember(path, key, dict(state), history)
            _write_receipt(path, _receipt_binding(identity, schema_fingerprint), state)
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
        require(row["event_id"] == event["event_id"] and row["origin_key"] is None,
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
    memory, so the O(history) checks run holding no SQLite lock: a reader holding SHARED through the
    replay made a writer's commit fail (Kata xeb8). The state is still one snapshot: all rows were
    read inside it, and callers memoize it only if the file identity is unchanged afterwards."""
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
                          taken=lambda target: successor(history, target, upto))


def successor(history, target, upto):
    return history.successor(target, upto)


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
