"""Private transactional intake intent and recovery ledger."""
from contextlib import closing, contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import stat
import time

from tools import policy
from tools.file_identity import file_identity
from .contracts import IntakeError, LedgerBusy, canonical, decode, decode_request, digest, request_shape, require

APPLICATION_ID = 0x4C494E54
SCHEMA = 1
DDL = """
CREATE TABLE requests (
 correlation_id TEXT PRIMARY KEY, correction_of TEXT REFERENCES requests(correlation_id),
 raw BLOB NOT NULL, request_sha256 TEXT NOT NULL, root TEXT NOT NULL);
CREATE TABLE stages (
 correlation_id TEXT NOT NULL REFERENCES requests(correlation_id), stage_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL, status TEXT NOT NULL, cycle INTEGER NOT NULL DEFAULT 0,
 intent TEXT, intent_sha256 TEXT, evidence TEXT, evidence_sha256 TEXT,
 PRIMARY KEY(correlation_id, stage_id), UNIQUE(correlation_id, ordinal));
CREATE TABLE attempts (
 correlation_id TEXT NOT NULL, stage_id TEXT NOT NULL, cycle INTEGER NOT NULL,
 ordinal INTEGER NOT NULL, started REAL NOT NULL, ended REAL,
 outcome TEXT, category TEXT, code TEXT, eligible_at REAL,
 PRIMARY KEY(correlation_id, stage_id, cycle, ordinal),
 FOREIGN KEY(correlation_id,stage_id) REFERENCES stages(correlation_id,stage_id));
CREATE TABLE replays (
 correlation_id TEXT NOT NULL, stage_id TEXT NOT NULL, cycle INTEGER NOT NULL,
 authorized_at REAL NOT NULL, PRIMARY KEY(correlation_id,stage_id,cycle),
 FOREIGN KEY(correlation_id,stage_id) REFERENCES stages(correlation_id,stage_id));
CREATE TABLE acknowledgments (
 correlation_id TEXT NOT NULL, stage_id TEXT NOT NULL, payload TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL, PRIMARY KEY(correlation_id,stage_id),
 FOREIGN KEY(correlation_id,stage_id) REFERENCES stages(correlation_id,stage_id));
"""


def safe(path):
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), "unsafe_path")
    return path


def locations(root):
    root = safe(root)
    require(root.is_dir(), "missing_root")
    try:
        rel_home = policy.validate_instance_roots(root)["intake_home"]
    except ValueError as error:
        raise IntakeError(str(error)) from error
    home = safe(root / rel_home)
    return root, home, home / "intake.sqlite3"


def private(path, directory=False):
    path = safe(path)
    try:
        info = path.stat()
    except OSError as error:
        raise IntakeError("missing_store") from error
    require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600), "not_private")
    return path


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def initialize(root):
    root, home, db = locations(root)
    require(not home.exists(), "already_initialized")
    safe(home.parent)
    missing = []
    cursor = home.parent
    while cursor != root and not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    home.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for created in reversed(missing):
        created.chmod(0o700)
        sync_directory(created.parent)
    home.mkdir(mode=0o700)
    fd = os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(db)) as c, c:
        c.execute("PRAGMA synchronous=FULL")
        c.executescript(f"PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version={SCHEMA};" + DDL)
    sync_directory(home)
    sync_directory(home.parent)
    return {"initialized": True, "schema": SCHEMA}


# Verified-ledger memo. ``verify`` walks every row in the same order each time, so a fixed-size
# LRU smaller than the ledger never hits. Instead each pass consults the previous pass's memo and
# builds the next one from the rows it just verified: memory follows the live ledger, not history,
# and a cyclic walk cannot thrash. Entries are keyed by the exact bytes (request bytes, artifact
# text) and record the digest those bytes were proven to have, so a hit requires identical bytes
# and an identical stored digest; any rewritten row is a miss and is decoded and hashed afresh.
# Entries are recorded only after the whole pass succeeded; failures are never cached.
MEMO_BYTES = 128 * 1024 * 1024  # key bytes retained; rows beyond it are re-verified every pass
_MEMO_ENTRY_OVERHEAD = 160  # digest string and dict slot per entry
_verified = ({}, {})  # (request bytes -> (sha256, shape), artifact text -> sha256)


