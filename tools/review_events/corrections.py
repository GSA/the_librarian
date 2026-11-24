#!/usr/bin/env python3
"""Append-only claim corrections: correct, dispute, confirm or retract one quoted claim at an indexed
locator, citing a newer document, a meeting transcript or an answered question.

Corrections never edit renditions, reviews or earlier corrections. They live in their own small store
beside the review-event store (``<review_events_home>/corrections``) because a new review-event kind
would change the review-event contract hash every existing store pins, and because a review-event
append changes the review state Docling selections bind, forcing an index rebuild per correction.
Retrieval applies corrections at read time instead (``tools/context_index/corrections.py``).
"""

import argparse
from contextlib import closing
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid

from tools import backups as snapshots, durable
from tools.durable import fsync_directory
from tools.review_events import event_store as store, review_events as contract
from tools.review_events.event_history import (
    canonical, fingerprint, require, validate_actor, validate_request,
)


SCHEMA = 1
APPLICATION_ID = 0x4C52434F
CONTRACT = "claim-correction-v1"
AUTHORITY = "claim-corrections-only"
DB_NAME = "corrections.sqlite3"
DDL = """
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE batches(
 revision INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, actor TEXT NOT NULL,
 committed_at TEXT NOT NULL, input_hash TEXT NOT NULL, record_count INTEGER NOT NULL CHECK(record_count>0),
 previous_hash TEXT NOT NULL, batch_hash TEXT NOT NULL);
CREATE TABLE corrections(
 sequence INTEGER PRIMARY KEY, correction_id TEXT NOT NULL UNIQUE,
 revision INTEGER NOT NULL REFERENCES batches(revision), payload TEXT NOT NULL,
 payload_hash TEXT NOT NULL, previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL);
CREATE INDEX corrections_revision ON corrections(revision);
CREATE TRIGGER immutable_metadata_update BEFORE UPDATE ON metadata BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_metadata_delete BEFORE DELETE ON metadata BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_batches_update BEFORE UPDATE ON batches BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_batches_delete BEFORE DELETE ON batches BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_corrections_update BEFORE UPDATE ON corrections BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_corrections_delete BEFORE DELETE ON corrections BEGIN SELECT RAISE(ABORT,'append-only store'); END;
"""
TRIGGERS = {name: sql for sql, name in re.findall(r"^(CREATE TRIGGER (\w+) .*? END);$", DDL, re.M)}
NOTICE = ("Agent-recorded claim corrections, not source verification or operational acceptance. They mark "
          "indexed text as shaky or confirmed; they never change it. Authorized local use only.")

KINDS = ("correct", "dispute", "confirm", "retract")
# Kinds that open a correction on a claim; confirm and retract only resolve open ones.
OPENING = ("correct", "dispute")
SOURCE_TYPES = ("document", "transcript", "question")
RECORD_FIELDS = ("correction_id", "kind", "target", "statement", "summary", "source", "resolves",
                 "actor", "recorded_at")
TARGET_FIELDS = ("locator", "path", "line_start", "line_end", "source_hash", "receipt_id", "claim",
                 "claim_sha256")
SOURCE_FIELDS = ("type", "receipt_id", "locator", "question", "answer", "answered_by", "answered_on")
DRAFT_FIELDS = ("kind", "locator", "claim", "statement", "summary", "source", "resolves")
LIMITS = {"claim": 1000, "statement": 2000, "summary": 1000, "locator": 1000, "question": 1000,
          "answer": 2000, "answered_by": 200}
MAX_RECORDS = 100
ID = re.compile(r"cor-[a-f0-9]{20}")
RECEIPT = re.compile(r"D0*[1-9][0-9]*")
HASH = re.compile(r"[a-f0-9]{64}")
FILE_SCHEMA = "claim-corrections-v1"


def normalize(text):
    """Whitespace-collapsed text; claims match and hash in this form so reflowed lines still match."""
    return " ".join(text.split())


def claim_hash(claim):
    return hashlib.sha256(normalize(claim).encode("utf-8")).hexdigest()


