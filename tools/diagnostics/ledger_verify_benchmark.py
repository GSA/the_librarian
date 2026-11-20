#!/usr/bin/env python3
"""Cost of the ledger's two checks over a synthetic production-sized intake ledger (Kata qwtp).

Seeds ``--ledger`` completed single-stage requests in a temporary instance, then times in a fresh
connection each pass: ``audit`` (every digest and rule, ``store.audit``) and, once, the writer's open
(``check_identity``: application identity, SQLite integrity, foreign keys) that every writer process
pays. Body-free JSON. Use ``--tree`` to compare another checkout's ``tools`` package (a tree that
predates ``store.audit`` is timed with its ``store.validate``). Synthetic fixtures only; never point
it at a real instance.

    python3 tools/diagnostics/ledger_verify_benchmark.py --ledger 36309 --passes 5
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time


def resident_mb():
    with open("/proc/self/status") as status:
        return next(int(line.split()[1]) for line in status if line.startswith("VmRSS:")) / 1024


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2],
                        help="checkout whose tools package is measured")
    parser.add_argument("--ledger", type=int, default=36309, help="completed ledger requests to seed")
    parser.add_argument("--passes", type=int, default=5)
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    from _harness import use_tree
    use_tree(args.tree)
    from tools.intake import store
    from tools.diagnostics.ledger_scale_benchmark import seed_ledger
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        store.initialize(root)
        seed_ledger(root, args.ledger)
        db = root / ".local/intake/intake.sqlite3"
        with closing(sqlite3.connect(db)) as connection:
            start = time.perf_counter()
            store.check_identity(connection)
            open_seconds = round(time.perf_counter() - start, 3)
        def validate_old(r):
            with closing(sqlite3.connect(db)) as connection:
                connection.row_factory = sqlite3.Row
                store.validate(connection, r)
        full = getattr(store, "audit", None) or validate_old
        before, seconds = resident_mb(), []
        for _ in range(args.passes):
            start = time.perf_counter()
            full(root)
            seconds.append(round(time.perf_counter() - start, 3))
        print(json.dumps({"label": args.label, "ledger_requests": args.ledger, "writer_open_s": open_seconds,
                          "pass_seconds": seconds,
                          "repeat_median_s": sorted(seconds[1:])[len(seconds[1:]) // 2] if len(seconds) > 1 else None,
                          "resident_delta_mb": round(resident_mb() - before, 1)}, sort_keys=True))


if __name__ == "__main__":
    main()
