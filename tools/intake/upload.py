"""Explicit manual file upload and local-drop intake over the durable ledger.

An operator names one file (or one prepared drop directory). The bytes are read
once through a symlink-free, regular-file-only descriptor, type-checked against
an already supported extractor format, privately staged and published under a
per-submission directory in the first configured original root, then registered
through the supported catalog writer as a durable ``catalog-submit`` stage.

A receipt is reported separately from processing: upload never extracts,
reviews, selects or publishes anything. A filename never supplies a remote
identity or version; only explicitly declared origin fields do.
"""
from contextlib import closing, contextmanager
import fcntl
import io
import os
from pathlib import Path
import re
import stat
import uuid
import zipfile

from tools import policy
from tools.catalog import catalog_store as catalog
from tools.extraction import mhtml
from tools.extraction.extract import TEXT_SUFFIXES
from . import runner, store
from .contracts import IntakeError, canonical, decode, digest, fields, identifier, relative, require, sha, text

VERSION = 1
DROP_MARKER = ".librarian-drop"
DESCRIPTOR_SUFFIX = ".submission.json"
UPLOAD_DIRECTORY = "intake"
SIDECAR = "submission.json"
MIB = 1024 * 1024
# Formats and limits mirror the existing extractors; no new format scope.
FORMATS = {
    "pdf": ({".pdf"}, 64 * MIB),
    "image": ({".png", ".jpg", ".jpeg"}, 64 * MIB),
    "odt": ({".odt"}, 16 * MIB),
    "text": (set(TEXT_SUFFIXES), 16 * MIB),
    "gemini-txt": ({".txt"}, 16 * MIB),
    "mhtml": (set(TEXT_SUFFIXES), 16 * MIB),
}
IDENTITY_BASIS = {"declared-remote": "declared-origin", "operator-supplied": "operator-supplied-bytes",
                  "local-path": "local-path-copy"}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
RECEIPT_STATES = {"succeeded": "committed", "staged": "pending", "running": "pending",
                  "retryable": "retryable", "blocked": "blocked", "dead-letter": "dead-letter"}
NOTICE = ("A committed receipt registers retained bytes only; it is not extraction, "
          "review, selection or qualified retrieval.")


def validate_descriptor(value):
    fields(value, {"version", "submission_id", "correction_of", "format", "name",
                   "actor", "reason", "origin", "expected_sha256"})
    require(type(value["version"]) is int and value["version"] == VERSION, "invalid_descriptor")
    identifier(value["submission_id"])
    require(len(value["submission_id"]) <= 100, "invalid_descriptor")
    if value["correction_of"] is not None:
        identifier(value["correction_of"])
        require(value["correction_of"] != value["submission_id"], "invalid_descriptor")
    require(isinstance(value["format"], str) and value["format"] in FORMATS, "unsupported_type")
    name = value["name"]
    require(isinstance(name, str) and NAME.fullmatch(name) is not None
            and not name.endswith(DESCRIPTOR_SUFFIX) and name != SIDECAR, "invalid_name")
    require(Path(name).suffix.lower() in FORMATS[value["format"]][0], "unsupported_type")
    text(value["actor"], 200)
    text(value["reason"], 500)
    origin = value["origin"]
    fields(origin, {"kind", "system", "locator", "version"})
    if origin["kind"] == "operator-supplied":
        # Without a declaration there is no remote identity to record.
        require(origin["system"] is None and origin["locator"] is None
                and origin["version"] is None, "invalid_origin")
    elif origin["kind"] == "local-path":
        # A local copy names its read-only source tree and relative path; it never
        # asserts a remote identity or version.
        require(origin["version"] is None and isinstance(origin["locator"], str), "invalid_origin")
        try:
            text(origin["system"], 200)
            relative(origin["locator"])
        except IntakeError as error:
            raise IntakeError("invalid_origin") from error
    else:
        require(origin["kind"] == "declared-remote", "invalid_origin")
        text(origin["system"], 200)
        text(origin["locator"], 2000)
        if origin["version"] is not None:
            text(origin["version"], 200)
    if value["expected_sha256"] is not None:
        sha(value["expected_sha256"])
    return value