def forget_verified():
    global _verified
    _verified = ({}, {})


def verified_bytes():
    """Bytes of keys (plus per-entry overhead) the memo currently holds."""
    requests, artifacts = _verified
    return (sum(len(raw) for raw in requests) + sum(len(text) for text in artifacts)
            + _MEMO_ENTRY_OVERHEAD * (len(requests) + len(artifacts)))


def check_identity(c):
    """Application identity, SQLite integrity and foreign keys (no row decoding)."""
    require(c.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
            and c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA, "incompatible_store")
    require(c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            and not c.execute("PRAGMA foreign_key_check").fetchall(), "corrupt_store")


def snapshot(c):
    """Every ledger row, read in one pass so a caller can verify it without holding a read lock."""
    stages_of, attempts_of, replays_of = {}, {}, {}
    for saved in c.execute("SELECT * FROM stages ORDER BY correlation_id, ordinal"):
        stages_of.setdefault(saved["correlation_id"], []).append(saved)
    for attempt in c.execute("SELECT * FROM attempts ORDER BY correlation_id, stage_id, cycle, ordinal"):
        attempts_of.setdefault((attempt["correlation_id"], attempt["stage_id"]), []).append(attempt)
    for replay in c.execute("SELECT correlation_id, stage_id, cycle FROM replays ORDER BY correlation_id, stage_id, cycle"):
        replays_of.setdefault((replay[0], replay[1]), []).append(replay[2])
    return c.execute("SELECT * FROM requests").fetchall(), stages_of, attempts_of, replays_of


def verify(rows, root):
    """Digest and shape checks over a ``snapshot``; pure, so it needs no database lock."""
    global _verified
    requests, stages_of, attempts_of, replays_of = rows
    known_requests, known_artifacts = _verified
    remembered_requests, remembered_artifacts, held = {}, {}, 0

    def remember(into, key, value):
        nonlocal held
        if held + len(key) + _MEMO_ENTRY_OVERHEAD <= MEMO_BYTES:
            held += len(key) + _MEMO_ENTRY_OVERHEAD
            into[key] = value

    for row in requests:
        raw = row["raw"]
        known = known_requests.get(raw)
        if known is not None and known[0] == row["request_sha256"]:
            shape = known[1]
        else:
            shape = request_shape(raw)
            require(digest(raw) == row["request_sha256"], "corrupt_request")
        correlation_id, correction_of, stage_ids = shape
        require(row["root"] == str(root) and correlation_id == row["correlation_id"]
                and correction_of == row["correction_of"], "corrupt_request")
        remember(remembered_requests, raw, (row["request_sha256"], shape))
        stages = stages_of.get(row["correlation_id"], [])
        require(len(stages) == len(stage_ids), "corrupt_stage")
        for ordinal, (stage_id, saved) in enumerate(zip(stage_ids, stages)):
            require(saved["ordinal"] == ordinal and saved["stage_id"] == stage_id
                    and saved["status"] in {"staged", "running", "retryable", "blocked", "dead-letter", "succeeded", "return-pending"}
                    and type(saved["cycle"]) is int and saved["cycle"] >= 0, "corrupt_stage")
            for key in ("intent", "evidence"):
                raw = saved[key]
                require((raw is None) == (saved[key + "_sha256"] is None), "corrupt_artifact")
                if raw is not None:
                    if known_artifacts.get(raw) != saved[key + "_sha256"]:
                        require(digest(raw.encode()) == saved[key + "_sha256"]
                                and isinstance(decode(raw.encode()), dict), "corrupt_artifact")
                    remember(remembered_artifacts, raw, saved[key + "_sha256"])
            require(saved["status"] not in {"succeeded", "return-pending"}
                    or (saved["intent"] is not None and saved["evidence"] is not None), "corrupt_stage")
            for attempt in attempts_of.get((row["correlation_id"], stage_id), []):
                require(0 <= attempt["cycle"] <= saved["cycle"] and attempt["ordinal"] >= 1
                        and (attempt["outcome"] in {None, "succeeded", "failed", "return-pending"})
                        and ((attempt["ended"] is None) == (attempt["outcome"] is None)), "corrupt_attempt")
            require(replays_of.get((row["correlation_id"], stage_id), []) == list(range(1, saved["cycle"] + 1)), "corrupt_replay")
    _verified = (remembered_requests, remembered_artifacts)


