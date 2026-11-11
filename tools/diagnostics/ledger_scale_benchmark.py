#!/usr/bin/env python3
"""Synthetic production-sized ledger and catalog: per-file ingest cost and full ledger validations.

Builds a temporary instance whose intake ledger already holds ``--ledger`` completed
single-stage requests and whose catalog holds ``--catalog`` upload-shaped registrations (both written
directly, without per-write validation, so large sizes build quickly), then ingests ``--files`` new
synthetic markdown files through ``tools.intake.cohort.ingest``. Reports body-free JSON: wall and CPU
seconds, seconds per file, how many times the whole ledger was validated and what share of the wall
time that took. With a healthy hand-off (decision 0014) the ledger is validated about once per
process, not once per lock acquisition. Synthetic fixtures only; never point it at a real instance.

    python3 tools/diagnostics/ledger_scale_benchmark.py --ledger 36000 --catalog 25600 --files 40
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


def seed_ledger(root, count):
    from tools import policy
    from tools.intake import contracts
    policy_sha = policy.load()[1]
    with closing(sqlite3.connect(root / ".local/intake/intake.sqlite3")) as connection, connection:
        for number in range(count):
            correlation = f"pad-{number:06d}"
            raw = json.dumps({
                "version": 1, "correlation_id": correlation, "correction_of": None,
                "policy": {"version": 1, "sha256": policy_sha}, "retry": {"max_attempts": 3, "backoff_seconds": 0},
                "stages": [{"id": "s", "kind": "return", "operation": "await-ack",
                            "arguments": {"delivery_correlation": "d", "disposition_path": "x.json",
                                          "disposition_sha256": "b" * 64}}]}).encode()
            intent = json.dumps({"k": number, "pad": "x" * 250}, sort_keys=True)
            evidence = json.dumps({"k": number, "pad": "y" * 230}, sort_keys=True)
            connection.execute("INSERT INTO requests VALUES (?,?,?,?,?)",
                               (correlation, None, raw, contracts.digest(raw), str(root)))
            connection.execute("INSERT INTO stages VALUES (?,?,?,?,?,?,?,?,?)",
                               (correlation, "s", 0, "succeeded", 0, intent, contracts.digest(intent.encode()),
                                evidence, contracts.digest(evidence.encode())))
            connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (correlation, "s", 0, 1, 1.0, 2.0, "succeeded", None, None, None))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2],
                        help="checkout whose tools package is measured")
    parser.add_argument("--ledger", type=int, default=36000, help="completed ledger requests to seed")
    parser.add_argument("--catalog", type=int, default=25600, help="catalog registrations to seed")
    parser.add_argument("--files", type=int, default=40, help="new files to ingest")
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.tree.resolve()))
    os.chdir(args.tree.resolve())
    from tools.catalog import catalog_store as catalog
    from tools.diagnostics.catalog_lock_benchmark import seed
    from tools.diagnostics.ingest_benchmark import fixture
    from tools.intake import cohort, store
    with tempfile.TemporaryDirectory() as temp:
        root, inbox = Path(temp) / "instance", Path(temp) / "inbox"
        root.mkdir()
        store.initialize(root)
        catalog.initialize(root)
        seed_ledger(root, args.ledger)
        if args.catalog:
            seed(root, args.catalog)
        fixture(inbox, args.files, seed="ledger-scale")
        spent = [0.0, 0]
        real = store.validate

        def timed(*arguments):
            start = time.perf_counter()
            try:
                return real(*arguments)
            finally:
                spent[0] += time.perf_counter() - start
                spent[1] += 1
        store.validate = timed
        start, cpu = time.perf_counter(), time.process_time()
        try:
            report = cohort.ingest(root, inbox, system="bench", actor="bench")
        finally:
            store.validate = real
        wall, cpu = time.perf_counter() - start, time.process_time() - cpu
        print(json.dumps({
            "label": args.label, "ledger_requests": args.ledger, "catalog_registrations": args.catalog,
            "ledger_bytes": (root / ".local/intake/intake.sqlite3").stat().st_size,
            "catalog_bytes": (root / ".local/catalog/catalog.sqlite3").stat().st_size,
            "new_files": args.files, "wall_s": round(wall, 1), "cpu_s": round(cpu, 1),
            "seconds_per_file": round(wall / args.files, 2), "full_ledger_validations": spent[1],
            "ledger_validation_wall_s": round(spent[0], 1), "ledger_validation_share": round(spent[0] / wall, 2),
            "outcome": report["counts"]["outcome"]}, sort_keys=True))


if __name__ == "__main__":
    main()
