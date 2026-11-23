#!/usr/bin/env python3
"""Local metadata-only catalog: immutable revisions, explicit provenance, no document bodies."""

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
import unicodedata
import uuid

from tools import backups as snapshots, policy
from tools.file_identity import file_identity
from tools.catalog.backups import _daily_snapshot, _snapshot, _snapshot_in_transaction, verify_backup
from tools.catalog.contracts import (
    AUDIENCES, COVERAGES, KINDS, CatalogError, ConflictingWrite, MissingRecord, StaleState, canonical,
    choice, exact, fingerprint, identifier, now, require, safe_relative, text, validate_graph,
    validate_records, validate_delta, _references,
)
from tools.catalog.storage import (
    AUTHORITY, BUSY_TIMEOUT_SECONDS, LOOKUP_REFUSAL, REFERENCE_FIELDS, VERSION, connect, create_lookup, decode,
    drop_lookup, fsync_directory, inspect, locations, lookup_shape, make_state, private_directory, private_file,
    reserve, safe_path, schema_sql, seal, writer_lock, _head_state, _lookup_expression, _writer_active,
)
# Kept reachable here because callers use this module as the catalog's API.
from tools.catalog.backups import backup, backup_inventory, prune  # noqa: F401
from tools.catalog.contracts import WriterBusy  # noqa: F401

# Every module whose code decides what a read-validation receipt vouches for.
VALIDATOR_FILES = tuple(Path(__file__).with_name(name)
                        for name in ("catalog_store.py", "storage.py", "backups.py", "contracts.py"))


def validator_sha256():
    digest = hashlib.sha256()
    for path in VALIDATOR_FILES:
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


VALIDATOR_SHA256 = validator_sha256()


def initialize(root):
    home, db, backups = locations(root)
    with writer_lock(home):
        require(not db.exists(), "Catalog already exists; init never overwrites")
        for suffix in ("-journal", "-wal", "-shm"):
            require(not safe_path(str(db) + suffix, probe=True).exists(), "SQLite sidecar present; explicit recovery required")
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


def _recover_journal(db):
    """Let SQLite roll back the rollback journal an interrupted writer left; nothing else is touched.

    Called only with the writer lock held, so no live writer can own the journal. Any other sidecar
    (WAL, shared memory) is still refused. The caller verifies the recovered catalog in full.
    """
    for suffix in ("-wal", "-shm"):
        require(not safe_path(str(db) + suffix, probe=True).exists(), "SQLite sidecar present; explicit recovery required")
    journal = safe_path(str(db) + "-journal", probe=True)
    if not journal.exists():
        return False
    require(journal.is_file() and stat.S_IMODE(journal.stat().st_mode) == 0o600,
            "SQLite sidecar present; explicit recovery required")
    with closing(sqlite3.connect(private_file(db).as_uri() + "?mode=rw", uri=True, isolation_level=None,
                                 timeout=BUSY_TIMEOUT_SECONDS)) as connection:
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()  # opening rolls a hot journal back
    require(not journal.exists(), "SQLite sidecar present; explicit recovery required")
    return True


def migrate_indexes(root, *, rebuild=False, check=False, recover=False):
    """Add the lookup structures to a catalog that lacks them (Kata 1h7h); idempotent and crash-safe.

    One writer transaction under the writer lock: fully inspect the catalog, publish a verified
    pre-migration snapshot, create the indexes and fill ``refs``, fully inspect again and require the
    identical state, then commit. Any failure rolls back (a killed process leaves SQLite's rollback
    journal, which restores the earlier file when next opened). Authority data, the schema version
    and every state token are unchanged. A catalog that already has the structures is only verified.
    ``rebuild`` first drops whatever lookup structures exist (damaged or incomplete) in the same
    transaction, so it succeeds only when everything else in the catalog verifies. ``check``
    reports the shape and verifies without writing. A process killed mid-migration leaves SQLite's
    rollback journal, which every other command refuses; ``recover`` (writer lock held) lets SQLite roll
    it back, then migrates as usual, so the catalog returns to a verified state before anything is written.
    """
    home, db, backups = locations(root)
    private_file(db)
    if check:
        with closing(connect(db)) as connection:
            connection.execute("BEGIN")
            shape = lookup_shape(connection)
            state = None if shape == "partial" else inspect(connection, detail=False)[0]
            return {"command": "migrate-indexes", "migrated": False, "lookup": shape, "state": state}
    with writer_lock(home):
        if recover:
            _recover_journal(db)
        elif safe_path(str(db) + "-journal", probe=True).exists():
            raise CatalogError("SQLite sidecar present; explicit recovery required: "
                               "run migrate-indexes --recover after confirming no other process is migrating")
        return _migrate_locked(home, db, backups, rebuild)


