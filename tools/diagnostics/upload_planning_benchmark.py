#!/usr/bin/env python3
"""Synthetic per-file upload planning profile: what ``upload.records_for`` costs as state grows.

Builds a temporary instance whose catalog holds ``--catalog`` upload-shaped registrations and whose
intake ledger holds the matching ``upload-*`` requests (each promising the same four records, as a
real upload does) plus padding requests up to ``--ledger`` rows, then times each part of one
planning step: ``catalog_view`` split into ``validated_state``, ``current_view`` and the current-ID
scan, ``reserved_ids``, ``next_id`` and the whole ``records_for`` call. Reports body-free JSON.
Synthetic fixtures only; never point it at a real instance.

    nice python3 tools/diagnostics/upload_planning_benchmark.py --catalog 1600,6400,25600 --ledger 36000
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time


def timed(function, *args):
    start = time.perf_counter()
    result = function(*args)
    return result, (time.perf_counter() - start) * 1000


def profile(catalog_size, ledger_size, samples, work):
    from tools.catalog import catalog_store as catalog
    from tools.diagnostics.catalog_lock_benchmark import registration, seed
    from tools.diagnostics.ledger_scale_benchmark import seed_uploads
    from tools.intake import store, upload
    root = work / "instance"
    root.mkdir()
    store.initialize(root)
    catalog.initialize(root)
    seed(root, catalog_size)
    seed_uploads(root, catalog_size, ledger_size)
    catalog.status(root, audit=True)  # one full validation: leaves a same-day receipt for the identity path
    cold = None
    rows = {key: [] for key in ("validated_state", "current_view", "id_scan", "catalog_view", "reserved_ids",
                                "next_id", "records_for")}
    value = {"submission_id": "bench-new", "correction_of": None, "format": "text", "name": "n.md",
             "actor": "bench", "reason": "bench",
             "origin": {"kind": "operator-supplied", "system": None, "locator": None, "version": None},
             "expected_sha256": None}
    raw = b"# synthetic\n"
    directory, original, sidecar_path, sidecar = upload.plan(root, value, raw)
    for _ in range(samples):
        home, db, _ = catalog.locations(root)
        with closing(catalog.connect(db)) as connection:
            connection.execute("BEGIN")
            _, ms = timed(catalog.validated_state, connection, home)
            rows["validated_state"].append(ms)
            _, ms = timed(catalog.current_view, connection)
            rows["current_view"].append(ms)
            ids, ms = timed(lambda: {r[0] for r in connection.execute("SELECT id FROM catalog_current")})
            rows["id_scan"].append(ms)
        (_, taken), ms = timed(upload.catalog_view, root)
        rows["catalog_view"].append(ms)
        reserved, ms = timed(upload.reserved_ids, root)
        rows["reserved_ids"].append(ms)
        if len(rows["reserved_ids"]) == 1:
            cold = ms  # the first call of a process pays for any per-process memo
        _, ms = timed(upload.next_id, "S", taken | reserved)
        rows["next_id"].append(ms)
        _, ms = timed(upload.records_for, root, value, raw, original, sidecar_path, sidecar)
        rows["records_for"].append(ms)
    return {"catalog_registrations": catalog_size, "ledger_requests": ledger_size,
            "catalog_bytes": (root / ".local/catalog/catalog.sqlite3").stat().st_size,
            "ledger_bytes": (root / ".local/intake/intake.sqlite3").stat().st_size,
            "reserved_ids_first_call_ms": round(cold, 1),
            "median_ms_after_first": {k: round(statistics.median(v[1:] or v), 1) for k, v in rows.items()}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--catalog", default="1600,6400,25600", help="registrations to seed, comma separated")
    parser.add_argument("--ledger", type=int, default=36000, help="minimum ledger rows (at least one per registration)")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.tree.resolve()))
    os.chdir(args.tree.resolve())
    results = []
    for size in (int(s) for s in args.catalog.split(",")):
        with tempfile.TemporaryDirectory() as temp:
            results.append(profile(size, max(args.ledger, size), args.samples, Path(temp)))
        print(json.dumps(results[-1], sort_keys=True), file=sys.stderr, flush=True)
    print(json.dumps({"label": args.label, "results": results}, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
