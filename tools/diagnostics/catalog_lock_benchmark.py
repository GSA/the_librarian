#!/usr/bin/env python3
"""Synthetic catalog writer-lock profile: where one registration's lock hold time goes.

Builds a temporary instance whose catalog holds N upload-shaped registrations (each a source,
receipt and two artifacts), then registers more through ``catalog_store.submit`` and reports a
body-free JSON breakdown of the writer-lock hold time (``inspect`` before and after the snapshot
check, ``validate_graph``, the daily-snapshot check, insert/commit, seal). The seeding path
writes revisions exactly as ``submit`` does but skips validation so large catalogs build quickly;
``inspect`` re-verifies the result in full before any measurement. Synthetic fixtures only;
never point it at a real instance.

    python3 tools/diagnostics/catalog_lock_benchmark.py --sizes 100,400,1600 --samples 5
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from contextlib import closing


def registration(n):
    sha = hashlib.sha256(f"bench:{n}".encode()).hexdigest()
    prov = [{"path": f"originals/uploads/s{n}/sidecar.json", "sha256": sha, "line_start": None, "line_end": None}]
    base = f"originals/uploads/s{n}"
    artifact = {"id": f"A-upload-{n}-original", "kind": "artifact", "data": {
        "path": f"{base}/note.md", "parent_id": None, "receipt_id": f"D{n}", "role": "original",
        "audience": "source", "coverage": "pending-extraction", "reason": "Synthetic.", "presence": "present",
        "sha256": sha, "hash_basis": "observed", "bytes": 100, "extraction": "pending",
        "handling_refs": [], "provenance": prov}}
    meta = {"id": f"A-upload-{n}-submission", "kind": "artifact", "data": {
        "path": f"{base}/sidecar.json", "parent_id": artifact["id"], "receipt_id": f"D{n}",
        "role": "preserved-metadata", "audience": "agent", "coverage": "excluded", "reason": "Synthetic.",
        "presence": "present", "sha256": sha, "hash_basis": "observed", "bytes": 50,
        "extraction": "not-required", "handling_refs": [], "provenance": []}}
    return [{"id": f"S{n}", "kind": "source", "data": {"category": "artifact", "provenance": prov}},
            {"id": f"D{n}", "kind": "receipt", "data": {
                "source_ids": [f"S{n}"], "artifact_id": artifact["id"], "provenance": prov}},
            artifact, meta]


def seed(root, count):
    """Append ``count`` registrations as ``count`` revisions without per-write validation."""
    from tools.catalog import catalog_store as cs
    from tools.catalog.contracts import canonical, fingerprint, now
    home, db, _ = cs.locations(root)
    with closing(cs.connect(db, write=True)) as connection:
        connection.execute("BEGIN")
        state = cs.inspect(connection)[0]
        revision, previous = state["revision"], state["head_hash"]
        for n in range(count):
            records = registration(n)
            revision += 1
            batch = {"revision": revision, "request_id": f"seed-{n}", "actor": "bench", "reason": "seed",
                     "committed_at": now(),
                     "input_hash": fingerprint({"actor": "bench", "reason": "seed", "records": records}),
                     "record_count": len(records), "previous_hash": previous}
            batch["revision_hash"] = previous = fingerprint(batch)
            connection.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?)", tuple(batch.values()))
            for ordinal, record in enumerate(records):
                connection.execute("INSERT INTO versions VALUES (?,?,?,?,?)",
                                   (revision, ordinal, record["id"], canonical(record), fingerprint(record)))
                connection.execute("INSERT INTO current_records VALUES (?,?,?)", (record["id"], revision, ordinal))
        connection.execute("COMMIT")
        cs.seal(db)


PHASES = ("inspect_1", "validate_graph", "snapshot_check", "inspect_2", "insert_commit", "seal",
          "snapshot_hash", "validate_delta", "attest_commit")
# Present only in trees with the trusted-head commit path; absent names are skipped.
OPTIONAL = {"snapshot_hash": "_binding", "validate_delta": "validate_delta", "attest_commit": "_attest_commit"}


def instrumented_submit(root, records, request_id, **kwargs):
    """Run ``catalog_store.submit`` and return (result, lock_hold_s, per-phase seconds)."""
    from tools.catalog import catalog_store as cs
    marks = {"inspect": [], "graph": 0.0, "snap": 0.0, "seal": 0.0, "hold": None, "pre": 0.0,
             "snapshot_hash": 0.0, "validate_delta": 0.0, "attest_commit": 0.0, "depth": 0}
    originals = {n: getattr(cs, n) for n in ("inspect", "validate_graph", "_daily_snapshot", "seal", "writer_lock")}
    optional = {key: name for key, name in OPTIONAL.items() if hasattr(cs, name)}
    originals.update({name: getattr(cs, name) for name in optional.values()})

    def timed(name, key):
        def run(*args, **kw):
            start = time.perf_counter()
            marks["depth"] += 1
            try:
                return originals[name](*args, **kw)
            finally:
                marks["depth"] -= 1
                spent = time.perf_counter() - start
                if marks["depth"] and key == "snapshot_hash":
                    return  # already inside a timed phase (the attestation re-hashes the new file)
                if key == "inspect":
                    marks["inspect"].append(spent)
                else:
                    marks[key] += spent
        return run

    def locked(*args, **kw):
        manager = originals["writer_lock"](*args, **kw)

        class Hold:
            def __enter__(self_inner):
                manager.__enter__()
                marks["entered"] = time.perf_counter()

            def __exit__(self_inner, *exc):
                marks["hold"] = time.perf_counter() - marks["entered"]
                return manager.__exit__(*exc)
        return Hold()
    cs.inspect, cs.validate_graph = timed("inspect", "inspect"), timed("validate_graph", "graph")
    cs._daily_snapshot, cs.seal, cs.writer_lock = timed("_daily_snapshot", "snap"), timed("seal", "seal"), locked
    for key, name in optional.items():
        setattr(cs, name, timed(name, key))
    try:
        start = time.perf_counter()
        result = cs.submit(root, records, request_id=request_id, **kwargs)
        total = time.perf_counter() - start
    finally:
        for name, original in originals.items():
            setattr(cs, name, original)
    first, second = (marks["inspect"] + [0.0, 0.0])[:2]
    # Time inside the hold not attributed to another phase: SQL reads, INSERTs and the COMMIT itself.
    inner = (marks["hold"] - sum(marks["inspect"]) - marks["graph"] - marks["snap"] - marks["seal"]
             - marks["snapshot_hash"] - marks["validate_delta"] - marks["attest_commit"])
    return result, marks["hold"], total, {
        "inspect_1": first, "validate_graph": marks["graph"], "snapshot_check": marks["snap"],
        "inspect_2": second, "insert_commit": inner, "seal": marks["seal"],
        "snapshot_hash": marks["snapshot_hash"], "validate_delta": marks["validate_delta"],
        "attest_commit": marks["attest_commit"]}


def profile(size, samples, work):
    from tools.catalog import catalog_store as cs
    root = work / f"instance-{size}"
    root.mkdir()
    from tools.intake import store
    store.initialize(root)
    cs.initialize(root)
    start = time.perf_counter()
    seed(root, size)
    seeded = time.perf_counter() - start
    start = time.perf_counter()
    cs.status(root, audit=True)
    audit = time.perf_counter() - start
    rows = []
    for k in range(samples):
        state = cs.status(root)["state_token"]
        _, hold, total, phases = instrumented_submit(
            root, registration(size + k), f"measure-{k}", actor="bench", expected_state=state,
            reason="measure")
        rows.append((hold, total, phases))
    median = lambda values: round(statistics.median(values) * 1000, 1)
    return {"registrations": size, "records": size * 4, "seed_s": round(seeded, 2), "audit_s": round(audit, 2),
            "hold_ms": median([r[0] for r in rows]), "total_ms": median([r[1] for r in rows]),
            "phases_ms": {p: median([r[2][p] for r in rows]) for p in PHASES},
            "db_bytes": (root / ".local/catalog/catalog.sqlite3").stat().st_size}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--sizes", default="100,400,1600")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.tree.resolve()))
    os.chdir(args.tree.resolve())
    results = []
    for size in (int(s) for s in args.sizes.split(",")):
        with tempfile.TemporaryDirectory() as temp:
            results.append(profile(size, args.samples, Path(temp)))
        print(json.dumps(results[-1], sort_keys=True), file=sys.stderr, flush=True)
    print(json.dumps({"label": args.label, "results": results}, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