def _text(value, name, nullable=False):
    if value is None and nullable:
        return
    require(isinstance(value, str) and value.strip() and "\0" not in value and len(value) <= LIMITS[name],
            f"{name} must be nonempty text of at most {LIMITS[name]} characters")


def _exact(value, fields, name):
    require(isinstance(value, dict) and set(value) == set(fields),
            f"{name} must contain exactly: {', '.join(fields)}")


def check_source(source, kind, recorded=None):
    if source is None:
        require(kind == "retract", "A correct, dispute or confirm record must cite its source")
        return
    _exact(source, SOURCE_FIELDS, "source")
    require(source["type"] in SOURCE_TYPES, "source.type must be one of " + ", ".join(SOURCE_TYPES))
    if source["type"] == "question":
        for name in ("question", "answer", "answered_by"):
            _text(source[name], name)
        require(isinstance(source["answered_on"], str)
                and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", source["answered_on"]) is not None,
                "An answered question needs answered_on (YYYY-MM-DD)")
        try:
            answered = date.fromisoformat(source["answered_on"])
        except ValueError as error:
            raise contract.ReviewError("answered_on is not a calendar date") from error
        require(recorded is None or answered <= recorded.date(), "answered_on is after recording")
        require(source["receipt_id"] is None and source["locator"] is None,
                "An answered question cites no receipt or locator")
    else:
        require(isinstance(source["receipt_id"], str) and RECEIPT.fullmatch(source["receipt_id"]) is not None,
                "A document or transcript source needs its receipt_id (D…)")
        _text(source["locator"], "locator", nullable=True)
        require(all(source[name] is None for name in ("question", "answer", "answered_by", "answered_on")),
                "A document or transcript source has no question fields")


def check_record(record):
    """One stored record, on its own; ``replay`` checks it against the history."""
    _exact(record, RECORD_FIELDS, "Correction record")
    require(isinstance(record["correction_id"], str) and ID.fullmatch(record["correction_id"]) is not None,
            "Invalid correction_id")
    kind = record["kind"]
    require(kind in KINDS, "kind must be one of " + ", ".join(KINDS))
    target = record["target"]
    _exact(target, TARGET_FIELDS, "target")
    _text(target["locator"], "locator")
    contract.relative(target["path"])
    require(target["locator"] == target["path"] or target["locator"].startswith(target["path"] + "#"),
            "Target locator is not in its path")
    for name in ("line_start", "line_end"):
        require(target[name] is None or type(target[name]) is int and target[name] >= 0,
                "Target coordinates must be nonnegative integers")
    require((target["line_start"] is None) == (target["line_end"] is None)
            and (target["line_start"] is None or target["line_start"] <= target["line_end"]),
            "Invalid target coordinates")
    require(isinstance(target["source_hash"], str) and HASH.fullmatch(target["source_hash"]) is not None,
            "Target needs the indexed source hash")
    require(target["receipt_id"] is None or isinstance(target["receipt_id"], str)
            and RECEIPT.fullmatch(target["receipt_id"]) is not None, "Invalid target receipt_id")
    _text(target["claim"], "claim")
    require(target["claim_sha256"] == claim_hash(target["claim"]), "claim_sha256 does not hash the claim")
    if kind == "correct":
        _text(record["statement"], "statement")
    else:
        require(record["statement"] is None, "Only a correct record carries a corrected statement")
    _text(record["summary"], "summary")
    validate_actor(record["actor"])
    recorded = contract.timestamp(record["recorded_at"])
    check_source(record["source"], kind, recorded)
    if record["source"] is not None and record["source"]["receipt_id"] is not None:
        require(record["source"]["receipt_id"] != target["receipt_id"],
                "A correction must cite a different document than the one it corrects")
    resolves = record["resolves"]
    require(isinstance(resolves, list) and len(set(resolves)) == len(resolves) <= MAX_RECORDS
            and all(isinstance(item, str) and ID.fullmatch(item) for item in resolves),
            "resolves must list distinct correction IDs")
    require(kind in OPENING or resolves, "A confirm or retract record must resolve an open correction")


