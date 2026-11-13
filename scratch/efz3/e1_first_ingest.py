#!/usr/bin/env python3
"""E1: cost model of FIRST ingest per document on current code, at production-ish seed.

Seeds a ledger/catalog (matched uploads), then ingests --files NEW synthetic docs
with cProfile, fsync/write counters (LD_PRELOAD shim) and /proc io. Synthetic only."""
import argparse, cProfile, io, json, os, pstats, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.getcwd() + "/.local/plans"); sys.path.insert(0, os.getcwd() + "/.local/plans/proto")
import shim

def rw():
    d = dict(l.split(": ") for l in open("/proc/self/io").read().splitlines())
    return int(d["rchar"]), int(d["wchar"]), int(d["write_bytes"]), int(d["syscw"])

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ledger", type=int, default=120000); p.add_argument("--catalog", type=int, default=38000)
    p.add_argument("--files", type=int, default=100); p.add_argument("--padding", type=int, default=1800)
    p.add_argument("--profile", action="store_true"); p.add_argument("--work", default=None)
    p.add_argument("--mixed", default="mixed"); p.add_argument("--seed", default=None)
    a = p.parse_args()
    from tools.catalog import catalog_store as catalog
    from tools.diagnostics.catalog_lock_benchmark import seed
    from tools.diagnostics.ingest_benchmark import fixture
    from tools.diagnostics.ledger_scale_benchmark import seed_uploads
    from tools.intake import cohort, store
    with tempfile.TemporaryDirectory(dir=a.work) as t:
        patched = os.environ.get("PATCH") == "1"
        t = Path(t); root, inbox = t / "instance", t / "inbox"
        t0 = time.time()
        if a.seed:
            import shutil, sqlite3; shutil.copytree(a.seed, root)
            with sqlite3.connect(root/".local/intake/intake.sqlite3") as con: con.execute("UPDATE requests SET root=?", (str(root.resolve()),))
        else:
            root.mkdir(); store.initialize(root); catalog.initialize(root)
        if a.catalog and not a.seed:
            seed(root, a.catalog); seed_uploads(root, a.catalog, a.ledger)
        print("seeded in %.1fs ledger=%dMB catalog=%dMB" % (time.time() - t0,
              (root/".local/intake/intake.sqlite3").stat().st_size >> 20,
              (root/".local/catalog/catalog.sqlite3").stat().st_size >> 20), file=sys.stderr)
        if patched:
            import p1_index_patch as P; P.build(root/".local/catalog/catalog.sqlite3"); P.patch()
        fixture(inbox, a.files, seed="e1", padding=a.padding, mixed=a.mixed)
        catalog.status(root, audit=True)  # first-of-day replay not counted
        shim.log_open('/home/nkirrish/.cache/efz3-scratch/sync.log')
        c0, r0, t0, pr = shim.counts(), rw(), time.perf_counter(), time.process_time()
        pf = cProfile.Profile() if a.profile else None
        if pf: pf.enable()
        rep = cohort.ingest(root, inbox, system="bench", actor="bench")
        if pf: pf.disable()
        wall, cpu = time.perf_counter() - t0, time.process_time() - pr
        c1, r1 = shim.counts(), rw(); shim.log_close()
        n = a.files
        out = {"files": n, "outcome": rep["counts"]["outcome"], "wall_ms_per_doc": round(wall/n*1e3, 1),
               "cpu_ms_per_doc": round(cpu/n*1e3, 1),
               "fsync_per_doc": round((c1["fsync"]-c0["fsync"])/n, 1),
               "fdatasync_per_doc": round((c1["fdatasync"]-c0["fdatasync"])/n, 1),
               "write_syscalls_per_doc": round(((c1["write"]-c0["write"])+(c1["pwrite"]-c0["pwrite"]))/n, 1),
               "KiB_written_per_doc": round((r1[2]-r0[2])/n/1024, 1), "KiB_read_per_doc": round((r1[0]-r0[0])/n/1024, 1),
               "ledger_MB_after": (root/".local/intake/intake.sqlite3").stat().st_size >> 20,
               "catalog_MB_after": (root/".local/catalog/catalog.sqlite3").stat().st_size >> 20,
               "inbox_KiB_per_doc": sum(f.stat().st_size for f in inbox.rglob("*") if f.is_file())//n//1024}
        # files and dirs created per doc
        out["files_created_per_doc"] = round(sum(1 for _ in root.rglob("*") if _.is_file()) / (n) , 1)
        print(json.dumps(out, sort_keys=True))
        if pf:
            s = io.StringIO(); ps = pstats.Stats(pf, stream=s).sort_stats("cumulative"); ps.print_stats(45); print(s.getvalue()[:9000])
            s = io.StringIO(); ps = pstats.Stats(pf, stream=s).sort_stats("tottime"); ps.print_stats(25); print(s.getvalue()[:6000])
main()
