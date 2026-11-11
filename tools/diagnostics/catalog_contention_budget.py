#!/usr/bin/env python3
"""Synthetic stage-level catalog contention: what a long catalog writer hold does to an intake stage.

A registration stage (``max_attempts`` 3, as upload and cohort stage them) runs through
``runner.settle`` while the catalog writer lock is contended. The catalog's 10 s ``WriterBusy`` wait
is scaled down (``--wait``) so a run takes seconds; hold times are reported as multiples of that
wait, so the ratios carry over to the real 10 s wait. Reports are body-free: final stage status,
attempt outcome codes and wall time.

Another process holds the lock for each ``--holds`` multiple of the wait (a restore, a manual
backup, a first-of-day snapshot of a large catalog, a slow disk). Intake processes themselves never
contend on the catalog: ``runner.run`` holds the ledger lock across the stage's catalog commit.

    python3 tools/diagnostics/catalog_contention_budget.py --wait 0.5 --holds 0.5,2.5,3.5,6.5
    python3 tools/diagnostics/catalog_contention_budget.py --tree /path/to/older/checkout

Synthetic fixtures in a temporary workspace only; never point it at a real instance.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HOLDER = ("import fcntl,os,sys,time\nfd=os.open(sys.argv[1],os.O_RDWR)\nfcntl.flock(fd,fcntl.LOCK_EX)\n"
          "print('held',flush=True)\ntime.sleep(float(sys.argv[2]))\n")


def trial(wait, multiple, max_attempts):
    from tools import policy
    from tools.catalog import catalog_store as catalog
    from tools.intake import runner, store
    catalog.WRITER_WAIT_SECONDS = wait
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "instance"
        root.mkdir()
        store.initialize(root)
        state = catalog.initialize(root)["state"]
        record = {"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}}
        store.stage(root, json.dumps({
            "version": 1, "correlation_id": "contended", "correction_of": None,
            "policy": {"version": 1, "sha256": policy.for_root(root)[1]},
            "retry": {"max_attempts": max_attempts, "backoff_seconds": 0},
            "stages": [{"id": "receipt", "kind": "receipt", "operation": "catalog-submit", "arguments": {
                "records": [record], "actor": "bench", "request_id": "contended", "reason": "Synthetic.",
                "expected_state": state["state_token"]}}]}).encode())
        home = catalog.locations(root)[0]
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(home / "writer.lock"), str(multiple * wait)],
                                  stdout=subprocess.PIPE, text=True)
        try:
            holder.stdout.readline()
            started = time.monotonic()
            result = runner.settle(root, "contended", "receipt")
            wall = time.monotonic() - started
        finally:
            holder.wait()
            holder.stdout.close()
        stage = result["stages"][0]
        codes = [a["code"] or a["outcome"] for a in stage["attempts"]]
        return {"hold_waits": multiple, "status": stage["status"], "attempts": len(codes), "codes": codes,
                "wall_waits": round(wall / wait, 2)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--wait", type=float, default=0.5, help="scaled catalog writer wait in seconds")
    parser.add_argument("--holds", default="0.5,2.5,3.5,6.5", help="hold times as multiples of the wait")
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    tree = str(args.tree.resolve())
    sys.path.insert(0, tree)
    os.chdir(tree)
    print(json.dumps({"wait_s": args.wait, "max_attempts": args.max_attempts,
                      "trials": [trial(args.wait, float(h), args.max_attempts) for h in args.holds.split(",")]},
                     indent=1))


if __name__ == "__main__":
    main()