def validate(c, root):
    """Full O(ledger) validation, run when no trusted attestation covers the file."""
    check_identity(c)
    verify(snapshot(c), root)


LOCK_WAIT_ENV = "LIBRARIAN_INTAKE_LOCK_WAIT_SECONDS"
# Seconds a writer waits for the ledger lock, and for SQLite readers to finish, before
# the retryable ``ledger_busy`` outcome.
LOCK_WAIT_SECONDS = 30.0


def lock_wait(wait=None):
    """The bounded wait: an explicit value, else the environment override, else the default.

    Every source passes the same finite 0-3600 second check, so no value can wait forever.
    """
    configured = wait if wait is not None else os.environ.get(LOCK_WAIT_ENV)
    if configured is None:
        return LOCK_WAIT_SECONDS
    try:
        value = float(configured)
    except (TypeError, ValueError) as error:
        raise IntakeError("invalid_lock_wait") from error
    require(math.isfinite(value) and 0 <= value <= 3600, "invalid_lock_wait")
    return value


def _busy(error):
    """True for SQLite's transient BUSY/LOCKED outcomes, which a retry can resolve."""
    return isinstance(error, sqlite3.OperationalError) and (
        getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))


def _acquire(fd, wait):
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as error:
            if time.monotonic() >= deadline:
                raise LedgerBusy() from error
            time.sleep(random.uniform(0.02, 0.1))


_identity = file_identity