def _migrate_locked(home, db, backups, rebuild):
    with closing(connect(db, write=True)) as connection:
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


def _binding(connection, *, hash_bytes=True):
    """What a validation covers: the database file, the validator bytes and the runtime.

    With ``hash_bytes`` false a file that only its bytes could bind gives None, never a hash.

    The file is bound by its identity (device, inode, size, mtime, ctime, SQLite change counter;
    docs/design/current-state.md, "What a catalog write validates") when it is a plain rollback-journal file; otherwise by the SHA-256 of its exact
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
    if not hash_bytes:
        return None
    return {"version": 1, "snapshot_sha256": hashlib.sha256(connection.serialize()).hexdigest(),
            "validator_sha256": VALIDATOR_SHA256, "runtime": runtime}


def _today():
    return snapshots.utcnow().strftime("%Y-%m-%d")


def _write_receipt(home, binding, state, validated_on=None):
    """``validated_on`` is the UTC date of the last full replay this receipt descends from.

    It is recorded for the audit report; it never limits how long the receipt is trusted.
    """
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

    A receipt is trusted for as long as it covers exactly this file (Kata 91h5): there is no daily
    expiry. A change the identity cannot see (an edit that restores size, mtime, ctime and the
    change counter, or silent media corruption) is found by the scheduled audit
    (``python -m tools.store_audit``), ``status --audit``, backup verification or restore. A
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
    state = _head_state(connection)
    require(state == saved["state"], "Read-validation state mismatch; run audit")
    return state, saved["validated_on"]


# A writer commits, then attests the new bytes, all under the writer lock. A reader that meets the new
# file before the attestation would replay the whole history (17-38 s on a 37k-rendition instance)
# for a receipt that lands moments later; it waits at most this long for the writer to finish first.
ATTESTATION_WAIT_SECONDS = 5.0


def _await_attestation(connection, home):
    """Wait boundedly while another process holds the writer lock; True when there was a writer to wait for.

    The read transaction ends first and a new one begins after: holding SQLite's read lock through the
    wait would block that writer's next commit (it needs the lock exclusively) while this reader waits
    for it, each waiting on the other until the bound.
    """
    if not _writer_active(home):
        return False
    connection.execute("COMMIT")
    deadline = time.monotonic() + ATTESTATION_WAIT_SECONDS
    while _writer_active(home) and time.monotonic() < deadline:
        time.sleep(0.01)
    connection.execute("BEGIN")
    return True


def validated_state(connection, home, *, audit=False):
    """Reuse full inspection only for the identical database file and validator bytes.

    A miss while a writer holds the writer lock (it is between its commit and its attestation, or
    mid-write) waits for that writer and asks again against the file as it then is: a receipt is
    trusted only when it covers exactly the file this connection reads, as ever.
    """
    binding = _binding(connection)
    trusted = None if audit else _trusted_state(connection, home, binding)
    if trusted is None and not audit and _await_attestation(connection, home):
        binding = _binding(connection)
        trusted = _trusted_state(connection, home, binding)
    if trusted is not None:
        return trusted[0]
    state = inspect(connection, detail=False)[0]
    _write_receipt(home, binding, state)
    return state


def observed_state(connection, home, seen, *, inspect_limit=None):
    """The validated state for a read-only observer; writes nothing, not even a receipt.

    For callers such as ``tools/intake/progress.py`` that report on the catalog and must not change
    it. ``connection`` holds a read transaction. A read-validation receipt that covers exactly this
    file vouches for a state a writer or reader already validated in full. ``seen`` is a dict
    the caller keeps for the life of its process: an identity-bound file this process has already
    inspected in full, with the same head, reuses that state. Otherwise the whole history is
    inspected and, for an identity-bound file, remembered in ``seen``.

    Unlike ``validated_state``, a receipt or binding that cannot be read or checked (CatalogError,
    OSError, ValueError) is not reported: it is never trusted, so the full inspection decides and
    any real defect surfaces as that inspection's refusal. ``status`` and ``audit`` remain the
    commands that report an unusable receipt.

    With ``inspect_limit``, a database file larger than that many bytes is neither inspected nor
    hashed for a byte binding (as while a writer's journal is beside it): without an
    identity-bound receipt or memo the result is None. An O(history) pass holds the read lock throughout, and in rollback-journal
    mode a writer's commit then waits in PENDING while every new reader, such as the
    instance-identity probe, fails (Kata 8as8).
    """
    connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    path = connection.execute("PRAGMA database_list").fetchone()["file"]
    large = inspect_limit is not None and os.path.getsize(path) > inspect_limit
    try:
        binding = _binding(connection, hash_bytes=not large)
        if binding is None:
            return None
        marker = json.dumps(binding, sort_keys=True)
        trusted = _trusted_state(connection, home, binding)
    except (CatalogError, OSError, ValueError):
        binding = marker = trusted = None
    if trusted is not None:
        return trusted[0]
    if marker is not None and seen.get("marker") == marker and seen["head"] == _head_state(connection):
        return seen["state"]
    if large:
        return None
    state = inspect(connection, detail=False)[0]
    if marker is not None and binding["version"] == 2:
        seen.clear()
        seen.update(marker=marker, head=_head_state(connection), state=state)
    return state


def status(root, *, audit=False):
    home, db, _ = locations(root)
    require(home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700, "Catalog home missing or not private")
    with closing(connect(db)) as connection:
        connection.execute("BEGIN")
        return validated_state(connection, home, audit=audit)


class _Writer:
    """One writer-locked transaction's view: connection, trusted or fully validated head, history."""

    def __init__(self, root, home, db, backups, connection, binding, trusted, state, current, validated_on):
        self.root, self.home, self.db, self.backups = root, home, db, backups
        self.connection, self.binding, self.trusted = connection, binding, trusted
        self.state, self.current, self.validated_on = state, current, validated_on
        self.committed = _CommittedStates(connection, state)


@contextmanager
def _writer(root):
    """The writer lock and a read transaction on a validated head (the first step of every write)."""
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
        validated_on = None  # a full replay below starts the audit date afresh
        if trusted is None:
            state, current, _, _, _ = inspect(connection)
        else:
            (state, validated_on), current = trusted, None
        yield _Writer(root, home, db, backups, connection, binding, trusted, state, current, validated_on)


def _commit_revision(writer, incoming, *, request_id, actor, reason, input_hash, added,
                     precommit_check=None):
    """Snapshot if the UTC day has none, then write ``incoming`` as the next revision and attest it.

    Returns ``(written state, committed_at, snapshot, retention)``.
    """
    connection, state = writer.connection, writer.state
    # The head is either replayed in full just above (no receipt) or vouched for by a receipt covering
    # exactly these bytes, so the snapshot is the image of a validated transaction either way and never
    # replays history under the writer lock (Kata 91h5); ``status --audit`` and the scheduled audit verify it.
    taken, retention = _daily_snapshot(writer.root, connection, writer.backups, state, True)
    connection.execute("COMMIT")
    committed_flag = False
    revision = state["revision"] + 1
    try:
        connection.execute("BEGIN IMMEDIATE")
        require(_binding(connection) == writer.binding, "Store changed outside the writer lock")
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
        seal(writer.db)
        written = make_state(state, revision, batch["revision_hash"], state["records"] + added)
    except (OSError, sqlite3.Error, CatalogError) as error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        if committed_flag:
            raise CatalogError(
                f"Write committed at revision {revision}, but durability/backup failed; retry the same request"
            ) from error
        raise
    _attest_commit(connection, writer.home, written, writer.validated_on)
    return written, batch["committed_at"], taken, retention


def _present(committed, connection, marker, incoming):
    """``(revision, state)`` when ``marker``'s first version equals this request's record, else None.

    Raises ConflictingWrite when the marker exists with other content.
    """
    first = _first_version(connection, marker)
    if first is None:
        return None
    mine = canonical(next(record for record in incoming if record["id"] == marker))
    if first[1] != mine:
        raise ConflictingWrite("The marker record already exists with different content")
    return first[0], committed.state(first[0])


def submit(root, records, *, actor, request_id, expected_state, reason,
           precommit_check=None, rebase=False, marker=None):
    """Submit metadata; an optional local guard runs inside the final transaction.

    With ``rebase``, an outdated ``expected_state`` is accepted when no later
    revision wrote any record this request writes; the full graph is still
    validated against the current state. Overlap raises ConflictingWrite.

    ``marker`` (the ID of a record this request creates) makes the request idempotent by that record
    as well as by ``request_id``: a catalog whose first version of the marker equals this request's is
    reported as already committed at that revision, whichever revision (a batch's, say) wrote it.

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
    require(marker is None or marker in {record["id"] for record in incoming},
            "The marker must be one of the request's record IDs")
    input_hash = fingerprint({"actor": actor, "reason": reason, "records": incoming})
    with _writer(root) as writer:
        connection, state, current, committed = writer.connection, writer.state, writer.current, writer.committed
        prior = committed.prior(request_id)
        if prior is not None:
            require(prior["input_hash"] == input_hash, "Request ID reused with different actor, reason, or records")
            if writer.trusted is None:
                # The replay above fully validated this file; keep the next retry or read from repeating it.
                try:
                    _write_receipt(writer.home, writer.binding, state)
                except (OSError, CatalogError):
                    pass
            if precommit_check is not None:
                precommit_check()
            return {"command": "submit", "idempotent": True, **state, "state": state,
                    "request_id": request_id, "committed_revision": prior["revision"],
                    "committed_at": prior["committed_at"], "committed_state": committed.state(prior["revision"]),
                    "backup": None, "retention": None}
        if marker is not None:
            found = _present(committed, connection, marker, incoming)
            if found is not None:
                if precommit_check is not None:
                    precommit_check()
                return {"command": "submit", "idempotent": True, **state, "state": state,
                        "request_id": request_id, "committed_revision": found[0],
                        "committed_at": connection.execute("SELECT committed_at FROM revisions WHERE revision=?",
                                                           (found[0],)).fetchone()[0],
                        "committed_state": found[1], "backup": None, "retention": None}
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
        written, committed_at, taken, retention = _commit_revision(
            writer, incoming, request_id=request_id, actor=actor, reason=reason, input_hash=input_hash,
            added=added, precommit_check=precommit_check)
        return {"command": "submit", "idempotent": False, **written, "state": written,
                "request_id": request_id, "committed_revision": written["revision"], "committed_at": committed_at,
                "committed_state": written, "backup": taken, "retention": retention,
                "rebased_from": rebased_from}


class _Layered:
    """Committed lookups overlaid with the records of batch members already accepted.

    Gives ``validate_delta`` the view a member would meet if the members accepted before it had been
    committed one at a time: their records win over the committed ones, and committed records they
    rewrite no longer answer for the old content.
    """

    def __init__(self, old):
        self.old, self.records = old, {}

    def accept(self, records):
        self.records.update((record["id"], record) for record in records)

    def get(self, record_id):
        return self.records[record_id] if record_id in self.records else self.old.get(record_id)

    def referrers(self, ids):
        ids = set(ids)
        kept = [record for record in self.old.referrers(ids) if record["id"] not in self.records]
        return kept + [record for record in self.records.values() if ids.intersection(_references(record))]

    def claimants(self, paths):
        paths = set(paths)
        kept = [record for record in self.old.claimants(paths) if record["id"] not in self.records]
        return kept + [record for record in self.records.values()
                       if record["kind"] == "artifact" and record["data"]["path"] in paths]


def _first_version(connection, record_id):
    """``(revision, payload)`` of the first version ever written for ``record_id``, or None."""
    row = connection.execute("SELECT revision,payload FROM versions WHERE record_id=? ORDER BY revision LIMIT 1",
                             (record_id,)).fetchone()
    return None if row is None else (row[0], row[1])


def _claims(record):
    """What two members of one batch may not both write: a record ID, or a path an artifact holds."""
    claims = [("id", record["id"])]
    if record["kind"] == "artifact" and record["data"]["path"] is not None:
        claims.append(("path", record["data"]["path"]))
    return claims


def _select_members(pending, records_of, old, results):
    """The members of ``pending`` (in order) that commit together; the others are refused in ``results``.

    Two members of one batch may not write the same record or give two artifacts one path: the earlier
    one that is not itself refused keeps it. The retained members are validated together (one ``validate_delta``); only if that is
    refused is each judged against the committed state plus every OTHER retained member, so a member may
    reference one that comes later in the batch, an invalid member is found, and a member that depended on
    a refused one is refused in the next pass. The loop ends with a union that validated.
    """
    pending = list(pending)
    while True:
        taken, active, overlapped = set(), [], []
        for index in pending:
            claims = {key for r in records_of[index] for key in _claims(r)}
            if claims & taken:
                overlapped.append(index)
            else:
                taken |= claims
                active.append(index)
        union = [record for index in active for record in records_of[index]]
        try:
            if union:
                validate_delta(union, get=old.get, referrers=old.referrers, claimants=old.claimants)
        except CatalogError as union_error:
            failing = {}
            for index in active:
                view = _Layered(old)
                view.accept([record for other in active if other != index for record in records_of[other]])
                try:
                    validate_delta(records_of[index], get=view.get, referrers=view.referrers,
                                   claimants=view.claimants)
                except CatalogError as error:
                    failing[index] = error
            if not failing:
                raise union_error  # unreachable unless validate_delta disagrees with itself
            for index, error in failing.items():
                results[index].update(status="refused", error=error, rebased_from=None)
            pending = [index for index in pending if index not in failing]
            continue
        for index in overlapped:
            results[index].update(status="refused", rebased_from=None, error=ConflictingWrite(
                "An earlier member of this batch wrote a record in this member"))
        return active


def marker_present(root, marker):
    """Whether the catalog holds a first version of record ``marker`` (read in one validated snapshot)."""
    text(marker, "Marker", 200)  # any record ID; validate_record has checked its shape on write
    home, db, _ = locations(root)
    require(home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700, "Catalog home missing or not private")
    with closing(connect(db)) as connection:
        connection.execute("BEGIN")
        validated_state(connection, home)
        return _first_version(connection, marker) is not None


def submit_batch(root, members, *, actor, reason, rebase=True, precommit_check=None):
    """Commit many documents' records as ONE revision (Kata herq); each document is judged on its own.

    ``members`` are ``{"marker", "records", "expected_state"}``. The ``marker`` is the ID of a record
    the member creates, so it is the member's idempotency key: a member whose marker already has a
    first version equal to the member's marker record is ``present`` (its revision and state are
    reported, nothing is written); a marker that exists with other content is refused. A retry
    therefore builds its batch from the members not yet present, whatever revisions committed the rest.

    Each remaining member is judged as if committed alone and in order: its records must validate and
    be written by no earlier member of this batch, and its pin must be the head or, with ``rebase``
    (the default), a state in this catalog's history after which no revision wrote one of its records
    (the same rule as ``submit``). A refused member is reported with its error and never blocks the
    others. The accepted members are validated together (``validate_delta`` once; only when that
    refuses are the members validated one by one to find the refused ones) and written in one SQLite
    transaction: all of them or none. The revision hash chain is the ordinary one. The members of a
    batch share one committed revision and state, so they settle together.

    Returns ``{"members": [{marker, status, committed_revision, committed_state, rebased_from, error}],
    state, backup, retention, committed_revision}`` where ``status`` is ``committed``, ``present`` or
    ``refused``; ``committed_revision`` is None when nothing was written.
    """
    text(actor, "Actor", 200)
    text(reason, "Revision reason")
    require(isinstance(members, list) and bool(members), "A batch needs at least one member")
    markers = [member.get("marker") for member in members]
    require(len(set(markers)) == len(markers), "Duplicate batch markers")
    with _writer(root) as writer:
        connection, state, committed = writer.connection, writer.state, writer.committed
        old = _CurrentRecords(connection)
        results = [{"marker": marker, "status": None, "committed_revision": None, "committed_state": None,
                    "rebased_from": None, "error": None} for marker in markers]
        records_of, pins, pending = {}, {}, []
        for index, member in enumerate(members):
            result = results[index]
            try:
                validate_records(member["records"])
                incoming = decode(canonical(member["records"]))
                validate_records(incoming)
                ids = [record["id"] for record in incoming]
                require(member["marker"] in ids, "The marker must be one of the member's record IDs")
                found = _present(committed, connection, member["marker"], incoming)
                if found is not None:
                    result.update(status="present", committed_revision=found[0], committed_state=found[1])
                    continue
                if member["expected_state"] != state["state_token"]:
                    if not rebase:
                        raise StaleState("Stale expected state; re-read before writing")
                    token = member["expected_state"]
                    if token not in pins:
                        pins[token] = committed.revision_of(token)
                    if pins[token] is None:
                        raise ConflictingWrite("Pinned state is not in this catalog's history")
                    if committed.written_after(pins[token], ids):
                        raise ConflictingWrite("A later revision rewrote a record in this request")
                    result["rebased_from"] = pins[token]
            except CatalogError as error:
                result.update(status="refused", error=error)
                continue
            records_of[index] = incoming
            pending.append(index)
        accepted = _select_members(pending, records_of, old, results)
        if not accepted:
            return {"command": "submit-batch", "members": results, "state": state, "backup": None,
                    "retention": None, "committed_revision": None}
        incoming = [record for index in accepted for record in records_of[index]]
        request_id = "batch-" + fingerprint([markers[index] for index in accepted])[:40]
        require(committed.prior(request_id) is None, "Batch request ID already committed")
        added = sum(1 for record in incoming if old.get(record["id"]) is None)
        input_hash = fingerprint({"actor": actor, "reason": reason, "records": incoming})
        written, committed_at, taken, retention = _commit_revision(
            writer, incoming, request_id=request_id, actor=actor, reason=reason, input_hash=input_hash,
            added=added, precommit_check=precommit_check)
        for index in accepted:
            results[index].update(status="committed", committed_revision=written["revision"],
                                  committed_state=written)
        return {"command": "submit-batch", "members": results, "state": written, "backup": taken,
                "retention": retention, "committed_revision": written["revision"],
                "committed_at": committed_at}


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
        self._scan = None

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

        Searches newest first, so the cost follows how far behind the pin is. The search resumes where
        the last one stopped, so a batch whose members carry many pins reads the history once.
        """
        if self._scan is None:
            hashes = dict(self.connection.execute("SELECT revision,revision_hash FROM revisions"))
            self._scan = [self.head["revision"], self.head["records"], hashes, {}]
        scan = self._scan
        while token not in scan[3] and scan[0] >= 0:
            number = scan[0]
            if number == 0:
                scan[3].setdefault(make_state(self.head, 0, "", 0)["state_token"], 0)
            else:
                scan[3].setdefault(make_state(self.head, number, scan[2][number], scan[1])["state_token"], number)
                scan[1] -= self._first_writes().get(number, 0)
            scan[0] -= 1
        return scan[3].get(token)

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