def descriptor(*, submission_id, format_name, name, actor, reason, correction_of=None,
               origin_system=None, origin_locator=None, origin_version=None, expected_sha256=None,
               origin_kind=None):
    declared = any(v is not None for v in (origin_system, origin_locator, origin_version))
    kind = origin_kind or ("declared-remote" if declared else "operator-supplied")
    return validate_descriptor({
        "version": VERSION, "submission_id": submission_id, "correction_of": correction_of,
        "format": format_name, "name": name, "actor": actor, "reason": reason,
        "origin": {"kind": kind,
                   "system": origin_system, "locator": origin_locator, "version": origin_version},
        "expected_sha256": expected_sha256})


def snapshot(stat_result):
    return (stat_result.st_dev, stat_result.st_ino, stat_result.st_size,
            stat_result.st_mtime_ns, stat_result.st_ctime_ns)


def read_stream(stream, size):
    return stream.read(size)


def read_file(path, limit):
    """Read one regular file once; refuse symlinks, devices and concurrent mutation."""
    path = Path(path)
    require(".." not in path.parts, "unsafe_path")
    path = path.absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), "unsafe_path")
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise IntakeError("missing_input") from error
    require(stat.S_ISREG(before.st_mode), "not_regular_file")
    try:
        descriptor_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise IntakeError("unsafe_path") from error
    with os.fdopen(descriptor_fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        require(stat.S_ISREG(opened.st_mode) and snapshot(opened)[:2] == snapshot(before)[:2], "changed_input")
        raw = read_stream(stream, limit + 1)
        after = os.fstat(stream.fileno())
    require(len(raw) <= limit, "input_budget")
    final = path.stat(follow_symlinks=False)
    require(snapshot(opened) == snapshot(after) == snapshot(final) and len(raw) == after.st_size, "changed_input")
    return raw


def check_type(raw, format_name, name):
    suffix = Path(name).suffix.lower()
    require(len(raw) > 0, "unsupported_type")
    if format_name == "pdf":
        require(raw.startswith(b"%PDF-"), "type_mismatch")
    elif format_name == "image":
        magic = b"\x89PNG\r\n\x1a\n" if suffix == ".png" else b"\xff\xd8\xff"
        require(raw.startswith(magic), "type_mismatch")
    elif format_name == "odt":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                info = archive.getinfo("mimetype")
                require(info.file_size < 100, "type_mismatch")
                mimetype = archive.read(info)
        except (zipfile.BadZipFile, KeyError, OSError, RuntimeError, ValueError) as error:
            raise IntakeError("type_mismatch") from error
        require(mimetype == b"application/vnd.oasis.opendocument.text", "type_mismatch")
    elif format_name == "mhtml":
        require(mhtml.recognised(raw), "type_mismatch")
    else:
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise IntakeError("type_mismatch") from error
        require("\0" not in decoded, "type_mismatch")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_directory(root, relative, owned):
    """Create missing directories below root; the last ``owned`` levels must be 0700."""
    parts = Path(relative).parts
    cursor = root
    for index, part in enumerate(parts):
        cursor = cursor / part
        require(not cursor.is_symlink(), "unsafe_path")
        if not cursor.exists():
            cursor.mkdir(mode=0o700)
            sync_directory(cursor.parent)
        require(cursor.is_dir(), "unsafe_path")
        if index >= len(parts) - owned:
            require(stat.S_IMODE(cursor.stat(follow_symlinks=False).st_mode) == 0o700, "not_private")
    return cursor


def write_all(stream, raw):
    stream.write(raw)


def publish(directory, name, raw):
    """Atomically publish bytes at directory/name; equal bytes are idempotent, others conflict."""
    final = directory / name
    for stale in directory.glob(f".{name}.*.partial"):
        if stale.is_file() and not stale.is_symlink():
            stale.unlink()
    if final.exists() or final.is_symlink():
        require(not final.is_symlink() and final.is_file(), "destination_conflict")
        require(stat.S_IMODE(final.stat(follow_symlinks=False).st_mode) == 0o600, "not_private")
        try:
            existing = read_file(final, len(raw))
        except IntakeError as error:
            raise IntakeError("destination_conflict") from error
        require(digest(existing) == digest(raw), "destination_conflict")
        return False
    partial = directory / f".{name}.{uuid.uuid4().hex}.partial"
    fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            write_all(stream, raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(partial, final)
        except FileExistsError as error:
            raise IntakeError("destination_conflict") from error
        sync_directory(directory)
    finally:
        partial.unlink(missing_ok=True)
        sync_directory(directory)
    return True


@contextmanager
def upload_lock(root):
    _, home, _ = store.locations(root)
    store.private(home, True)
    lock = store.safe(home / "upload.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        store.private(lock)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def correlation(submission_id):
    return "upload-" + submission_id


def ledger_request(root, correlation_id):
    with store.locked(root) as c:
        row = c.execute("SELECT raw FROM requests WHERE correlation_id=?", (correlation_id,)).fetchone()
        return None if row is None else decode(row[0])


class Taken:
    """What the planner needs to know about catalog IDs that are already used or promised.

    ``highest`` maps each allocation prefix to the largest number in use (0 when none);
    ``present`` holds those of the probed IDs that are in use. Two views combine with ``|``.
    """
    __slots__ = ("highest", "present")

    def __init__(self, highest=None, present=frozenset()):
        self.highest = {prefix: 0 for prefix in PREFIXES} if highest is None else highest
        self.present = frozenset(present)

    def __or__(self, other):
        return Taken({p: max(self.highest[p], other.highest[p]) for p in PREFIXES},
                     self.present | other.present)

    def next_id(self, prefix):
        return f"{prefix}{self.highest[prefix] + 1}"


PREFIXES = ("S", "D")
# A digit run this short always fits SQLite's 64-bit integer, so ``CAST`` cannot saturate.
SQL_SAFE_DIGITS = 17


def number_of(identifier, prefix):
    """The number a ``prefix`` ID allocates from, or None for any other ID (unchanged rule)."""
    return int(identifier[1:]) if identifier[:1] == prefix and identifier[1:].isdigit() else None


def highest_number(identifiers, prefix):
    return max((n for n in (number_of(i, prefix) for i in identifiers) if n is not None), default=0)


def catalog_highest(connection, prefix):
    """Largest ``prefix`` number among current record IDs, read from the primary-key index.

    Equal to ``highest_number`` over every current ID (the catalog validates record IDs as ASCII
    ``[A-Za-z0-9-]``, so every current ID is covered by the ASCII-digit filter). IDs with a digit run
    too long for SQLite's integers take the exact Python path over the same indexed range.
    """
    low, high = prefix, chr(ord(prefix) + 1)
    number, longest = connection.execute(
        "SELECT max(CAST(substr(record_id,2) AS INTEGER)), max(length(record_id)) FROM current_records "
        "WHERE record_id >= ? AND record_id < ? AND length(record_id) > 1 "
        "AND substr(record_id,2) NOT GLOB '*[^0-9]*'", (low, high)).fetchone()
    if longest is not None and longest - 1 > SQL_SAFE_DIGITS:
        return highest_number((r[0] for r in connection.execute(
            "SELECT record_id FROM current_records WHERE record_id >= ? AND record_id < ?", (low, high))), prefix)
    return number or 0


def catalog_view(root, probe=()):
    """The validated catalog state and a ``Taken`` for the current records, from one snapshot.

    Only the highest ``S``/``D`` numbers and which ``probe`` IDs exist are read; the current
    records are never materialized (see "Intake ledger" in docs/design/current-state.md).
    """
    probe = sorted(set(probe))
    try:
        home, db, _ = catalog.locations(root)
        require(home.is_dir(), "catalog_unavailable")
        with closing(catalog.connect(db)) as connection:
            connection.execute("BEGIN")
            state = catalog.validated_state(connection, home)
            highest = {prefix: catalog_highest(connection, prefix) for prefix in PREFIXES}
            present = {row[0] for row in connection.execute(
                "SELECT record_id FROM current_records WHERE record_id IN (%s)" % ",".join("?" * len(probe)),
                probe)} if probe else set()
    except (catalog.CatalogError, OSError) as error:
        raise IntakeError("catalog_unavailable") from error
    return state, Taken(highest, present)


# Promised IDs of a saved request, by the SHA-256 the ledger stores for its exact bytes. The key is
# the content, so a hit is correct for any row carrying that digest; a rewritten request has a
# different digest and is decoded afresh. Each fresh entry is checked against its own bytes, and
# the ledger's validation (or its attested hand-off, see "Intake ledger" in docs/design/current-state.md) is what ties every other
# stored digest to its row. The memo is per process, bounded, and never persisted.
PROMISES = {}
PROMISES_LIMIT = 65536
# Above this many unseen rows one scan of the upload requests beats a lookup per row.
BULK = 64


def promised(raw):
    """``(ids, highest S, highest D)`` for every ``catalog-submit`` stage of one saved request."""
    ids = set()
    for stage in decode(raw)["stages"]:
        if stage["operation"] == "catalog-submit":
            ids.update(record["id"] for record in stage["arguments"]["records"])
    return (tuple(ids), *(highest_number(ids, prefix) for prefix in PREFIXES))


class Reserved:
    """Promised IDs of every ``upload-*`` request, extended with only the requests added since.

    Like ``store.Heads`` it follows the ledger by row id: requests are appended under the ledger
    lock and never deleted or rewritten by the toolkit, so ``top`` (the highest row id read) names
    exactly the requests already counted. A different ledger file (another root, or the same path
    replaced: device and inode) or a smaller highest row id resets it. A row added since is read,
    checked against its stored digest and decoded once. Only a finished, valid read advances it.
    Per process; never persisted.
    """

    def __init__(self):
        self.reset(None)

    def reset(self, key):
        self.key, self.top, self.ids, self.over = key, 0, set(), False
        self.highest = {prefix: 0 for prefix in PREFIXES}

    def extend(self, c, key):
        """Bring the index up to date; False when this ledger holds more than ``INDEX_LIMIT`` IDs."""
        top = c.execute("SELECT max(rowid) FROM requests").fetchone()[0] or 0
        if key != self.key or top < self.top:
            self.reset(key)
        if self.over:
            return False  # remembered: later calls go straight to the scan, not through a re-read
        if top == self.top:
            return True
        # Streamed in row order and abandoned as soon as the bound is passed, so an oversized
        # ledger is never read whole here; the entries are applied only if all of them fit.
        entries = []
        held = len(self.ids)
        for _, sha256, raw in c.execute(
                "SELECT rowid, request_sha256, raw FROM requests "
                "WHERE rowid>? AND rowid<=? AND correlation_id LIKE 'upload-%' ORDER BY rowid", (self.top, top)):
            require(digest(raw) == sha256, "corrupt_request")
            entry = promised(raw)
            held += len(entry[0])
            if held > INDEX_LIMIT:
                self.reset(key)
                self.over, self.top = True, top
                return False
            entries.append(entry)
        for entry in entries:
            self.ids.update(entry[0])
            for index, prefix in enumerate(PREFIXES, 1):
                self.highest[prefix] = max(self.highest[prefix], entry[index])
        self.top = top
        return True


# Promised IDs the index may hold (about 100 bytes each); a ledger past it takes the digest-memo scan.
INDEX_LIMIT = 500_000
RESERVED = Reserved()


def forget_reserved():
    RESERVED.reset(None)


def reserved_ids(root, probe=()):
    """A ``Taken`` for catalog IDs promised by staged upload requests that may not be committed yet.

    Every ``upload-*`` request counts, whatever its stage status, so a blocked, retrying or
    dead-lettered request keeps its IDs. Only requests added since the previous call are read.
    """
    probe = frozenset(probe)
    with store.locked(root) as c:
        _, _, db = store.locations(root)
        info = os.stat(db)
        if RESERVED.extend(c, (str(store.safe(root)), info.st_dev, info.st_ino)):
            return Taken(dict(RESERVED.highest), probe & RESERVED.ids)
    return scanned_ids(root, probe)


def scanned_ids(root, probe):
    """``reserved_ids`` without the index: every ``upload-*`` row, decoding only digests not seen before."""
    with store.locked(root) as c:
        rows = c.execute("SELECT correlation_id, request_sha256 FROM requests "
                         "WHERE correlation_id LIKE 'upload-%'").fetchall()
        unseen = {sha256 for _, sha256 in rows if sha256 not in PROMISES}
        fresh = {}
        if len(unseen) > BULK:
            fetched = c.execute("SELECT request_sha256, raw FROM requests WHERE correlation_id LIKE 'upload-%'")
        else:
            fetched = (c.execute("SELECT request_sha256, raw FROM requests WHERE correlation_id=?",
                                 (correlation_id,)).fetchone()
                       for correlation_id, sha256 in rows if sha256 in unseen)
        for sha256, raw in fetched:
            if sha256 in unseen and sha256 not in fresh:
                require(digest(raw) == sha256, "corrupt_request")
                fresh[sha256] = promised(raw)
        for sha256, entry in fresh.items():
            if len(PROMISES) >= PROMISES_LIMIT:
                break
            PROMISES[sha256] = entry
        entries = [PROMISES.get(sha256) or fresh[sha256] for _, sha256 in rows]
    highest = {prefix: max((entry[index] for entry in entries), default=0)
               for index, prefix in enumerate(PREFIXES, 1)}
    present = {i for entry in entries for i in entry[0] if i in probe} if probe else set()
    return Taken(highest, present)


def next_id(prefix, taken):
    return taken.next_id(prefix) if isinstance(taken, Taken) else f"{prefix}{highest_number(taken, prefix) + 1}"


def submit_records(request):
    return next(s for s in request["stages"] if s["operation"] == "catalog-submit")["arguments"]["records"]


def plan(root, value, raw):
    """Build the exact sidecar and catalog records for a new submission."""
    settings = policy.for_root(root)[0]
    require(settings["original_roots"], "no_original_root")
    sid = value["submission_id"]
    directory = f"{settings['original_roots'][0]}/{UPLOAD_DIRECTORY}/{sid}"
    original_path = f"{directory}/{value['name']}"
    sidecar_path = f"{directory}/{SIDECAR}"
    require(policy.allowed(original_path, "original_roots", root)
            and policy.allowed(sidecar_path, "original_roots", root), "excluded_path")
    sidecar = (canonical({
        "version": VERSION, "submission_id": sid, "correction_of": value["correction_of"],
        "format": value["format"], "name": value["name"], "actor": value["actor"],
        "reason": value["reason"], "origin": value["origin"],
        "original": {"path": original_path, "sha256": digest(raw), "bytes": len(raw)},
        "identity_basis": IDENTITY_BASIS[value["origin"]["kind"]],
    }) + "\n").encode()
    return directory, original_path, sidecar_path, sidecar


def records_for(root, value, raw, original_path, sidecar_path, sidecar):
    sid = value["submission_id"]
    # Distinct role suffixes keep every submission's IDs disjoint from every other's.
    artifact_id, meta_id = f"A-upload-{sid}-original", f"A-upload-{sid}-submission"
    relation_id = f"R-upload-{sid}-version-of"
    mine = {artifact_id, meta_id, relation_id}
    state, taken = catalog_view(root, mine)
    taken |= reserved_ids(root, mine)
    require(not taken.present, "identifier_conflict")
    parent_artifact = None
    if value["correction_of"]:
        parent = ledger_request(root, correlation(value["correction_of"]))
        require(parent is not None, "unknown_correction_parent")
        parent_status = status_of(root, correlation(value["correction_of"]))
        parent_receipt = next(r for r in submit_records(parent) if r["kind"] == "receipt")
        if parent_status == "succeeded":
            source_id = parent_receipt["data"]["source_ids"][0]
            parent_artifact = parent_receipt["data"]["artifact_id"]
        else:
            # Only a durably refused, never-committed parent may be replaced; work that
            # can still commit is finished by retrying the exact original submission.
            require(parent_status in {"blocked", "dead-letter"}, "correction_parent_pending")
            committed = catalog_view(root, {r["id"] for r in submit_records(parent)})[1]
            require(not committed.present, "correction_parent_pending")
    if parent_artifact is None:
        source_id = taken.next_id("S")
    receipt_id = taken.next_id("D")
    provenance = [{"path": sidecar_path, "sha256": digest(sidecar), "line_start": None, "line_end": None}]
    records = []
    if parent_artifact is None:
        records.append({"id": source_id, "kind": "source", "data": {"category": "artifact", "provenance": provenance}})
    records += [
        {"id": receipt_id, "kind": "receipt", "data": {
            "source_ids": [source_id], "artifact_id": artifact_id, "provenance": provenance}},
        {"id": artifact_id, "kind": "artifact", "data": {
            "path": original_path, "parent_id": None, "receipt_id": receipt_id, "role": "original",
            "audience": "source", "coverage": "pending-extraction",
            "reason": "Operator-supplied manual upload; not extracted, reviewed or qualified.",
            "presence": "present", "sha256": digest(raw), "hash_basis": "observed", "bytes": len(raw),
            "extraction": "pending", "handling_refs": [], "provenance": provenance}},
        {"id": meta_id, "kind": "artifact", "data": {
            "path": sidecar_path, "parent_id": artifact_id, "receipt_id": receipt_id,
            "role": "preserved-metadata", "audience": "agent", "coverage": "excluded",
            "reason": "Submission provenance sidecar written by manual upload intake.",
            "presence": "present", "sha256": digest(sidecar), "hash_basis": "observed",
            "bytes": len(sidecar), "extraction": "not-required", "handling_refs": [], "provenance": []}},
    ]
    if parent_artifact is not None:
        records.append({"id": relation_id, "kind": "relationship", "data": {
            "from_id": artifact_id, "to_id": parent_artifact, "relation": "version-of",
            "scope": "Manual upload correction of submission " + value["correction_of"],
            "basis": "reported", "provenance": provenance}})
    try:
        catalog.validate_records(records)
    except catalog.CatalogError as error:
        raise IntakeError("invalid_records") from error
    return state, records


def status_of(root, correlation_id):
    return store.inspect(root, correlation_id)["stages"][0]["status"]


def acknowledgment(root, correlation_id, existing, cached=None):
    request, summary = cached if cached else (None, None)
    if cached is None:
        summary = store.inspect(root, correlation_id)
        request = ledger_request(root, correlation_id)
    records = {r["id"]: r for r in submit_records(request)}
    receipt = next(r for r in records.values() if r["kind"] == "receipt")
    original = records[receipt["data"]["artifact_id"]]
    meta = next(r for r in records.values() if r["kind"] == "artifact" and r["data"]["role"] == "preserved-metadata")
    relation = next((r["id"] for r in records.values() if r["kind"] == "relationship"), None)
    stage = summary["stages"][0]
    receipt_state = {"state": RECEIPT_STATES[stage["status"]], "stage": stage["id"],
                     "attempts": len(stage["attempts"]), "conflicts": runner.conflicts(stage)}
    if stage["evidence"]:
        receipt_state["committed_revision"] = stage["evidence"]["committed_revision"]
    elif stage["attempts"] and stage["attempts"][-1]["code"]:
        receipt_state["code"] = stage["attempts"][-1]["code"]
    sidecar_raw = read_file(root / meta["data"]["path"], MIB)
    require(digest(sidecar_raw) == meta["data"]["sha256"], "changed_submission_record")
    sidecar = decode(sidecar_raw)
    return {
        "submission_id": sidecar["submission_id"], "correlation_id": correlation_id,
        "correction_of": sidecar["correction_of"], "existing": existing,
        "original": {"path": original["data"]["path"], "sha256": original["data"]["sha256"],
                     "bytes": original["data"]["bytes"], "format": sidecar["format"]},
        "origin": sidecar["origin"], "identity_basis": sidecar["identity_basis"],
        "records": {"source_id": receipt["data"]["source_ids"][0], "receipt_id": receipt["id"],
                    "artifact_id": original["id"], "submission_artifact_id": meta["id"],
                    "relationship_id": relation},
        "receipt": receipt_state,
        "processing": {"extraction": "not-started", "review": "not-started",
                       "selection": "not-selected", "qualified": False},
        "notice": NOTICE,
    }


def settled(known, correlation_id):
    """``(request, summary)`` read earlier by ``known_requests`` when the only stage had succeeded, else None.

    A succeeded stage never changes again, so it is safe to use without another ledger read; any
    other status may have moved since and is read afresh.
    """
    cached = (known or {}).get(correlation_id)
    return cached if cached and cached[1]["stages"][0]["status"] == "succeeded" else None


def settled_summary(known, correlation_id):
    cached = settled(known, correlation_id)
    return cached and cached[1]


def known_requests(root, correlation_ids):
    """One ledger lock: ``{id: (decoded request, summary)}`` for each saved request among ``correlation_ids``."""
    found = {}
    with store.locked(root) as c:
        for correlation_id in correlation_ids:
            row = c.execute("SELECT raw FROM requests WHERE correlation_id=?", (correlation_id,)).fetchone()
            if row is not None:
                found[correlation_id] = (decode(row[0]), store.summary(c, correlation_id))
    return found


def submit(root, path, value, *, known=None):
    """Stage, publish and register one explicitly supplied file; return its acknowledgment."""
    root = store.safe(root)
    value = validate_descriptor(value)
    raw = read_file(path, FORMATS[value["format"]][1])
    if value["expected_sha256"] is not None:
        require(digest(raw) == value["expected_sha256"], "changed_input")
    check_type(raw, value["format"], value["name"])
    correlation_id = correlation(value["submission_id"])
    with upload_lock(root):
        directory, original_path, sidecar_path, sidecar = plan(root, value, raw)
        prior = known[correlation_id][0] if known and correlation_id in known else ledger_request(root, correlation_id)
        existing = prior is not None
        if existing:
            saved = {r["data"]["path"]: r["data"]["sha256"] for r in submit_records(prior) if r["kind"] == "artifact"}
            require(saved == {original_path: digest(raw), sidecar_path: digest(sidecar)}, "submission_conflict")
        else:
            state, records = records_for(root, value, raw, original_path, sidecar_path, sidecar)
            request = {"version": 1, "correlation_id": correlation_id,
                       "correction_of": correlation(value["correction_of"]) if value["correction_of"] else None,
                       "policy": {"version": 1, "sha256": policy.for_root(root)[1]},
                       "retry": {"max_attempts": 3, "backoff_seconds": 0},
                       "stages": [{"id": "receipt", "kind": "receipt", "operation": "catalog-submit",
                                   "arguments": {"records": records, "actor": value["actor"],
                                                 "request_id": correlation_id, "expected_state": state["state_token"],
                                                 "reason": "Manual upload: " + value["reason"][:400]}}]}
        # Retained bytes are durable before the receipt stage exists, so no
        # run/resume can commit a present artifact whose file is missing.
        target = private_directory(root, directory, 2)
        publish(target, value["name"], raw)
        publish(target, SIDECAR, sidecar)
        if not existing:
            store.stage(root, canonical(request).encode())
        cached = settled(known, correlation_id)
        if cached:
            return acknowledgment(root, correlation_id, existing, cached)
        if status_of(root, correlation_id) in {"staged", "running", "retryable"}:
            # Blocked and dead-letter receipts stay visible; recovery is explicit replay.
            runner.settle(root, correlation_id, "receipt")
        return acknowledgment(root, correlation_id, existing)


def drop_directory(path):
    """A drop is usable only when present, real and explicitly marked; absence is never success."""
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), "unsafe_path")
    require(path.is_dir(), "drop_unavailable")
    marker = path / DROP_MARKER
    require(not marker.is_symlink() and marker.is_file(), "drop_unavailable")
    return path


def drop_entries(path):
    path = drop_directory(path)
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: p.name):
        name = child.name
        if name == DROP_MARKER or name.endswith(DESCRIPTOR_SUFFIX) or name.startswith("."):
            continue
        entry = {"name": name}
        if child.is_symlink() or not child.is_file():
            entry.update(state="refused", code="not_regular_file")
        else:
            sidecar = path / (name + DESCRIPTOR_SUFFIX)
            try:
                value = validate_descriptor(decode(read_file(sidecar, 64 * 1024)))
                require(value["name"] == name, "invalid_name")
                entry.update(state="ready", submission_id=value["submission_id"], descriptor=value)
            except IntakeError as error:
                entry.update(state="refused", code="missing_descriptor" if error.code == "missing_input" else error.code)
        entries.append(entry)
    return path, entries


def drop_scan(path):
    """Discovery only: report candidates without reading payloads or writing anything."""
    _, entries = drop_entries(path)
    return {"entries": [{k: v for k, v in e.items() if k != "descriptor"} for e in entries],
            "count": len(entries), "notice": "Discovery is not commitment; nothing was staged."}


def drop_submit(root, path):
    """Submit each described drop entry; the drop itself is never modified."""
    directory, entries = drop_entries(path)
    results = []
    for entry in entries:
        if entry["state"] != "ready":
            results.append({k: v for k, v in entry.items() if k != "descriptor"})
            continue
        try:
            ack = submit(root, directory / entry["name"], entry["descriptor"])
            results.append({"name": entry["name"], "state": "submitted", "acknowledgment": ack})
        except (IntakeError, OSError) as error:
            results.append({"name": entry["name"], "state": "failed",
                            "code": error.code if isinstance(error, IntakeError) else "os-error"})
    return {"entries": results, "count": len(results),
            "submitted": sum(r["state"] == "submitted" for r in results)}