def replay(records, resolved_by=None, by_id=None):
    """Validate ``records`` in order after an already validated history; returns the two indexes."""
    by_id = dict(by_id or {})
    resolved_by = dict(resolved_by or {})
    for record in records:
        check_record(record)
        require(record["correction_id"] not in by_id, "Duplicate correction ID")
        for target in record["resolves"]:
            prior = by_id.get(target)
            require(prior is not None, "resolves names an unknown correction: " + target)
            require(prior["kind"] in OPENING, "Only correct or dispute records can be resolved")
            require(target not in resolved_by, "Correction already resolved: " + target)
            require(prior["target"]["locator"] == record["target"]["locator"]
                    and prior["target"]["claim_sha256"] == record["target"]["claim_sha256"],
                    "A resolution must target the same claim as the correction it resolves")
            require(contract.timestamp(record["recorded_at"]) >= contract.timestamp(prior["recorded_at"]),
                    "A resolution cannot predate the correction it resolves")
            resolved_by[target] = record["correction_id"]
        by_id[record["correction_id"]] = record
    return by_id, resolved_by


class Snapshot:
    """One validated history: records in store order, and which correction resolved which."""

    def __init__(self, state, records, resolved_by):
        self.state, self.records, self.resolved_by = state, records, resolved_by
        self.by_id = {record["correction_id"]: record for record in records}

    def status(self, record):
        if record["kind"] not in OPENING:
            return "resolution"
        return "resolved" if record["correction_id"] in self.resolved_by else "open"

    def query(self, *, locator=None, path=None, status="all", limit=25, after=0):
        require(type(limit) is int and 1 <= limit <= 100, "limit must be an integer from 1 to 100")
        require(type(after) is int and after >= 0, "after must be a nonnegative sequence")
        require(status in ("open", "resolved", "all"), "status must be open, resolved or all")
        matches = []
        for sequence, record in enumerate(self.records, 1):
            current = self.status(record)
            if (locator is not None and record["target"]["locator"] != locator
                    or path is not None and record["target"]["path"] != path
                    or status != "all" and current != status):
                continue
            matches.append({"sequence": sequence, "status": current,
                            "resolved_by": self.resolved_by.get(record["correction_id"]), "record": record})
        selected = [match for match in matches if match["sequence"] > after][:limit]
        remaining = sum(match["sequence"] > after for match in matches)
        return {"state": dict(self.state), "records": selected, "count": len(selected),
                "total_matching": len(matches),
                "next_after": selected[-1]["sequence"] if remaining > len(selected) else None}


# ---------------------------------------------------------------------------------------------- storage

def locations(root, *, allow_missing=False):
    review_home = store.locations(root, allow_missing=allow_missing)[0]
    home = contract.safe_path(review_home / "corrections")
    return home, home / DB_NAME, home / "backups"


def schema_sql():
    return f"PRAGMA application_id={APPLICATION_ID};\nPRAGMA user_version={SCHEMA};\n{DDL}"


def contract_hash():
    return fingerprint({"contract": CONTRACT, "ddl": DDL, "kinds": KINDS, "record": RECORD_FIELDS,
                        "target": TARGET_FIELDS, "source": SOURCE_FIELDS, "limits": LIMITS})