def _check_read_arguments(kind, receipt, source, path, coverage, audience, after, limit, expected_state,
                          include_history):
    """Validate ``read_records`` arguments; returns the normalized path."""
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
    return path


def _linked_scope(label):
    """The scan form of the receipt or source scope: linked receipts, their members and relationships."""
    receipts = ("SELECT id FROM catalog_current WHERE kind='receipt' AND id=:receipt"
                if label == "receipt" else
                "SELECT id FROM catalog_current WHERE kind='receipt' AND EXISTS "
                "(SELECT 1 FROM json_each(payload,'$.data.source_ids') WHERE value=:source)")
    sources = ("SELECT value FROM catalog_current,json_each(payload,'$.data.source_ids') "
               "WHERE catalog_current.id IN (SELECT id FROM linked_receipts)" if label == "receipt"
               else "SELECT :source")
    return f"""id IN (
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
            )"""


def _read_filters(connection, kind, receipt, source, path, coverage, audience):
    """The ``WHERE`` clauses and named parameters for the ``read_records`` filters."""
    scopes = []
    parameters = {}
    if receipt is not None and lookup_shape(connection) == "indexed":
        scopes.append("id IN (SELECT value FROM json_each(:receipt_scope))")
        parameters["receipt_scope"] = json.dumps(sorted(_receipt_scope(connection, receipt)))
    elif receipt is not None:
        scopes.append(_linked_scope("receipt"))
        parameters["receipt"] = receipt
    if source is not None:
        scopes.append(_linked_scope("source"))
        parameters["source"] = source
    if kind is not None:
        scopes.append("kind=:kind")
        parameters["kind"] = kind
    for key, value in (("path", path), ("coverage", coverage), ("audience", audience)):
        if value is not None:
            scopes.append(f"(kind='artifact' AND json_extract(payload,'$.data.{key}')=:{key})")
            parameters[key] = value
    return scopes, parameters


