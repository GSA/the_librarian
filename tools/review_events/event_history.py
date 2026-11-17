"""What a valid stored review-event history is, and the in-process memo of validated results."""

import hashlib
import json
import re
import sqlite3
import threading
import uuid

from tools import backups as snapshots
from tools.file_identity import file_identity
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


def view(connection, root=None, *, reuse=False, full=False, materialize=True):
    """``(state, events, successors, history)`` of the validated store.

    ``reuse`` serves an identical file from memory and verifies only a delta after SQLite's own
    commits (see above); ``full`` skips both and replays the whole history, then remembers it.
    ``history`` is the shared read-only index when the result was memoized and None otherwise.
    ``materialize=False`` leaves ``events`` and ``successors`` None when ``history`` is returned, so
    state-only and indexed-lookup callers do no work proportional to the history.
    """
    if not reuse:
        state, events = _inspect(connection, root)
        return state, events, _successors(events), None
    own = not connection.in_transaction
    if own:
        connection.execute("BEGIN")
    try:
        # Take the read snapshot before looking at the file; no commit can land inside it.
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        binding = _binding(connection)
        if binding is None:
            state, events = _inspect(connection, root)
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
        state, events = _inspect(connection, root)
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


def _inspect(connection, root=None):
    metadata = _header(connection, root, deep=True)
    batches = [dict(r) for r in connection.execute("SELECT * FROM batches ORDER BY revision")]
    rows = [dict(r) for r in connection.execute("SELECT * FROM events ORDER BY sequence")]
    events, by_revision, previous = _chain(rows, "", 1)
    previous_batch = _batches(batches, by_revision, "", 1)
    require(all(1 <= r["revision"] <= len(batches) for r in rows), "Event references invalid batch")
    validate_history(events, root)
    expected_corrections = {(target, event["event_id"]) for event in events for target in event["supersedes"]}
    actual_corrections = {tuple(r) for r in connection.execute("SELECT predecessor,successor FROM corrections")}
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