def inspect(connection):
    """``(state, records, resolved_by)`` after checking structure, both hash chains and every record."""
    require(connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID,
            "Not a claim-correction store")
    require(connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA, "Unsupported store version")
    require([r[0] for r in connection.execute("PRAGMA integrity_check")] == ["ok"], "SQLite integrity check failed")
    require(not list(connection.execute("PRAGMA foreign_key_check")), "Foreign-key integrity check failed")
    metadata = dict(connection.execute("SELECT key,value FROM metadata"))
    require(set(metadata) == {"store_id", "created_at", "contract_hash", "authority"}, "Invalid store metadata")
    require(metadata["authority"] == AUTHORITY and metadata["contract_hash"] == contract_hash(),
            "Stored contract differs from installed contract; explicit migration required")
    require(str(uuid.UUID(metadata["store_id"])) == metadata["store_id"], "Invalid store identity")
    contract.timestamp(metadata["created_at"])
    for name, sql in TRIGGERS.items():
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        require(row is not None and row[0] == sql, "Append-only trigger missing or changed")
    batches = [dict(r) for r in connection.execute("SELECT * FROM batches ORDER BY revision")]
    rows = [dict(r) for r in connection.execute("SELECT * FROM corrections ORDER BY sequence")]
    previous, records, members = "", [], {}
    for sequence, row in enumerate(rows, 1):
        require(row["sequence"] == sequence and row["previous_hash"] == previous, "Broken correction hash chain")
        record = json.loads(row["payload"], object_pairs_hook=contract.pairs)
        require(row["payload"] == canonical(record) and row["payload_hash"] == fingerprint(record)
                and row["correction_id"] == record.get("correction_id"), "Correction payload hash mismatch")
        require(row["entry_hash"] == fingerprint({k: v for k, v in row.items() if k != "entry_hash"}),
                "Correction chain integrity mismatch")
        previous = row["entry_hash"]
        records.append(record)
        members.setdefault(row["revision"], []).append(record)
    previous_batch = ""
    for revision, batch in enumerate(batches, 1):
        require(batch["revision"] == revision and batch["previous_hash"] == previous_batch, "Broken batch chain")
        require(batch["batch_hash"] == fingerprint({k: v for k, v in batch.items() if k != "batch_hash"}),
                "Batch hash mismatch")
        validate_actor(batch["actor"])
        validate_request(batch["request_id"])
        contract.timestamp(batch["committed_at"])
        recorded = members.get(revision, [])
        require(len(recorded) == batch["record_count"], "Batch record count mismatch")
        require(all(record["actor"] == batch["actor"] for record in recorded),
                "Correction actor differs from batch attribution")
        previous_batch = batch["batch_hash"]
    require(set(members) <= set(range(1, len(batches) + 1)), "Correction references an invalid batch")
    _, resolved_by = replay(records)
    state = {"store_id": metadata["store_id"], "revision": len(batches), "records": len(records),
             "open": sum(r["kind"] in OPENING and r["correction_id"] not in resolved_by for r in records),
             "head_hash": previous, "batch_head_hash": previous_batch, "authority": AUTHORITY,
             "contract_hash": metadata["contract_hash"]}
    state["state_token"] = fingerprint(state)
    return state, records, resolved_by


def initialize(root):
    home, db, backups = locations(root)
    with store.writer_lock(home):
        require(not db.exists(), "Store already exists; init never overwrites history")
        temporary = home / ("init-" + uuid.uuid4().hex + ".partial")
        store.reserve(temporary)
        try:
            with closing(sqlite3.connect(temporary, isolation_level=None)) as connection:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(schema_sql())
                metadata = {"store_id": str(uuid.uuid4()), "created_at": store.now(),
                            "contract_hash": contract_hash(), "authority": AUTHORITY}
                connection.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
                connection.row_factory = sqlite3.Row
                state = inspect(connection)[0]
                initial = snapshot(connection, backups, "init")
            store.seal(temporary)
            os.replace(temporary, db)
            fsync_directory(home)
            return {"command": "init", **state, "backup": initial}
        finally:
            if temporary.exists():
                temporary.unlink()


def read(root):
    """The validated ``Snapshot``, or None when no correction store was initialized."""
    _, db, _ = locations(root)
    if not db.exists():
        return None
    try:
        with closing(store.connect(db)) as connection:
            connection.execute("BEGIN")
            return Snapshot(*inspect(connection))
    except contract.ReviewError:
        raise
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, RecursionError) as error:
        raise contract.ReviewError("Cannot read validated claim-correction store: " + type(error).__name__) from error