def _record_versions(connection, record_id):
    return [{
        "revision": row["revision"], "request_id": row["request_id"], "actor": row["actor"],
        "reason": row["reason"], "committed_at": row["committed_at"], "record": decode(row["payload"]),
    } for row in connection.execute(
        "SELECT r.*,v.payload FROM versions v JOIN revisions r USING(revision) "
        "WHERE v.record_id=? ORDER BY revision", (record_id,))]


def read_records(root, *, kind=None, receipt=None, source=None, path=None, coverage=None,
                 audience=None, after=None, limit=25, expected_state=None, include_history=False):
    path = _check_read_arguments(kind, receipt, source, path, coverage, audience, after, limit, expected_state,
                                 include_history)
    home, db, _ = locations(root)
    require(home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700, "Catalog home missing or not private")
    with closing(connect(db)) as connection:
        connection.execute("BEGIN")
        state = validated_state(connection, home)
        if expected_state is not None and expected_state != state["state_token"]:
            raise StaleState("Stale query state")
        current_view(connection)
        scopes, parameters = _read_filters(connection, kind, receipt, source, path, coverage, audience)
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
                record["versions"] = _record_versions(connection, record_id)
            result.append(record)
        return {"authority": AUTHORITY, "state": state, "records": result, "count": len(result),
                "total_matching": total,
                "next_after": ids[-1] if len(page) > limit else None}


