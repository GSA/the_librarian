"""P3: SQLite expression indexes (no side tables, nothing to keep in sync) for the two substring scans in
catalog_store._CurrentRecords.referrers/claimants. Measures size, build time, lookup time and incremental insert overhead."""
import os, sys, time, shutil, sqlite3, tempfile, json
from pathlib import Path
sys.path.insert(0, os.getcwd())
seed = Path(sys.argv[1])
with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as d:
    db = Path(d)/"c.sqlite3"; shutil.copy(seed/".local/catalog/catalog.sqlite3", db)
    c = sqlite3.connect(db, isolation_level=None); before = db.stat().st_size
    ex = {"path": "json_extract(payload,'$.data.path')", "receipt_id": "json_extract(payload,'$.data.receipt_id')",
          "parent_id": "json_extract(payload,'$.data.parent_id')", "artifact_id": "json_extract(payload,'$.data.artifact_id')",
          "from_id": "json_extract(payload,'$.data.from_id')", "to_id": "json_extract(payload,'$.data.to_id')"}
    t = time.perf_counter()
    for k, e in ex.items(): c.execute(f"CREATE INDEX versions_{k} ON versions({e})")
    print("build 6 expression indexes: %.1fs, +%d MB (db %d -> %d MB)" % (time.perf_counter()-t, (db.stat().st_size-before)>>20, before>>20, db.stat().st_size>>20))
    # lookup: claimants for paths among current records
    t = time.perf_counter()
    for _ in range(50):
        rows = c.execute("SELECT v.payload FROM versions v WHERE json_extract(v.payload,'$.data.path') IN (?,?) AND EXISTS(SELECT 1 FROM current_records c WHERE c.revision=v.revision AND c.ordinal=v.ordinal)", ("sources/uploads/x/a.md","sources/uploads/y/b.md")).fetchall()
    print("claimants (expr index): %.2f ms" % ((time.perf_counter()-t)/50*1e3))
    t = time.perf_counter()
    for _ in range(50):
        rows = c.execute("SELECT v.payload FROM versions v WHERE (json_extract(v.payload,'$.data.receipt_id')=? OR json_extract(v.payload,'$.data.parent_id')=? OR json_extract(v.payload,'$.data.artifact_id')=? OR json_extract(v.payload,'$.data.from_id')=? OR json_extract(v.payload,'$.data.to_id')=?) AND EXISTS(SELECT 1 FROM current_records c WHERE c.revision=v.revision AND c.ordinal=v.ordinal)", ("D77",)*5).fetchall()
    print("referrers (5 expr indexes, OR): %.2f ms  rows=%d" % ((time.perf_counter()-t)/50*1e3, len(rows)))
    print(c.execute("EXPLAIN QUERY PLAN SELECT v.payload FROM versions v JOIN current_records c ON c.revision=v.revision AND c.ordinal=v.ordinal WHERE json_extract(v.payload,'$.data.receipt_id')=? OR json_extract(v.payload,'$.data.parent_id')=?", ("D1","D1")).fetchall())
    # insert overhead of 6 indexes
    from tools.diagnostics.catalog_lock_benchmark import registration
    from tools.catalog.contracts import canonical, fingerprint
    def ins(conn, base, n):
        t = time.perf_counter(); conn.execute("BEGIN")
        for i in range(n):
            for o, r in enumerate(registration(base+i)):
                conn.execute("INSERT INTO versions VALUES (?,?,?,?,?)", (10**6+base+i, o, r["id"], canonical(r), fingerprint(r)))
        conn.execute("COMMIT"); return (time.perf_counter()-t)/n*1e3
    print("insert 4 versions/doc with 6 expr indexes: %.2f ms/doc" % ins(c, 0, 500))
    for k in ex: c.execute(f"DROP INDEX versions_{k}")
    print("insert 4 versions/doc without:             %.2f ms/doc" % ins(c, 5000, 500))