def draft(value):
    """A submitted correction in canonical form (every field present, missing ones null/empty)."""
    require(isinstance(value, dict) and set(value) <= set(DRAFT_FIELDS) and "kind" in value,
            "A correction names its kind and only: " + ", ".join(DRAFT_FIELDS))
    result = {name: value.get(name) for name in DRAFT_FIELDS}
    result["resolves"] = list(value.get("resolves") or [])
    source = value.get("source")
    if isinstance(source, dict):
        require(set(source) <= set(SOURCE_FIELDS), "source may contain only: " + ", ".join(SOURCE_FIELDS))
        result["source"] = {name: source.get(name) for name in SOURCE_FIELDS}
    require(result["kind"] in KINDS, "kind must be one of " + ", ".join(KINDS))
    if result["kind"] in OPENING:
        require(result["locator"] is not None and result["claim"] is not None,
                "A correct or dispute record names the locator and quotes the claim")
    else:
        require(result["locator"] is None and result["claim"] is None,
                "A confirm or retract record takes its target from the correction it resolves")
    return result


def corrections_file(value):
    """Drafts from a corrections file that accompanies a newer document or meeting transcript."""
    require(isinstance(value, dict) and set(value) <= {"schema", "source", "corrections"}
            and value.get("schema") == FILE_SCHEMA, f"Corrections file needs schema {FILE_SCHEMA}")
    items = value.get("corrections")
    require(isinstance(items, list) and 1 <= len(items) <= MAX_RECORDS,
            f"Corrections file lists 1 to {MAX_RECORDS} corrections")
    shared = value.get("source")
    return [draft({**item, "source": item.get("source", shared)} if isinstance(item, dict) else item)
            for item in items]


def _registered(root, receipt_id):
    from tools.catalog import catalog_store as catalog
    try:
        catalog.receipt_metadata(root, [receipt_id])
    except catalog.CatalogError as error:
        raise contract.ReviewError(f"Cited receipt {receipt_id} is not in the catalog: {error}") from error


def _locate(root, locator, claim):
    from tools.context_index import corrections as reading
    from tools.context_index.context_index import IndexError_
    try:
        return reading.target(root, locator, claim)
    except (IndexError_, OSError, sqlite3.Error) as error:
        raise contract.ReviewError(str(error)) from error


def _records(root, drafts, actor, ids, known):
    """Stored records for ``drafts``: new claims are located in the fresh index, resolutions copy
    the target of the correction they resolve."""
    built, by_id = [], dict(known)
    recorded_at = store.now()
    for value, correction_id in zip(drafts, ids):
        if value["kind"] in OPENING:
            _text(value["claim"], "claim")
            _text(value["locator"], "locator")
            target = _locate(root, value["locator"], value["claim"])
        else:
            prior = by_id.get(value["resolves"][0]) if value["resolves"] else None
            require(prior is not None, "resolves names an unknown correction")
            target = prior["target"]
        source = value["source"]
        if source is not None and source.get("receipt_id") is not None:
            _registered(root, source["receipt_id"])
        record = {"correction_id": correction_id, "kind": value["kind"], "target": target,
                  "statement": value["statement"], "summary": value["summary"], "source": source,
                  "resolves": value["resolves"], "actor": actor, "recorded_at": recorded_at}
        built.append(record)
        by_id[correction_id] = record
    return built