def _receipt_scope(connection, receipt):
    """The ids the receipt scope of ``read_records`` selects, probed through the lookup indexes.

    The same set as the scan form: the current receipt, its source ids, the current artifacts
    naming it as ``receipt_id``, and the current relationships from or to any of those that is a
    current artifact or source. Only for an ``indexed`` catalog.
    """
    def current(field, value, kind):
        return {row[0] for row in connection.execute(
            f"SELECT v.record_id FROM versions v WHERE {_lookup_expression(field)}=? "
            "AND json_extract(v.payload,'$.kind')=?" + _CurrentRecords.CURRENT, (value, kind))}
    row = connection.execute("SELECT kind,payload FROM catalog_current WHERE id=?", (receipt,)).fetchone()
    if row is None or row["kind"] != "receipt":
        return set()
    members = {receipt, *(value for value, in connection.execute(
        "SELECT value FROM json_each(?,'$.data.source_ids')", (row["payload"],)))}
    members |= current("receipt_id", receipt, "artifact")
    endpoints = [row[0] for row in connection.execute(
        "SELECT id FROM catalog_current WHERE kind IN ('artifact','source') "
        "AND id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(members)),))]
    for endpoint in endpoints:
        members |= current("from_id", endpoint, "relationship") | current("to_id", endpoint, "relationship")
    return members


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
    return _receipt_read(root, receipt_ids, limit=limit, expected_state=expected_state)[1]


def receipt_snapshot(root, receipt_ids, *, limit=25):
    """``(receipt_metadata(...), read_records_by_receipt(...))`` read from ONE validated snapshot.

    Two calls can straddle a commit (the second then refuses the first's state as stale) and pay the
    validation, the current-record copy and its indexes twice; this pays them once and cannot straddle.
    Each receipt's ``records`` is a sequence that decodes its records when iterated, so a caller that
    visits one receipt at a time never holds every receipt's decoded records (about 1 GB for 40,000
    receipts) at once; the decoded values equal those of ``read_records_by_receipt``.
    """
    return _receipt_read(root, receipt_ids, limit=limit, registrations=True, lazy=True)


def _receipt_read(root, receipt_ids, *, limit, expected_state=None, registrations=False, lazy=False):
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
        # The copy is this snapshot's current records, so the read transaction ends here and the
        # joins and lookups below hold no lock on the catalog: a long hold made a writer's commit
        # wait in PENDING and new readers, such as the instance-identity probe, fail (Kata 8as8).
        connection.execute("COMMIT")
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
                "records": (_EncodedRecords(_payloads(connection, page)) if lazy else
                            [current_record(connection, record_id, "current_rows") for record_id in page]),
                "count": len(page), "total_matching": len(members),
                "next_after": page[-1] if len(members) > limit else None}
        if registrations:
            return _registrations(connection, state, receipt_ids, "current_rows"), result
        return None, result


def _payloads(connection, record_ids):
    payloads = []
    for record_id in record_ids:
        row = connection.execute("SELECT payload FROM current_rows WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise MissingRecord("Record missing from authoritative catalog: " + record_id)
        payloads.append(row["payload"])
    return payloads


class _EncodedRecords:
    """A read-only sequence of records kept as their stored JSON text and decoded on each iteration."""

    def __init__(self, payloads):
        self._payloads = payloads

    def __len__(self):
        return len(self._payloads)

    def __iter__(self):
        return (decode(payload) for payload in self._payloads)

    def __getitem__(self, index):
        return decode(self._payloads[index])

    def __eq__(self, other):
        return list(self) == list(other)


def current_record(connection, record_id, table="catalog_current"):
    row = connection.execute(f"SELECT payload FROM {table} WHERE id=?", (record_id,)).fetchone()
    if row is None:
        raise MissingRecord("Record missing from authoritative catalog: " + record_id)
    return decode(row["payload"])



def _registrations(connection, state, receipt_ids, table):
    selected = {}
    for receipt_id in receipt_ids:
        receipt = current_record(connection, receipt_id, table)
        if receipt["kind"] != "receipt":
            raise MissingRecord("Receipt missing from authoritative catalog: " + receipt_id)
        selected[receipt_id] = {
            "receipt": receipt, "artifact": current_record(connection, receipt["data"]["artifact_id"], table),
            "sources": [current_record(connection, sid, table) for sid in receipt["data"]["source_ids"]],
        }
    return {"state": state, "registrations": selected}


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
        return _registrations(connection, state, receipt_ids, "catalog_current")


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
                require(not safe_path(str(db) + suffix, probe=True).exists(), "SQLite sidecar present; explicit recovery required")
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


if __name__ == "__main__":
    from tools.catalog.cli import main
    sys.exit(main())
