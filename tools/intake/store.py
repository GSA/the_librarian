"""Private transactional intake intent and recovery ledger."""
from contextlib import closing, contextmanager
import fcntl
import functools
import os
from pathlib import Path
import sqlite3
import stat

from tools import policy
from .contracts import IntakeError, canonical, decode, decode_request, digest, request_shape, require

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


@functools.lru_cache(maxsize=32768)
def _digest_matches(raw, sha256):
    return digest(raw) == sha256


@functools.lru_cache(maxsize=65536)
def _saved_artifact(raw, sha256):
    """True when this exact saved artifact text matches its digest and is a JSON object.

    Both checks are pure functions of the arguments, so revalidating an unchanged
    ledger on every lock never re-hashes or re-decodes the same bytes.
    """
    return digest(raw.encode()) == sha256 and isinstance(decode(raw.encode()), dict)


def validate(c, root):
    require(c.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
            and c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA, "incompatible_store")
    require(c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            and not c.execute("PRAGMA foreign_key_check").fetchall(), "corrupt_store")
    # One query per table (not per request and stage); checks run in the same order.
    stages_of, attempts_of, replays_of = {}, {}, {}
    for saved in c.execute("SELECT * FROM stages ORDER BY correlation_id, ordinal"):
        stages_of.setdefault(saved["correlation_id"], []).append(saved)
    for attempt in c.execute("SELECT * FROM attempts ORDER BY correlation_id, stage_id, cycle, ordinal"):
        attempts_of.setdefault((attempt["correlation_id"], attempt["stage_id"]), []).append(attempt)
    for replay in c.execute("SELECT correlation_id, stage_id, cycle FROM replays ORDER BY correlation_id, stage_id, cycle"):
        replays_of.setdefault((replay[0], replay[1]), []).append(replay[2])
    for row in c.execute("SELECT * FROM requests"):
        correlation_id, correction_of, stage_ids = request_shape(row["raw"])
        require(_digest_matches(row["raw"], row["request_sha256"]) and row["root"] == str(root)
                and correlation_id == row["correlation_id"]
                and correction_of == row["correction_of"], "corrupt_request")
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
                    require(_saved_artifact(raw, saved[key + "_sha256"]), "corrupt_artifact")
            require(saved["status"] not in {"succeeded", "return-pending"}
                    or (saved["intent"] is not None and saved["evidence"] is not None), "corrupt_stage")
            for attempt in attempts_of.get((row["correlation_id"], stage_id), []):
                require(0 <= attempt["cycle"] <= saved["cycle"] and attempt["ordinal"] >= 1
                        and (attempt["outcome"] in {None, "succeeded", "failed", "return-pending"})
                        and ((attempt["ended"] is None) == (attempt["outcome"] is None)), "corrupt_attempt")
            require(replays_of.get((row["correlation_id"], stage_id), []) == list(range(1, saved["cycle"] + 1)), "corrupt_replay")


# Ledger files this process fully validated, keyed by (db, root), mapped to the file
# identity it left behind on unlock. An unchanged identity at the next lock means no
# other writer touched the file, so the full O(ledger) validation is skipped.
_validated = {}


def _identity(db):
    """File identity plus SQLite's header file change counter, which every commit
    increments in rollback-journal mode; ``None`` when a journal is present."""
    if os.path.lexists(f"{db}-journal") or os.path.lexists(f"{db}-wal"):
        return None
    fd = os.open(db, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        header = os.pread(fd, 100, 0)
    finally:
        os.close(fd)
    if len(header) < 100 or header[18:20] != b"\x01\x01":
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, header[24:28])


@contextmanager
def locked(root):
    root, home, db = locations(root)
    key = (str(db), str(root))
    private(home, True)
    private(db)
    lock = home / "writer.lock"
    safe(lock)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    c = None
    clean = False
    try:
        try:
            private(lock)
            fcntl.flock(fd, fcntl.LOCK_EX)
            known = _validated.pop(key, None)
            c = sqlite3.connect(db.as_uri() + "?mode=rw", uri=True)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=FULL")
            if known is None or known != _identity(db):
                validate(c, root)
        except sqlite3.Error as error:
            raise IntakeError("corrupt_store") from error
        try:
            # A failure after validation is an unavailable ledger, not a corrupt one.
            yield c
        except sqlite3.Error as error:
            raise IntakeError("ledger_unavailable") from error
        clean = True
    finally:
        if c is not None:
            c.close()
        try:
            # Recorded while still holding the writer lock, after this process's own commits.
            identity = _identity(db) if clean else None
        except OSError:
            identity = None
        if identity is not None:
            _validated[key] = identity
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