def append(root, drafts, actor, request_id, expected_state):
    """Record ``drafts`` (see ``draft``) in one transaction; all or none."""
    validate_actor(actor)
    validate_request(request_id)
    require(isinstance(drafts, list) and 1 <= len(drafts) <= MAX_RECORDS,
            f"A request records 1 to {MAX_RECORDS} corrections")
    drafts = [draft(value) for value in drafts]
    require(isinstance(expected_state, str) and HASH.fullmatch(expected_state) is not None,
            "expected_state must be a state token")
    input_hash = fingerprint(drafts)
    home, db, backups = locations(root)
    require(db.exists(), "Claim-correction store is missing; run init first")
    with store.writer_lock(home), closing(store.connect(db, write=True)) as connection:
        state, existing, resolved_by = inspect(connection)
        prior = connection.execute("SELECT * FROM batches WHERE request_id=?", (request_id,)).fetchone()
        if prior is not None:
            require(prior["actor"] == actor and prior["input_hash"] == input_hash,
                    "Request ID reused with a different actor or payload")
            ids = [row[0] for row in connection.execute(
                "SELECT correction_id FROM corrections WHERE revision=? ORDER BY sequence", (prior["revision"],))]
            return {"command": "append", "idempotent": True, **state, "committed_revision": prior["revision"],
                    "correction_ids": ids, "backup": None, "retention": None}
        require(state["state_token"] == expected_state, "Stale state token; re-read status before writing")
        ids = ["cor-" + hashlib.sha256(f"{state['store_id']}:{request_id}:{n}".encode()).hexdigest()[:20]
               for n in range(len(drafts))]
        known = {record["correction_id"]: record for record in existing}
        incoming = _records(root, drafts, actor, ids, known)
        replay(incoming, resolved_by, known)
        taken, retention = snapshots.daily_snapshot(
            backups, store_id=state["store_id"], take=lambda: snapshot(connection, backups, "daily"),
            verify=verified_snapshot, errors=(contract.ReviewError,),
            days=contract.policy.backup_retention_days(root))
        committed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            require(connection.execute("SELECT count(*) FROM batches").fetchone()[0] == state["revision"],
                    "Store changed outside the writer lock")
            revision = state["revision"] + 1
            batch = {"revision": revision, "request_id": request_id, "actor": actor, "committed_at": store.now(),
                     "input_hash": input_hash, "record_count": len(incoming),
                     "previous_hash": state["batch_head_hash"]}
            batch["batch_hash"] = fingerprint(batch)
            connection.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?,?)", tuple(batch.values()))
            previous = state["head_hash"]
            for sequence, record in enumerate(incoming, state["records"] + 1):
                row = {"sequence": sequence, "correction_id": record["correction_id"], "revision": revision,
                       "payload": canonical(record), "payload_hash": fingerprint(record), "previous_hash": previous}
                row["entry_hash"] = fingerprint(row)
                connection.execute("INSERT INTO corrections VALUES (?,?,?,?,?,?,?)", tuple(row.values()))
                previous = row["entry_hash"]
            connection.execute("COMMIT")
            committed = True
            store.seal(db)
        except (OSError, sqlite3.Error, contract.ReviewError) as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if committed:
                raise contract.ReviewError(
                    f"Write committed at revision {revision}, but post-commit durability failed. "
                    "Do not resubmit as a new request; retry the same request or run backup.") from error
            raise
        state = inspect(connection)[0]
        return {"command": "append", "idempotent": False, **state, "correction_ids": ids,
                "backup": taken, "retention": retention}


def snapshot(connection, backups, reason):
    state = inspect(connection)[0]
    store.private_directory(backups)
    name = f"{state['store_id']}-r{state['revision']:08d}-{reason}-{uuid.uuid4().hex}"
    final, temporary, receipt = (backups / (name + suffix) for suffix in (".sqlite3", ".partial", ".json"))
    store.reserve(temporary)
    try:
        with closing(sqlite3.connect(temporary)) as target:
            connection.backup(target)
        with closing(store.connect(temporary)) as target:
            require(inspect(target)[0] == state, "Backup does not match committed store")
        store.seal(temporary)
        record = {"backup_version": 1, "created_at": snapshots.utcnow().isoformat(), "reason": reason,
                  "database": final.name, "sha256": store.file_hash(temporary),
                  "bytes": temporary.stat().st_size, "state": state}
        os.replace(temporary, final)
        fsync_directory(backups)
        durable.write_new_private(receipt, (canonical(record) + "\n").encode("utf-8"))
        fsync_directory(backups)
        return {"snapshot": str(final), "receipt": str(receipt), "revision": state["revision"]}
    finally:
        if temporary.exists():
            temporary.unlink()


