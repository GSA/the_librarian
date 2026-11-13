"""E9: both catalog commits per doc (registration 4 records, then extract-registration rewriting the original + child + relation)
at batch size B on the 40k-doc seed, with expression indexes (P3) vs today's scans. Real catalog.submit incl. validate_delta."""
import os, sys, time, shutil, tempfile, hashlib, copy
from pathlib import Path
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.getcwd()+"/.local/plans"); sys.path.insert(0, os.getcwd()+"/.local/plans/proto")
import shim, p1_index_patch as P
seed, expr, B, docs = sys.argv[1], sys.argv[2] == "1", int(sys.argv[3]), int(sys.argv[4])
from tools.catalog import catalog_store as cs
from tools.diagnostics.catalog_lock_benchmark import registration
with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as d:
    root = Path(d)/"i"; shutil.copytree(seed, root)
    if expr: P.build_expr(root/".local/catalog/catalog.sqlite3"); P.patch_expr()
    cs.status(root, audit=True)
    base = 2_000_000
    def rendition_recs(n):
        recs = registration(n); orig = copy.deepcopy(recs[2]); sha = hashlib.sha256(f"r{n}".encode()).hexdigest()
        ref = {"path": f"data/x/D{n}/{sha}/e.json", "sha256": sha, "line_start": None, "line_end": None}
        orig["data"]["provenance"].append(ref); orig["data"]["extraction"] = "existing-rendition"; orig["data"]["coverage"] = "deferred"
        child = {"id": "A-extract-" + sha, "kind": "artifact", "data": {"path": ref["path"], "parent_id": orig["id"], "receipt_id": f"D{n}", "role": "generated-rendition",
                 "audience": "source", "coverage": "deferred", "reason": "r", "presence": "present", "sha256": sha, "hash_basis": "observed", "bytes": 5, "extraction": "not-required",
                 "handling_refs": [], "provenance": [ref, ref]}}
        rel = {"id": "R-extract-" + sha, "kind": "relationship", "data": {"from_id": child["id"], "to_id": orig["id"], "relation": "rendition-of", "scope": "s", "basis": "reported", "provenance": [ref]}}
        return [orig, child, rel]
    c0 = shim.counts(); t0 = time.perf_counter(); n = 0
    while n < docs:
        ids = range(base+n, base+n+B)
        st = cs.status(root)["state_token"]
        cs.submit(root, [r for i in ids for r in registration(i)], actor="b", request_id=f"A{B}-{n}", expected_state=st, reason="x")
        st = cs.status(root)["state_token"]
        cs.submit(root, [r for i in ids for r in rendition_recs(i)], actor="b", request_id=f"B{B}-{n}", expected_state=st, reason="x")
        n += B
    w = time.perf_counter()-t0; c1 = shim.counts()
    print(f"expr={expr} batch={B:3d} docs={docs}: {w/docs*1e3:7.2f} ms/doc catalog (2 commits of B docs)  fdatasync/doc {(c1['fdatasync']-c0['fdatasync'])/docs:.2f}  -> 40k docs = {w/docs*40000/60:.1f} min")
