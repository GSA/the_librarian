#!/usr/bin/env python3
"""Repeat ``store.verify`` cost over a synthetic production-sized intake ledger.

Seeds ``--ledger`` completed single-stage requests in a temporary instance, snapshots them once and
times ``--passes`` consecutive ``store.verify`` calls in one process (what every ``progress`` call and
every un-attested writer pays), plus the resident-memory growth. Body-free JSON. Use ``--tree`` to
compare another checkout's ``tools`` package. Synthetic fixtures only; never point it at a real instance.

    python3 tools/diagnostics/ledger_verify_benchmark.py --ledger 36309 --passes 5
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
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
    sys.path.insert(0, str(args.tree.resolve()))
    os.chdir(args.tree.resolve())
    from tools.intake import store
    from tools.diagnostics.ledger_scale_benchmark import seed_ledger
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        store.initialize(root)
        seed_ledger(root, args.ledger)
        with closing(sqlite3.connect(root / ".local/intake/intake.sqlite3")) as connection:
            connection.row_factory = sqlite3.Row
            rows = store.snapshot(connection)
        before, seconds = resident_mb(), []
        for _ in range(args.passes):
            start = time.perf_counter()
            store.verify(rows, root)
            seconds.append(round(time.perf_counter() - start, 3))
        print(json.dumps({"label": args.label, "ledger_requests": args.ledger, "pass_seconds": seconds,
                          "repeat_median_s": sorted(seconds[1:])[len(seconds[1:]) // 2] if len(seconds) > 1 else None,
                          "resident_delta_mb": round(resident_mb() - before, 1),
                          "memo_mb": round(getattr(store, "verified_bytes", lambda: 0)() / 2**20, 1)}, sort_keys=True))


if __name__ == "__main__":
    main()