def verified_snapshot(path):
    path = store.private_file(path)
    require(path.suffix == ".sqlite3", "Snapshot must be a SQLite file")
    record, _ = contract.read_json(store.private_file(path.with_suffix(".json")))
    require(isinstance(record, dict) and set(record) == snapshots.RECEIPT_FIELDS
            and record["backup_version"] == 1 and record["database"] == path.name, "Invalid backup receipt")
    contract.timestamp(record["created_at"])
    require(path.stat().st_size == record["bytes"] and store.file_hash(path) == record["sha256"],
            "Snapshot bytes differ from backup receipt")
    with closing(store.connect(path)) as connection:
        require(inspect(connection)[0] == record["state"], "Snapshot state differs from receipt")
    return record["state"]


def backup(root):
    home, db, backups = locations(root)
    with store.writer_lock(home), closing(store.connect(db)) as connection:
        return {"command": "backup", **snapshot(connection, backups, "manual")}


def backup_inventory(root):
    return snapshots.inventory(locations(root)[2])


def prune(root, *, apply=False):
    """Operator prune: newest verified snapshot per UTC day inside the window; dry run by default."""
    home, db, backups = locations(root)
    require(db.exists(), "Claim-correction store is missing; run init first")
    with store.writer_lock(home), closing(store.connect(db)) as connection:
        # Hold the writer lock so prune cannot overlap a write's retention or a restore.
        store_id = inspect(connection)[0]["store_id"]
        return snapshots.prune(backups, store_id=store_id, verify=verified_snapshot,
                               errors=(contract.ReviewError,), days=contract.policy.backup_retention_days(root),
                               apply=apply, thin=True)


def restore(root, source, expected_state=None):
    """Replace the store with a verified snapshot of the same lineage (a pre-restore backup is kept)."""
    source = store.private_file(source)
    source_state = verified_snapshot(source)
    home, db, backups = locations(root)
    require(source != db, "Restore source cannot be the live store")
    with store.writer_lock(home):
        safety = None
        if db.exists():
            with closing(store.connect(db)) as connection:
                state = inspect(connection)[0]
                require(expected_state == state["state_token"], "Replacing a store requires its current --expected-state")
                require(source_state["store_id"] == state["store_id"], "Cannot replace a different store lineage")
                safety = snapshot(connection, backups, "pre-restore")
        else:
            require(expected_state is None, "Absent store must not have replacement expectations")
        temporary = home / ("restore-" + uuid.uuid4().hex + ".partial")
        store.reserve(temporary)
        try:
            with closing(store.connect(source)) as original, closing(sqlite3.connect(temporary)) as target:
                original.backup(target)
            with closing(store.connect(temporary)) as restored:
                require(inspect(restored)[0] == source_state, "Restored state differs from snapshot")
            store.seal(temporary)
            for suffix in ("-wal", "-shm", "-journal"):
                require(not Path(str(db) + suffix).exists(), "SQLite sidecar present; stop and recover explicitly")
            os.replace(temporary, db)
            fsync_directory(home)
            return {"command": "restore", **source_state, "safety_backup": safety}
        finally:
            if temporary.exists():
                temporary.unlink()


def status(root):
    snapshot_ = read(root)
    require(snapshot_ is not None, "Claim-correction store is missing; run init first")
    return {"command": "status", **snapshot_.state}


# ---------------------------------------------------------------------------------------------- command line

