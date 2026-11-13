"""E3: time referrers/claimants alone at several catalog sizes, and the EXPLAIN QUERY PLAN."""
import os, sys, time, shutil, sqlite3, json
sys.path.insert(0, os.getcwd())
from contextlib import closing
from pathlib import Path
from tools.catalog import catalog_store as cs
for name, docs in (("seed2000", 2000), ("seed10000", 10000), ("seed20000", 20000), ("seed40k", 40000)):
    root = Path(os.environ["TMPDIR"]) / name
    home, db, _ = cs.locations(root)
    with closing(cs.connect(db)) as c:
        c.execute("BEGIN"); cur = cs._CurrentRecords(c)
        paths = [f"sources/uploads/new/x{i}/note.md" for i in range(2)]
        t = time.perf_counter(); cur.claimants(paths); a = time.perf_counter() - t
        t = time.perf_counter(); cur.referrers(["A-upload-5-original"]); b = time.perf_counter() - t
        t = time.perf_counter(); cur.get("D7"); g = time.perf_counter() - t
        n = c.execute("select count(*) from current_records").fetchone()[0]
        print(f"{docs:6d} docs {n:7d} current records: claimants {a*1e3:7.1f} ms  referrers {b*1e3:7.1f} ms  get {g*1e3:.2f} ms")
        if docs == 40000:
            for row in c.execute("EXPLAIN QUERY PLAN " + cs._CurrentRecords.SQL + "instr(v.payload,?)>0", ('"x"',)): print(tuple(row))
