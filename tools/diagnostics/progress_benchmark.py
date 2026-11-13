#!/usr/bin/env python3
"""Render time and peak memory of the intake progress view over a synthetic production-sized ledger.

Builds a temporary instance whose ledger holds ``--requests`` single-receipt requests (about 1.05 stages
each, mostly succeeded, some blocked on one repeated code, retryable, dead-lettered or staged; written
directly, so large sizes build quickly) and whose catalog holds ``--catalog`` upload-shaped
registrations, then runs ``progress.snapshot`` in a fresh process per scenario and reports body-free
JSON: wall seconds and peak RSS of ``cold`` (first render in a process), ``idle`` (the same process,
nothing changed) and ``after-commit`` (one ledger commit since the last render). Use ``--tree`` to compare
another checkout's ``tools`` package (a tree without the cache reports ``idle`` equal to ``cold``).
By default a catalog read-validation receipt exists, as it does once any writer or validated reader has run
today; ``--no-receipt`` measures the first render of a UTC day.
Synthetic fixtures only; never point it at a real instance.

    python3 tools/diagnostics/progress_benchmark.py --requests 25000,60000,125000
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time

CHILD = r"""
import json, os, resource, sqlite3, sys, time
sys.path.insert(0, sys.argv[1]); os.chdir(sys.argv[1])
from pathlib import Path
from tools.intake import progress
root = Path(sys.argv[2])
out = {}
for name in ("cold", "idle", "after-commit"):
    if name == "after-commit":
        with sqlite3.connect(root / ".local/intake/intake.sqlite3") as c:
            c.execute("UPDATE attempts SET ended = ended + 1 WHERE rowid = 1")
    start = time.perf_counter()
    report = progress.snapshot(root)
    out[name] = {"wall_s": round(time.perf_counter() - start, 2),
                 "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
                 "requests": report["overall"]["requests"], "exceptions": report["overall"]["exceptions"]}
print(json.dumps(out))
"""


def seed(root, count):
    from tools import policy
    from tools.catalog import catalog_store as catalog
    from tools.diagnostics.catalog_lock_benchmark import registration
    from tools.intake import contracts, store
    store.initialize(root)
    catalog.initialize(root)
    base = {"version": 1, "correction_of": None, "policy": {"version": 1, "sha256": policy.load()[1]},
            "retry": {"max_attempts": 3, "backoff_seconds": 0}}
    now = time.time()
    with closing(sqlite3.connect(root / ".local/intake/intake.sqlite3")) as c, c:
        for k in range(count):
            correlation = f"bulk-{k:07d}"
            stages = [{"id": "receipt", "kind": "receipt", "operation": "catalog-submit", "arguments": {
                "records": registration(k)[1:3], "actor": "bench", "request_id": correlation,
                "expected_state": "0" * 64, "reason": "Manual upload: synthetic"}}]
            if k % 20 == 0:
                stages.append({"id": "extract", "kind": "extraction", "operation": "docling-prepare",
                               "arguments": {"receipt": f"D{k}", "format_name": "pdf",
                                             "artifacts_path": f"x/{k}", "python": "py"}})
            raw = json.dumps({**base, "correlation_id": correlation, "stages": stages}).encode()
            share = k % 100
            status = ("succeeded" if share < 86 else "blocked" if share < 96 else "retryable"
                      if share < 98 else "dead-letter" if share < 99 else "staged")
            c.execute("INSERT INTO requests VALUES (?,?,?,?,?)",
                      (correlation, None, raw, contracts.digest(raw), str(root)))
            for ordinal, stage in enumerate(stages):
                last = ordinal == len(stages) - 1
                current = status if last else "succeeded"
                if last and len(stages) > 1 and status == "succeeded":
                    current = "staged"
                intent = json.dumps({"k": k, "pad": "x" * 400}, sort_keys=True)
                evidence = json.dumps({"k": k, "pad": "y" * 240}, sort_keys=True)
                done = current == "succeeded"
                c.execute("INSERT INTO stages VALUES (?,?,?,?,?,?,?,?,?)", (
                    correlation, stage["id"], ordinal, current, 0, intent, contracts.digest(intent.encode()),
                    evidence if done else None, contracts.digest(evidence.encode()) if done else None))
                if current != "staged":
                    code = {"blocked": "prohibited-scope", "retryable": "native-busy",
                            "dead-letter": "invalid-artifact"}.get(current)
                    started = now - 86400 + k * 0.5
                    c.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?)", (
                        correlation, stage["id"], 0, 1, started, started + 1,
                        "succeeded" if done else "failed", None if done else "permanent", code, None))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2],
                        help="checkout whose tools package is measured")
    parser.add_argument("--requests", default="25000,60000,125000", help="comma-separated ledger sizes")
    parser.add_argument("--catalog", type=int, default=25600, help="catalog registrations to seed")
    parser.add_argument("--no-receipt", action="store_true",
                        help="leave no catalog read-validation receipt (a fresh catalog nobody has read yet)")
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    tree = args.tree.resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    for size in (int(n) for n in args.requests.split(",")):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "instance"
            root.mkdir()
            seed(root, size)
            if args.catalog:
                from tools.diagnostics.catalog_lock_benchmark import seed as seed_catalog
                seed_catalog(root, args.catalog)
                if not args.no_receipt:  # what any validated catalog read or writer leaves behind
                    from tools.catalog import catalog_store as catalog
                    catalog.status(root)
            result = subprocess.run([sys.executable, "-c", CHILD, str(tree), str(root)],
                                    capture_output=True, text=True, check=True)
            print(json.dumps({"label": args.label, "ledger_requests": size,
                              "ledger_bytes": (root / ".local/intake/intake.sqlite3").stat().st_size,
                              **json.loads(result.stdout)}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
