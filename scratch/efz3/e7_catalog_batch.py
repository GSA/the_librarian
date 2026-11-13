"""E7: catalog.submit cost per document at batch sizes 1/25/100 on a 10k-doc seeded catalog (copy), with and without the
side-table lookup patch. Uses registration(n) records (4 per doc) with fresh ids. fsyncs via the LD_PRELOAD shim."""
import os, sys, time, shutil, tempfile, statistics
from pathlib import Path
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.getcwd()+"/.local/plans"); sys.path.insert(0, os.getcwd()+"/.local/plans/proto")
import shim
seed, patched, docs = sys.argv[1], sys.argv[2] == "1", int(sys.argv[3])
from tools.catalog import catalog_store as cs
from tools.diagnostics.catalog_lock_benchmark import registration
if patched:
    pass
for batch in (1, 25, 100):
    with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as d:
        root = Path(d)/"i"; shutil.copytree(seed, root)
        if patched:
            import p1_index_patch as P; (P.build_expr(root/".local/catalog/catalog.sqlite3"), P.patch_expr()) if os.environ.get("EXPR")=="1" else (P.build(root/".local/catalog/catalog.sqlite3"), P.patch())
        cs.status(root, audit=True)
        base = 900000 + batch*1000
        c0 = shim.counts(); t0 = time.perf_counter(); n = 0; hold = []
        while n < docs:
            recs = [r for k in range(batch) for r in registration(base + n + k)]
            state = cs.status(root)["state_token"]
            cs.submit(root, recs, actor="b", request_id=f"batch-{batch}-{n}", expected_state=state, reason="x")
            n += batch
        wall = time.perf_counter()-t0; c1 = shim.counts()
        print(f"patched={patched} batch={batch:3d}: {wall/n*1e3:7.1f} ms/doc  fsync {((c1['fsync']-c0['fsync'])/n):5.2f}/doc fdatasync {((c1['fdatasync']-c0['fdatasync'])/n):5.2f}/doc  revisions added {n//batch}")