def _source(args):
    if args.source_type is None:
        require(not any((args.source_receipt, args.source_locator, args.question, args.answer,
                         args.answered_by, args.answered_on)), "Source details need --source-type")
        return None
    return {"type": args.source_type, "receipt_id": args.source_receipt, "locator": args.source_locator,
            "question": args.question, "answer": args.answer, "answered_by": args.answered_by,
            "answered_on": args.answered_on}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=contract.ROOT)
    parser.add_argument("--max-chars", type=int, default=60000)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "backup"):
        sub.add_parser(name, allow_abbrev=False)
    listing = sub.add_parser("list", allow_abbrev=False, help="List recorded corrections")
    listing.add_argument("--locator")
    listing.add_argument("--path")
    listing.add_argument("--status", choices=("open", "resolved", "all"), default="all")
    listing.add_argument("--limit", type=int, default=25)
    listing.add_argument("--after", type=int, default=0)
    writes = []
    record = sub.add_parser("record", allow_abbrev=False, help="Correct or dispute a quoted claim at a locator")
    record.add_argument("--kind", choices=OPENING, required=True)
    record.add_argument("--locator", required=True)
    record.add_argument("--claim", required=True, help="Exact claim text quoted from the indexed section")
    record.add_argument("--statement", help="The corrected statement (correct only)")
    writes.append(record)
    resolve = sub.add_parser("resolve", allow_abbrev=False, help="Confirm or retract open corrections")
    resolve.add_argument("correction_ids", nargs="+")
    resolve.add_argument("--kind", choices=("confirm", "retract"), required=True)
    writes.append(resolve)
    for command in writes:
        command.add_argument("--summary", required=True, help="What is wrong, disputed or settled, and why")
        command.add_argument("--source-type", choices=SOURCE_TYPES)
        command.add_argument("--source-receipt", help="Receipt of the newer document or transcript")
        command.add_argument("--source-locator", help="Where in that document or transcript")
        command.add_argument("--question")
        command.add_argument("--answer")
        command.add_argument("--answered-by")
        command.add_argument("--answered-on", help="YYYY-MM-DD")
    intake = sub.add_parser("intake", allow_abbrev=False,
                            help=f"Record a {FILE_SCHEMA} JSON file in one transaction")
    intake.add_argument("input", type=Path)
    writes.append(intake)
    for command in writes:
        command.add_argument("--actor", required=True)
        command.add_argument("--request-id", required=True)
        command.add_argument("--expected-state", required=True)
    restore_command = sub.add_parser("restore", allow_abbrev=False)
    restore_command.add_argument("snapshot", type=Path)
    restore_command.add_argument("--expected-state")
    verify = sub.add_parser("verify-backup", allow_abbrev=False)
    verify.add_argument("snapshot", type=Path)
    args = parser.parse_args(argv)
    try:
        require(1 <= args.max_chars <= 1000000, "Output budget must be 1-1000000 characters")
        if args.command in ("init", "record", "resolve", "intake", "backup", "restore"):
            require(args.max_chars >= 60000, "Mutations require a receipt budget of at least 60000")
        if args.command == "init":
            result = initialize(args.root)
        elif args.command == "status":
            result = status(args.root)
        elif args.command == "list":
            current = read(args.root)
            require(current is not None, "Claim-correction store is missing; run init first")
            result = {"command": "list", **current.query(locator=args.locator, path=args.path, status=args.status,
                                                         limit=args.limit, after=args.after)}
        elif args.command == "backup":
            result = backup(args.root)
        elif args.command == "verify-backup":
            result = {"command": "verify-backup", **verified_snapshot(args.snapshot)}
        elif args.command == "restore":
            result = restore(args.root, args.snapshot, args.expected_state)
        else:
            if args.command == "intake":
                drafts = corrections_file(contract.read_json(args.input)[0])
            elif args.command == "record":
                drafts = [{"kind": args.kind, "locator": args.locator, "claim": args.claim,
                           "statement": args.statement, "summary": args.summary, "source": _source(args)}]
            else:
                drafts = [{"kind": args.kind, "summary": args.summary, "source": _source(args),
                           "resolves": args.correction_ids}]
            result = append(args.root, drafts, args.actor, args.request_id, args.expected_state)
        output = canonical({"notice": NOTICE, **result}) + "\n"
        require(len(output) <= args.max_chars, "Complete output exceeds budget; no fields were truncated")
        sys.stdout.write(output)
        return 0
    except (contract.ReviewError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, RecursionError) as error:
        message = str(error) if isinstance(error, (contract.ReviewError, ValueError)) else type(error).__name__
        print("corrections: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
