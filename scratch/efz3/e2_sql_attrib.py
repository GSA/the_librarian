"""E2: attribute first-ingest wall time per SQL statement shape and per phase, via sqlite3 trace on connections made by the code."""
import os, sys, time, json, tempfile, shutil, sqlite3, collections, re
from pathlib import Path
sys.path.insert(0, os.getcwd())
seed, files = sys.argv[1], int(sys.argv[2])
from tools.diagnostics.ingest_benchmark import fixture
from tools.intake import cohort
stats = collections.defaultdict(lambda: [0, 0.0])
orig_connect = sqlite3.connect
class Cur:
    pass
def shape(sql):
    s = re.sub(r"\s+", " ", sql.strip())[:110]
    return re.sub(r"\?+", "?", s)
class TConn(sqlite3.Connection):
    def execute(self, sql, *a, **k):
        t = time.perf_counter()
        try:
            cur = super().execute(sql, *a, **k)
            return cur
        finally:
            e = stats[shape(sql)]; e[0] += 1; e[1] += time.perf_counter() - t
sqlite3.connect = lambda *a, **k: orig_connect(*a, **{**k, "factory": TConn}) if "factory" not in k else orig_connect(*a, **k)
with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as t:
    t = Path(t); root, inbox = t / "instance", t / "inbox"
    shutil.copytree(seed, root)
    with orig_connect(root/".local/intake/intake.sqlite3") as con: con.execute("UPDATE requests SET root=?", (str(root.resolve()),))
    fixture(inbox, files, seed="e2", padding=1800, mixed="mixed")
    from tools.catalog import catalog_store as catalog
    catalog.status(root, audit=True)
    t0 = time.perf_counter(); cohort.ingest(root, inbox, system="bench", actor="bench"); wall = time.perf_counter() - t0
print("wall per doc ms", round(wall / files * 1e3, 1))
tot = sum(v[1] for v in stats.values())
print("sql total per doc ms", round(tot / files * 1e3, 1))
for k, (n, s) in sorted(stats.items(), key=lambda kv: -kv[1][1])[:14]:
    print(f"{s/files*1e3:8.1f} ms/doc {n/files:6.1f} calls/doc  {k}")
