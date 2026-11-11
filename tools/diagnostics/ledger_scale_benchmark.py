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
import contextlib
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


def seed_uploads(root, count, total):
    """Ledger rows matching ``catalog_lock_benchmark.seed``: ``count`` committed upload requests
    (same IDs the seeded catalog holds) padded with await-ack requests to ``total`` rows."""
    from tools import policy
    from tools.diagnostics.catalog_lock_benchmark import registration
    from tools.intake import contracts
    policy_sha = policy.load()[1]
    base = {"version": 1, "correction_of": None, "policy": {"version": 1, "sha256": policy_sha},
            "retry": {"max_attempts": 3, "backoff_seconds": 0}}
    with closing(sqlite3.connect(root / ".local/intake/intake.sqlite3")) as connection, connection:
        def insert(correlation, stage, raw, evidence_pad):
            intent = json.dumps({"pad": "x" * 250}, sort_keys=True)
            evidence = json.dumps({"pad": evidence_pad}, sort_keys=True)
            connection.execute("INSERT INTO requests VALUES (?,?,?,?,?)",
                               (correlation, None, raw, contracts.digest(raw), str(root)))
            connection.execute("INSERT INTO stages VALUES (?,?,?,?,?,?,?,?,?)",
                               (correlation, stage, 0, "succeeded", 0, intent, contracts.digest(intent.encode()),
                                evidence, contracts.digest(evidence.encode())))
            connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (correlation, stage, 0, 1, 1.0, 2.0, "succeeded", None, None, None))
        for n in range(count):
            correlation = f"upload-s{n}"
            args = {"records": registration(n), "actor": "bench", "request_id": correlation,
                    "expected_state": "0" * 64, "reason": "Manual upload: synthetic"}
            raw = json.dumps({**base, "correlation_id": correlation, "stages": [{
                "id": "receipt", "kind": "receipt", "operation": "catalog-submit", "arguments": args}]}).encode()
            insert(correlation, "receipt", raw, "y" * 230)
        for n in range(max(0, total - count)):
            correlation = f"pad-{n:06d}"
            raw = json.dumps({**base, "correlation_id": correlation, "stages": [{
                "id": "s", "kind": "return", "operation": "await-ack",
                "arguments": {"delivery_correlation": "d", "disposition_path": "x.json",
                              "disposition_sha256": "b" * 64}}]}).encode()
            insert(correlation, "s", raw, "y" * 230)


def ids_digest(root):
    """Digest of every current catalog record, so two trees can be compared on identical output."""
    import hashlib
    from tools.catalog import catalog_store as catalog
    digest = hashlib.sha256()
    for record in sorted(catalog.read_records(root, limit=10 ** 7)["records"], key=lambda r: r["id"]):
        digest.update(json.dumps(record, sort_keys=True).encode())
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2],
                        help="checkout whose tools package is measured")
    parser.add_argument("--ledger", type=int, default=36000, help="completed ledger requests to seed")
    parser.add_argument("--catalog", type=int, default=25600, help="catalog registrations to seed")
    parser.add_argument("--files", type=int, default=40, help="new files to ingest")
    parser.add_argument("--matched", action="store_true",
                        help="seed one committed upload-* ledger request per catalog registration (as real "
                             "uploads leave), so the per-file upload planning cost is representative")
    parser.add_argument("--warm", action="store_true",
                        help="validate the catalog once before timing, so the one-off first-of-day replay "
                             "(decision 0016) is not counted in the per-file figures")
    parser.add_argument("--profile", action="store_true", help="also report per-function wall and CPU totals")
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.tree.resolve()))
    os.chdir(args.tree.resolve())
    from tools.catalog import catalog_store as catalog
    from tools.diagnostics.catalog_lock_benchmark import seed
    from tools.diagnostics.ingest_benchmark import fixture, instrument
    from tools.intake import cohort, store
    with tempfile.TemporaryDirectory() as temp:
        root, inbox = Path(temp) / "instance", Path(temp) / "inbox"
        root.mkdir()
        store.initialize(root)
        catalog.initialize(root)
        if args.matched:
            seed(root, args.catalog)
            seed_uploads(root, args.catalog, args.ledger)
        else:
            seed_ledger(root, args.ledger)
            if args.catalog:
                seed(root, args.catalog)
        fixture(inbox, args.files, seed="ledger-scale")
        if args.warm:
            catalog.status(root, audit=True)
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
        timers = {}
        start, cpu = time.perf_counter(), time.process_time()
        try:
            with instrument(timers) if args.profile else contextlib.nullcontext():
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
            "outcome": report["counts"]["outcome"],
            **({"ids_sha256": ids_digest(root), "stages": {
                name: {"calls": c, "wall_s": round(w, 2), "cpu_s": round(p, 2)}
                for name, (c, w, p) in sorted(timers.items(), key=lambda kv: -kv[1][1])}} if args.profile else {})},
            sort_keys=True))


if __name__ == "__main__":
    main()
