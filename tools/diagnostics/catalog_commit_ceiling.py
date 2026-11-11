#!/usr/bin/env python3
"""Synthetic catalog commit ceiling: registrations per second with N concurrent writer processes.

Seeds a temporary catalog with ``--size`` registrations, then ``--workers`` processes each commit
``--per-worker`` further registrations through ``catalog_store.submit(..., rebase=True)``, pinned at
the state they last read (so lost races rebase and ``WriterBusy`` retries, as intake does).
This is the throughput ceiling the catalog commit imposes on any number of intake workers, free of
extraction and ledger work. Optional ``--batch`` commits that many registrations per revision.
Body-free JSON; synthetic fixtures only.

    python3 tools/diagnostics/catalog_commit_ceiling.py --size 1600 --workers 1,2,4 --per-worker 12
"""
import argparse
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time


def worker(tree, root, number, count, batch, start_at, queue):
    sys.path.insert(0, tree)
    from tools.catalog import catalog_store as cs
    from tools.diagnostics.catalog_lock_benchmark import registration
    root = Path(root)
    while time.time() < start_at:
        time.sleep(0.001)
    done, retries = 0, 0
    t0 = time.perf_counter()
    for index in range(0, count, batch):
        records = [r for k in range(index, min(index + batch, count))
                   for r in registration(1_000_000 + number * 10_000 + k)]
        request = f"w{number}-{index}"
        while True:
            try:
                pinned = cs.status(root)["state_token"]
                cs.submit(root, records, actor="bench", request_id=request, expected_state=pinned,
                          reason="ceiling", rebase=True)
                break
            except (cs.StaleState, cs.WriterBusy):
                retries += 1
        done += min(batch, count - index)
    queue.put((done, retries, time.perf_counter() - t0))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--size", type=int, default=1600)
    parser.add_argument("--workers", default="1,2,4")
    parser.add_argument("--per-worker", type=int, default=12)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    tree = str(args.tree.resolve())
    sys.path.insert(0, tree)
    os.chdir(tree)
    from tools.diagnostics.catalog_lock_benchmark import seed
    from tools.catalog import catalog_store as cs
    from tools.intake import store
    results = []
    for workers in (int(w) for w in args.workers.split(",")):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "instance"
            root.mkdir()
            store.initialize(root)
            cs.initialize(root)
            seed(root, args.size)
            cs.status(root)
            queue = multiprocessing.Queue()
            start_at = time.time() + 1.0
            procs = [multiprocessing.Process(target=worker, args=(
                tree, str(root), n, args.per_worker, args.batch, start_at, queue)) for n in range(workers)]
            for proc in procs:
                proc.start()
            reports = [queue.get() for _ in procs]
            for proc in procs:
                proc.join()
            wall = max(r[2] for r in reports)
            total = sum(r[0] for r in reports)
            results.append({"workers": workers, "batch": args.batch, "registrations": total,
                            "wall_s": round(wall, 2), "registrations_per_s": round(total / wall, 2),
                            "retries": sum(r[1] for r in reports),
                            "final_revision": cs.status(root, audit=True)["revision"]})
            print(json.dumps(results[-1], sort_keys=True), file=sys.stderr, flush=True)
    print(json.dumps({"label": args.label, "size": args.size, "results": results}, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
