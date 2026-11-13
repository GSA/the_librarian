"""P2 prototype ("target design" ingest): ONE SQLite file (WAL, synchronous=FULL), a per-document table with stat
identity + sha256 + state, renditions as zlib blobs, N parser processes, one writer committing batches of 100.
Reuses the real extractors (extract.text / mime_html / gemini) so parse cost is real. No real data; synthetic inbox."""
import hashlib, json, multiprocessing as mp, os, sqlite3, sys, tempfile, time, zlib
from pathlib import Path
sys.path.insert(0, os.getcwd())
from tools.extraction import extract, mhtml

DDL = """
CREATE TABLE docs(path TEXT PRIMARY KEY, dev INT, ino INT, size INT, mtime_ns INT, ctime_ns INT, sha256 TEXT,
  format TEXT, disposition TEXT, rendition_sha TEXT, state TEXT, state_at REAL, seen_run INT);
CREATE INDEX docs_state ON docs(state);
CREATE TABLE renditions(sha TEXT PRIMARY KEY, extractor TEXT, body BLOB NOT NULL, raw_bytes INT);
CREATE TABLE runs(id INTEGER PRIMARY KEY, started REAL, ended REAL, files INT, changed INT, hashed INT, skipped INT);
"""
TEXT = extract.TEXT_SUFFIXES

def parse(job):
    path, rel, root = job
    raw = Path(path).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    suf = Path(path).suffix.lower()
    try:
        if suf in TEXT and mhtml.recognised(raw): fmt, payload = "mhtml", mhtml.payload(raw)
        elif suf == ".txt":
            try: fmt, payload = "gemini-txt", extract.gemini(raw, root)
            except (extract.ExtractionError, UnicodeDecodeError): fmt, payload = "text", extract.text(raw)
        else: fmt, payload = "text", extract.text(raw)
    except Exception as e:
        return rel, sha, None, None, None, "unreadable"
    body = extract.encoded({"source": {"path": rel, "sha256": sha, "bytes": len(raw)}, "payload": payload})
    return rel, sha, fmt, hashlib.sha256(body).hexdigest(), zlib.compress(body, 3), len(body)

def scan(inbox):
    for d, _, files in os.walk(inbox):
        for n in files:
            p = os.path.join(d, n); st = os.lstat(p)
            yield p, os.path.relpath(p, inbox), st

def ingest(db, inbox, workers, batch=100, trust_stat=True, run=1, root="/nonexistent"):
    c = sqlite3.connect(db, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=FULL")
    if not c.execute("SELECT name FROM sqlite_master WHERE name='docs'").fetchone(): c.executescript(DDL)
    known = {r[0]: r[1:] for r in c.execute("SELECT path,dev,ino,size,mtime_ns,ctime_ns,sha256 FROM docs")}
    t0 = time.perf_counter(); jobs, unchanged, stats = [], [], {}
    for p, rel, st in scan(inbox):
        k = known.get(rel); sig = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        stats[rel] = sig
        if trust_stat and k and tuple(k[:5]) == sig: unchanged.append(rel)
        else: jobs.append((p, rel, root))
    t_scan = time.perf_counter()-t0
    c.execute("BEGIN"); c.executemany("UPDATE docs SET seen_run=? WHERE path=?", [(run, r) for r in unchanged]); c.execute("COMMIT")
    t1 = time.perf_counter(); done = 0; hashed = 0; changed = 0
    pool = mp.Pool(workers) if workers > 1 else None
    it = pool.imap_unordered(parse, jobs, chunksize=8) if pool else map(parse, jobs)
    c.execute("BEGIN"); n = 0
    for rel, sha, fmt, rsha, blob, size in it:
        sig = stats[rel]; hashed += 1
        k = known.get(rel)
        if k and k[5] != sha: changed += 1
        if blob is None:
            c.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (rel, *sig, sha, None, "unreadable", None, "skipped", time.time(), run))
        else:
            c.execute("INSERT OR IGNORE INTO renditions VALUES (?,?,?,?)", (rsha, "v1", blob, size))
            c.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (rel, *sig, sha, fmt, "admit", rsha, "extracted", time.time(), run))
        n += 1
        if n % batch == 0: c.execute("COMMIT"); c.execute("BEGIN")
    c.execute("COMMIT")
    if pool: pool.close(); pool.join()
    t_ing = time.perf_counter()-t1
    c.execute("INSERT INTO runs VALUES (NULL,?,?,?,?,?,?)", (t0, time.time(), len(stats), changed, hashed, len(unchanged)))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"files": len(stats), "stat_scan_s": round(t_scan,2), "hashed_parsed": hashed, "stat_skipped": len(unchanged), "changed": changed, "parse_write_s": round(t_ing,1)}

if __name__ == "__main__":
    inbox, workers, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 0
    work = Path(tempfile.mkdtemp(dir=os.environ["TMPDIR"])); db = work/"docs.sqlite3"
    sub = inbox
    t = time.perf_counter(); r1 = ingest(str(db), sub, workers, run=1); w1 = time.perf_counter()-t
    t = time.perf_counter(); r2 = ingest(str(db), sub, workers, run=2); w2 = time.perf_counter()-t
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    t = time.perf_counter(); st = c.execute("SELECT state,count(*) FROM docs GROUP BY 1").fetchall(); q = time.perf_counter()-t
    left = c.execute("SELECT count(*) FROM docs WHERE state='extracted'").fetchone()[0]
    print(json.dumps({"workers": workers, "first": r1, "first_wall_s": round(w1,1), "replay": r2, "replay_wall_s": round(w2,2), "status_query_ms": round(q*1e3,1),
        "db_MB": db.stat().st_size>>20, "rendition_raw_MB": c.execute("select sum(raw_bytes) from renditions").fetchone()[0]>>20,
        "rendition_zlib_MB": c.execute("select sum(length(body)) from renditions").fetchone()[0]>>20, "states": st}))
    import shutil; shutil.rmtree(work)
