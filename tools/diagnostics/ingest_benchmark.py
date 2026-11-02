#!/usr/bin/env python3
"""Synthetic cohort ingest benchmark for the catalog write path.

Generates non-identifying markdown files in a temporary inbox, ingests them into
a fresh temporary instance through ``tools.intake.cohort.ingest`` and prints a
body-free JSON report: throughput, p50/p95 per-file latency, CPU time, stage
timings (inclusive wall time of the catalog stages), backup count/bytes and
stable disposition totals. It never reads a real instance or corpus; run it from
any checkout with ``--tree`` to compare revisions on identical fixtures.

    python3 tools/diagnostics/ingest_benchmark.py --sizes 100,200,400 --resume
"""
import argparse
import contextlib
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

STAGES = (
    ("tools.catalog.catalog_store", "inspect"), ("tools.catalog.catalog_store", "validated_state"),
    ("tools.catalog.catalog_store", "submit"), ("tools.catalog.catalog_store", "status"),
    ("tools.catalog.catalog_store", "read_records"),
    ("tools.catalog.catalog_store", "_daily_snapshot"), ("tools.catalog.catalog_store", "_snapshot"),
    ("tools.intake.upload", "catalog_view"), ("tools.intake.upload", "reserved_ids"),
    ("tools.intake.upload", "publish"), ("tools.extraction.extract", "run"),
    ("tools.intake.store", "stage"), ("tools.intake.runner", "run"),
)


def fixture(inbox, count, seed="bench"):
    """Deterministic distinct small markdown files; no real or identifying content."""
    for number in range(count):
        digest = hashlib.sha256(f"{seed}:{number}".encode()).hexdigest()
        path = inbox / f"group-{number % 10}" / f"note-{number:06d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = f"# Synthetic note {number}\n\n" + "".join(
            f"Line {i} {digest[i:i + 16]}\n" for i in range(12))
        path.write_text(body, encoding="utf-8")


@contextlib.contextmanager
def instrument(timers):
    """Time the STAGES functions into ``timers``; always restore the originals on exit."""
    import importlib
    saved = []

    def wrap(module, name):
        original = getattr(module, name)

        def timed(*args, **kwargs):
            start, cpu = time.perf_counter(), time.process_time()
            try:
                return original(*args, **kwargs)
            finally:
                entry = timers.setdefault(f"{module.__name__.rsplit('.', 1)[-1]}.{name}", [0, 0.0, 0.0])
                entry[0] += 1
                entry[1] += time.perf_counter() - start
                entry[2] += time.process_time() - cpu
        saved.append((module, name, original))
        setattr(module, name, timed)
    try:
        for module_name, name in STAGES:
            module = importlib.import_module(module_name)
            if hasattr(module, name):
                wrap(module, name)
        yield timers
    finally:
        for module, name, original in reversed(saved):
            setattr(module, name, original)


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))] if ordered else 0.0


def backups(root):
    count = size = 0
    for sub in (".local/catalog/backups", ".local/review-events/backups"):
        directory = root / sub
        if directory.is_dir():
            for path in directory.iterdir():
                if path.suffix == ".sqlite3":
                    count += 1
                    size += path.stat().st_size
    return {"snapshots": count, "bytes": size}


def totals(root):
    from tools.catalog import catalog_store as catalog
    records = catalog.read_records(root, limit=10 ** 6)
    digest = hashlib.sha256()
    for record in sorted(records["records"], key=lambda r: r["id"]):
        digest.update(json.dumps(record, sort_keys=True).encode())
    db = root / ".local/catalog/catalog.sqlite3"
    return {"records": records["total_matching"], "records_sha256": digest.hexdigest(),
            "catalog_bytes": db.stat().st_size}


def phase(cohort, root, inbox, limit, timers):
    latencies = []
    original = cohort._process

    def process(*args, **kwargs):
        start = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            latencies.append(time.perf_counter() - start)
    cohort._process = process
    timers.clear()
    start, cpu = time.perf_counter(), time.process_time()
    try:
        report = cohort.ingest(root, inbox, system="bench", actor="bench", limit=limit)
    finally:
        cohort._process = original
    wall, cpu = time.perf_counter() - start, time.process_time() - cpu
    outcome = report["counts"]["outcome"]
    processed = len(latencies)
    return {
        "files_processed": processed, "wall_s": round(wall, 3), "cpu_s": round(cpu, 3),
        "files_per_s": round(processed / wall, 3) if wall else None,
        "latency_ms": {"p50": round(percentile(latencies, .5) * 1000, 1),
                       "p95": round(percentile(latencies, .95) * 1000, 1),
                       "first_decile_mean": round(sum(latencies[:max(1, processed // 10)])
                                                  / max(1, processed // 10) * 1000, 1),
                       "last_decile_mean": round(sum(latencies[-max(1, processed // 10):])
                                                 / max(1, processed // 10) * 1000, 1)},
        "outcome": {k: v for k, v in outcome.items() if v},
        "stages": {name: {"calls": c, "wall_s": round(w, 3), "cpu_s": round(p, 3)}
                   for name, (c, w, p) in sorted(timers.items(), key=lambda kv: -kv[1][1])},
    }


def run_size(size, resume, work):
    from tools.catalog import catalog_store as catalog
    from tools.intake import cohort, store
    root, inbox = work / "instance", work / "inbox"
    root.mkdir()
    store.initialize(root)
    catalog.initialize(root)
    fixture(inbox, size)
    timers = {}
    result = {"files": size}
    with instrument(timers):
        if resume:
            half = size // 2
            result["first_half"] = phase(cohort, root, inbox, half, timers)
            result["resumed"] = phase(cohort, root, inbox, None, timers)
        else:
            result["ingest"] = phase(cohort, root, inbox, None, timers)
    result["backups"] = backups(root)
    result["final"] = totals(root)
    start = time.perf_counter()
    catalog.status(root, audit=True)
    result["audit_s"] = round(time.perf_counter() - start, 3)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2],
                        help="checkout whose tools package is measured")
    parser.add_argument("--sizes", default="100,200,400")
    parser.add_argument("--resume", action="store_true",
                        help="ingest half, then rerun the whole inbox to measure resumption")
    parser.add_argument("--label", default="")
    parser.add_argument("--keep", type=Path, help="parent directory for fixtures (default: temporary)")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.tree.resolve()))
    os.chdir(args.tree.resolve())
    results = []
    for size in (int(s) for s in args.sizes.split(",")):
        with tempfile.TemporaryDirectory(dir=args.keep) as temp:
            results.append(run_size(size, args.resume, Path(temp)))
        print(json.dumps({"label": args.label, "partial": results[-1]}, sort_keys=True),
              file=sys.stderr, flush=True)
    print(json.dumps({"label": args.label, "tree": str(args.tree.name), "python": sys.version.split()[0],
                      "results": results}, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
