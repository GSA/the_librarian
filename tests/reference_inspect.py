"""The pre-wjf9 ``catalog_store.inspect``, verbatim, as the reference for differential tests.

The identity-bound receipt (docs/design/current-state.md, "What a catalog write validates") made inspect cheaper without weakening it; tests compare the production function
with this one on valid and tampered histories, so a change that accepts or refuses differently
fails. Do not optimize this copy.
"""
import sqlite3
import uuid

from tools.catalog.catalog_store import (
    APPLICATION_ID, AUTHORITY, SUPERSEDED_APPLICATION_ID, VERSION, decode, make_state, timestamp, trigger_sql)
from tools.catalog.contracts import (
    CatalogError, GraphValidator, canonical, exact, fingerprint, require, text, validate_record)


def reference_inspect(connection):
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
    graph = GraphValidator()  # same refusals as validate_graph(current), without re-walking history
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
            validate_record(record)  # the original memoized this by payload text; the reference never skips it
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
        # Each record was validated above; only validate_records' list checks remain.
        require(bool(records), "records must be a nonempty list")
        require(len({record["id"] for record in records}) == len(records), "Duplicate IDs in submission")
        graph.apply(records)
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
