"""E4: steady-state first-ingest cost at a seeded size. Warm-up ingest (not timed) pays the one-off ledger validation
and the daily catalog replay; then N new files are timed with cProfile and bucketed into cost categories."""
import cProfile, collections, io, os, pstats, sys, tempfile, time, shutil, sqlite3, json
from pathlib import Path
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.getcwd()+"/.local/plans"); sys.path.insert(0, os.getcwd()+"/.local/plans/proto")
import shim
seed, warm, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
patched = os.environ.get("PATCH") == "1"
from tools.diagnostics.ingest_benchmark import fixture
from tools.intake import cohort
from tools.catalog import catalog_store as catalog
BUCKETS = [  # (label, substring of "file:func" that attributes cumulative time, exclusive of nested buckets handled by tottime split)
 ("sqlite-exec+commit", ["method 'execute' of 'sqlite3", "method '__exit__' of 'sqlite3.Connection", "method 'commit'", "method 'executemany'", "method 'fetchall'", "method 'fetchone'", "method 'executescript'"]),
 ("fsync (python)", ["posix.fsync"]),
]
with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as t:
    t = Path(t); root = t/"instance"; shutil.copytree(seed, root)
    with sqlite3.connect(root/".local/intake/intake.sqlite3") as c: c.execute("UPDATE requests SET root=?", (str(root.resolve()),))
    if patched:
        import p1_index_patch as P; (P.build_expr(root/".local/catalog/catalog.sqlite3"), P.patch_expr()) if os.environ.get("EXPR")=="1" else (P.build(root/".local/catalog/catalog.sqlite3"), P.patch())
    catalog.status(root, audit=True)
    fixture(t/"w", warm, seed="warm", padding=1800, mixed="mixed"); cohort.ingest(root, t/"w", system="warm", actor="b")
    fixture(t/"inbox", n, seed="steady", padding=1800, mixed="mixed")
    shim.log_open(None)
    c0 = shim.counts(); t0 = time.perf_counter(); cp = time.process_time()
    pr = cProfile.Profile(); pr.enable()
    rep = cohort.ingest(root, t/"inbox", system="bench", actor="b")
    pr.disable(); wall = time.perf_counter()-t0; cpu = time.process_time()-cp; c1 = shim.counts()
    # unprofiled wall for the same shape (second batch)
    fixture(t/"inbox2", n, seed="steady2", padding=1800, mixed="mixed")
    t1 = time.perf_counter(); cp1 = time.process_time(); cohort.ingest(root, t/"inbox2", system="bench2", actor="b"); wall2 = time.perf_counter()-t1; cpu2=time.process_time()-cp1
st = pstats.Stats(pr); tot = collections.Counter()
for (fn, ln, name), (cc, nc, tt, ct, callers) in st.stats.items():
    key = f"{Path(fn).name}:{name}"
    for label, pats in BUCKETS:
        if any(p in name or p in fn+":"+name for p in pats): tot[label] += tt
    if "extract.py" in fn and name in ("text",): tot["extract.text()"] += tt
    if "mhtml.py" in fn or "html/parser" in fn: tot["mhtml/html parse"] += tt
    if "json/" in fn: tot["json enc/dec"] += tt
    if "hashlib" in name or "openssl" in name: tot["sha256"] += tt
print(json.dumps({"seed": os.path.basename(seed), "patched": patched, "files": n, "outcome": rep["counts"]["outcome"]}))
print("profiled wall ms/doc", round(wall/n*1e3), " unprofiled wall ms/doc", round(wall2/n*1e3), " unprofiled cpu ms/doc", round(cpu2/n*1e3))
print("fsync/doc", round((c1["fsync"]-c0["fsync"])/n,1), "fdatasync/doc", round((c1["fdatasync"]-c0["fdatasync"])/n,1))
for k, v in tot.most_common(): print(f"  {v/n*1e3:7.1f} ms/doc  {k}")
# top inclusive project functions
rows = []
for (fn, ln, name), (cc, nc, tt, ct, callers) in st.stats.items():
    if "/tools/" in fn and nc: rows.append((ct/n*1e3, nc/n, f"{fn.split('/tools/')[-1]}:{name}"))
for ct, nc, k in sorted(rows, reverse=True)[:22]: print(f"  incl {ct:7.1f} ms/doc {nc:6.1f} calls/doc {k}")
