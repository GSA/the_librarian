"""E5: measured cost of each integrity mechanism on the 40k-doc synthetic seed (copy). Body-free, synthetic."""
import os, sys, time, shutil, sqlite3, hashlib, json, tempfile, resource
from contextlib import closing
from pathlib import Path
sys.path.insert(0, os.getcwd())
from tools.catalog import catalog_store as cs
from tools.intake import store
seed = Path(sys.argv[1])
def rss(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss >> 10
def t(label, fn, n=1):
    s = time.perf_counter(); r = None
    for _ in range(n): r = fn()
    print(f"{label:62s} {(time.perf_counter()-s)/n*1e3:10.1f} ms   (maxrss {rss()} MB)"); return r
with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as d:
    root = Path(d)/"i"; shutil.copytree(seed, root)
    with sqlite3.connect(root/".local/intake/intake.sqlite3") as c: c.execute("UPDATE requests SET root=?", (str(root.resolve()),))
    home, db, backups = cs.locations(root)
    print("catalog bytes", db.stat().st_size>>20, "MB; ledger", (root/".local/intake/intake.sqlite3").stat().st_size>>20, "MB")
    # catalog
    def full_inspect():
        with closing(cs.connect(db)) as c:
            c.execute("BEGIN"); return cs.inspect(c, detail=False)[0]
    t("catalog full inspect (daily replay / audit / snapshot verify)", full_inspect)
    t("catalog full inspect again (payload memo warm)", full_inspect)
    def quick():
        with closing(cs.connect(db)) as c:
            c.execute("BEGIN"); return [r[0] for r in c.execute("PRAGMA integrity_check")]
    t("catalog PRAGMA integrity_check", quick)
    def qc():
        with closing(cs.connect(db)) as c:
            c.execute("BEGIN"); return [r[0] for r in c.execute("PRAGMA quick_check")]
    t("catalog PRAGMA quick_check", qc)
    t("catalog status() cold (writes receipt, full inspect)", lambda: cs.status(root, audit=True))
    t("catalog status() trusted by receipt (identity bound)", lambda: cs.status(root), 20)
    def headstate():
        with closing(cs.connect(db)) as c:
            c.execute("BEGIN"); return cs._head_state(c)
    t("catalog _head_state (count(*) current_records)", headstate, 20)
    def chain_only():
        # O(revisions) hash-chain check without payload decode
        with closing(cs.connect(db)) as c:
            prev = ""; n = 0
            for r in c.execute("SELECT * FROM revisions ORDER BY revision"):
                b = dict(r); assert b["previous_hash"] == prev
                assert b["revision_hash"] == cs.fingerprint({k: v for k, v in b.items() if k != "revision_hash"}); prev = b["revision_hash"]; n += 1
            return n
    t("catalog revision-chain only (80k revisions, no payloads)", chain_only)
    def payload_hash_only():
        with closing(cs.connect(db)) as c:
            n = 0
            for pl, ph in c.execute("SELECT payload, payload_hash FROM versions"):
                assert hashlib.sha256(pl.encode()).hexdigest() == ph; n += 1
            return n
    t("catalog payload sha256 of all versions (no JSON decode)", payload_hash_only)
    def serialize_hash():
        with closing(cs.connect(db)) as c:
            c.execute("BEGIN"); return hashlib.sha256(c.serialize()).hexdigest()
    t("catalog serialize+sha256 whole db (snapshot image)", serialize_hash)
    def file_sha():
        h = hashlib.sha256()
        with open(db, "rb") as f:
            for ch in iter(lambda: f.read(1<<20), b""): h.update(ch)
        return h.hexdigest()
    t("catalog sha256 of file (page cache warm)", file_sha)
    def backup_copy():
        dst = Path(d)/"bk.sqlite3"
        with closing(cs.connect(db)) as c, closing(sqlite3.connect(dst)) as tgt:
            c.execute("BEGIN"); c.backup(tgt)
        fd = os.open(dst, os.O_RDONLY); os.fsync(fd); os.close(fd); dst.unlink()
    t("catalog sqlite backup() copy + fsync (snapshot)", backup_copy)
    # ledger
    def ledger_validate():
        store.forget_verified()
        with closing(sqlite3.connect(root/".local/intake/intake.sqlite3")) as c:
            c.row_factory = sqlite3.Row; store.validate(c, root.resolve())
    t("ledger full validate (integrity_check+FK+digests+shape) cold memo", ledger_validate)
    t("ledger full validate again (memo warm)", ledger_validate)
    def ledger_ic():
        with closing(sqlite3.connect(root/".local/intake/intake.sqlite3")) as c:
            return c.execute("PRAGMA integrity_check").fetchall(), c.execute("PRAGMA foreign_key_check").fetchall()
    t("ledger PRAGMA integrity_check + foreign_key_check", ledger_ic)
    def ledger_sha():
        h = hashlib.sha256()
        with open(root/".local/intake/intake.sqlite3", "rb") as f:
            for ch in iter(lambda: f.read(1<<20), b""): h.update(ch)
    t("ledger sha256 of file (warm)", ledger_sha)
    def ledger_groupby():
        with closing(sqlite3.connect(f"file:{root}/.local/intake/intake.sqlite3?mode=ro", uri=True)) as c:
            return c.execute("SELECT stage_id, status, count(*) FROM stages GROUP BY 1,2").fetchall()
    t("ledger GROUP BY stage,status (owner: 0.05 s)", ledger_groupby)