def _take_attestation(fd, root):
    """The identity the previous clean writer left in the lock file, consumed on read.

    The lock file is truncated as soon as this writer holds the lock, so a crash,
    or any commit that does not finish with a fresh attestation, forces the next
    writer to validate in full.
    """
    try:
        raw = os.pread(fd, 4096, 0)
        os.ftruncate(fd, 0)
        value = json.loads(raw)
        if value["version"] == 1 and value["root"] == str(root) and isinstance(value["identity"], list):
            return value["identity"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _attest(fd, root, identity):
    os.pwrite(fd, json.dumps({"version": 1, "root": str(root), "identity": identity}).encode(), 0)


@contextmanager
def locked(root, wait=None):
    root, home, db = locations(root)
    seconds = lock_wait(wait)
    private(home, True)
    private(db)
    lock = home / "writer.lock"
    safe(lock)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    c = None
    clean = False
    entered = None
    try:
        try:
            private(lock)
            _acquire(fd, seconds)
            known = _take_attestation(fd, root)
            c = sqlite3.connect(db.as_uri() + "?mode=rw", uri=True, timeout=seconds)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=FULL")
            # Changes only when another connection commits; read before the identity check
            # so no foreign commit can land unseen between the two.
            foreign = c.execute("PRAGMA data_version").fetchone()[0]
            # Another writer's clean hand-off left its attestation; a file that still matches
            # it was last written by a validated writer, so the O(ledger) pass is skipped.
            if known is None or known != _identity(db):
                validate(c, root)
            entered = _identity(db)
        except sqlite3.Error as error:
            raise (LedgerBusy() if _busy(error) else IntakeError("corrupt_store")) from error
        try:
            # A failure after validation is an unavailable ledger, not a corrupt one.
            yield c
        except sqlite3.Error as error:
            raise (LedgerBusy() if _busy(error) else IntakeError("ledger_unavailable")) from error
        clean = True
    finally:
        try:
            # Recorded while still holding the writer lock, after this process's own commits,
            # and only when no other connection committed since validation.
            if c is not None:
                identity = _identity(db)
                # A block that raised is attested only when the file is exactly as it was when
                # validated (a read-only refusal such as ``unknown_request``): nothing was
                # written, so a partial write can never be handed off as validated.
                if identity is not None and (clean or identity == entered) \
                        and c.execute("PRAGMA data_version").fetchone()[0] == foreign:
                    _attest(fd, root, identity)
        except (OSError, sqlite3.Error):
            pass
        if c is not None:
            c.close()
        os.close(fd)


def summary(c, correlation):
    row = c.execute("SELECT * FROM requests WHERE correlation_id=?", (correlation,)).fetchone()
    require(row is not None, "unknown_request")
    stages = []
    for s in c.execute("SELECT * FROM stages WHERE correlation_id=? ORDER BY ordinal", (correlation,)):
        attempts = [dict(a) for a in c.execute("SELECT * FROM attempts WHERE correlation_id=? AND stage_id=? ORDER BY cycle,ordinal", (correlation, s["stage_id"]))]
        stages.append({"id": s["stage_id"], "status": s["status"], "cycle": s["cycle"],
                       "evidence": decode(s["evidence"].encode()) if s["evidence"] else None,
                       "attempts": attempts})
    return {"correlation_id": correlation, "correction_of": row["correction_of"],
            "request_sha256": row["request_sha256"], "stages": stages,
            "complete": all(s["status"] == "succeeded" for s in stages)}


def stage(root, raw):
    value = decode_request(raw)
    correlation = value["correlation_id"]
    with locked(root) as c:
        prior = c.execute("SELECT raw FROM requests WHERE correlation_id=?", (correlation,)).fetchone()
        if prior:
            require(prior["raw"] == raw, "correlation_conflict")
        else:
            if value["correction_of"]:
                require(c.execute("SELECT 1 FROM requests WHERE correlation_id=?", (value["correction_of"],)).fetchone() is not None, "unknown_correction_parent")
            with c:
                c.execute("INSERT INTO requests VALUES (?,?,?,?,?)", (correlation, value["correction_of"], raw, digest(raw), str(safe(root))))
                c.executemany("INSERT INTO stages(correlation_id,stage_id,ordinal,status) VALUES (?,?,?,'staged')", [(correlation, s["id"], i) for i, s in enumerate(value["stages"])])
        return summary(c, correlation)


def inspect(root, correlation_id):
    with locked(root) as c:
        return summary(c, correlation_id)


def head(root, correlation_id):
    """Summary of the newest request in ``correlation_id``'s correction chain."""
    with locked(root) as c:
        current, seen = correlation_id, {correlation_id}
        require(c.execute("SELECT 1 FROM requests WHERE correlation_id=?", (current,)).fetchone()
                is not None, "unknown_request")
        while True:
            children = [r[0] for r in c.execute(
                "SELECT correlation_id FROM requests WHERE correction_of=?", (current,))]
            if not children:
                return summary(c, current)
            require(len(children) == 1 and children[0] not in seen, "ambiguous_correction")
            current = children[0]
            seen.add(current)


def request(root, correlation_id):
    """The exact staged request, decoded."""
    with locked(root) as c:
        row = c.execute("SELECT raw FROM requests WHERE correlation_id=?", (correlation_id,)).fetchone()
        require(row is not None, "unknown_request")
        return decode_request(row["raw"])


def list_requests(root, state=None):
    with locked(root) as c:
        rows = [summary(c, r[0]) for r in c.execute("SELECT correlation_id FROM requests ORDER BY correlation_id")]
        return [r for r in rows if state is None or any(s["status"] == state for s in r["stages"])]


def relocate(root):
    """Update bound root in intake requests after an explicit host-path relocation."""
    root, home, db = locations(root)
    if not db.exists():
        return {"relocated": False, "updated_requests": 0}
    private(home, True)
    private(db)
    lock = home / "writer.lock"
    safe(lock)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    c = None
    try:
        private(lock)
        fcntl.flock(fd, fcntl.LOCK_EX)
        c = sqlite3.connect(db.as_uri() + "?mode=rw", uri=True)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=FULL")
        require(c.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
                and c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA, "incompatible_store")
        require(c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                and not c.execute("PRAGMA foreign_key_check").fetchall(), "corrupt_store")
        require(not c.execute("SELECT 1 FROM stages WHERE status='running'").fetchone(), "active_stage_during_relocation")
        with c:
            cursor = c.execute("UPDATE requests SET root=? WHERE root!=?", (str(root), str(root)))
            updated = cursor.rowcount
        validate(c, root)
        return {"relocated": bool(updated), "updated_requests": updated}
    finally:
        if c is not None:
            c.close()
        os.close(fd)
