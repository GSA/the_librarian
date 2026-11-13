#!/usr/bin/env python3
"""Synthetic cohort ingest benchmark for the catalog write path.

Generates non-identifying markdown files in a temporary inbox, ingests them into
a fresh temporary instance through ``tools.intake.cohort.ingest`` and prints a
body-free JSON report: throughput, p50/p95 per-file latency, CPU time, stage
timings (inclusive wall time of the catalog stages), backup count/bytes and
stable disposition totals. It never reads a real instance or corpus; run it from
any checkout with ``--tree`` to compare revisions on identical fixtures.

    python3 tools/diagnostics/ingest_benchmark.py --sizes 100,200,400 --resume

``--replay`` times re-ingesting an unchanged, fully ingested inbox (Kata wrwb): wall and CPU
seconds, bytes read by the process (``/proc/self/io`` ``rchar``, Linux only) and a SHA-256 of the
whole report, so two trees can be compared on identical output. ``--reuse DIR`` builds the
instance and inbox under ``DIR`` once and re-measures them on later runs (replays are read-only),
so an expensive first ingest is not paid per tree.

    python3 tools/diagnostics/ingest_benchmark.py --sizes 2000 --replay --reuse /tmp/replay-bench
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
    ("tools.intake.upload", "publish"), ("tools.intake.upload", "records_for"), ("tools.intake.upload", "next_id"),
    ("tools.intake.upload", "plan"), ("tools.intake.upload", "acknowledgment"), ("tools.intake.upload", "submit"),
    ("tools.intake.cohort", "classify"), ("tools.intake.cohort", "_extract"),
    ("tools.extraction.extract", "run"),
    ("tools.intake.store", "stage"), ("tools.intake.runner", "run"),
)


def fixture(inbox, count, seed="bench", padding=0, mixed=False):
    """Deterministic distinct small markdown files; no real or identifying content.

    ``padding`` adds that many filler lines (about 70 bytes each) to every file. ``mixed`` turns one
    file in six into a MIME HTML document and one in six into a Gemini-style transcript, so the
    format readers run as well as the plain-text path; ``mixed="mime"`` makes every file MIME HTML.
    """
    for number in range(count):
        digest = hashlib.sha256(f"{seed}:{number}".encode()).hexdigest()
        path = inbox / f"group-{number % 10}" / f"note-{number:06d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = f"# Synthetic note {number}\n\n" + "".join(
            f"Line {i} {digest[i:i + 16]}\n" for i in range(12)) + "".join(
            f"Filler {i} {digest[i % 48:i % 48 + 16]} {'x' * 40}\n" for i in range(padding))
        if mixed == "mime" or mixed and number % 6 == 1:
            html = "".join(f"<p class=3D\"x\">{line}</p>\r\n" for line in body.replace("=", "").splitlines())
            path = path.with_suffix(".doc")
            body = ("MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=\"----=_Part_1\"\r\n\r\n"
                    "------=_Part_1\r\nContent-Type: text/html; charset=utf-8\r\n"
                    "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
                    f"<html><body>{html}</body></html>\r\n------=_Part_1--\r\n")
        elif mixed and number % 6 == 2:
            path = path.with_suffix(".txt")
            body = "Summary\nDecided things.\nTranscript\n" + "".join(f"Alex: {line}\n" for line in body.splitlines())
        path.write_text(body, encoding="utf-8", newline="")


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
        # The catalog writer lock is a context manager, so time how long it is held.
        from tools.catalog import catalog_store
        lock = catalog_store.writer_lock

        @contextlib.contextmanager
        def timed_lock(home, *args, **kwargs):
            with lock(home, *args, **kwargs):
                if Path(home).name != "catalog":  # extraction has its own, separate lock
                    yield
                    return
                start = time.perf_counter()
                try:
                    yield
                finally:
                    entry = timers.setdefault("catalog_store.writer_lock_held", [0, 0.0, 0.0])
                    entry[0] += 1
                    entry[1] += time.perf_counter() - start
        saved.append((catalog_store, "writer_lock", lock))
        catalog_store.writer_lock = timed_lock
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


def bytes_read():
    """Bytes this process asked the kernel to read, or None where /proc/self/io is unavailable."""
    try:
        with open("/proc/self/io", encoding="ascii") as stream:
            return int(dict(line.split(": ") for line in stream.read().splitlines())["rchar"])
    except (OSError, KeyError, ValueError):
        return None


def replay(cohort, root, inbox, files, repeats):
    """Re-ingest the unchanged inbox ``repeats`` times; report the median run and the report digest."""
    runs = []
    for _ in range(repeats):
        before, start, cpu = bytes_read(), time.perf_counter(), time.process_time()
        report = cohort.ingest(root, inbox, system="bench", actor="bench")
        wall, cpu, after = time.perf_counter() - start, time.process_time() - cpu, bytes_read()
        runs.append((wall, cpu, None if before is None else after - before, report))
    runs.sort(key=lambda run: run[0])
    wall, cpu, read, report = runs[len(runs) // 2]
    return {
        "wall_s": round(wall, 2), "cpu_s": round(cpu, 2), "ms_per_file": round(wall / files * 1000, 2),
        "cpu_ms_per_file": round(cpu / files * 1000, 2),
        "bytes_read": read, "kib_read_per_file": None if read is None else round(read / files / 1024, 1),
        "repeats": repeats, "wall_s_all": [round(run[0], 2) for run in runs],
        "outcome": {k: v for k, v in report["counts"]["outcome"].items() if v},
        "report_sha256": hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(),
    }


def run_replay(size, work, repeats, padding, kind):
    """Build (or reuse) a fully ingested instance, then time replays of its unchanged inbox."""
    from tools.catalog import catalog_store as catalog
    from tools.intake import cohort, store
    root, inbox = work / "instance", work / "inbox"
    marker = work / "built.json"
    wanted = {"files": size, "padding": padding, "fixture": kind}
    built = marker.exists()
    if built:
        # Only a fully ingested fixture with exactly these parameters may be replayed.
        if json.loads(marker.read_text()) != wanted:
            raise SystemExit(f"{work} holds a different fixture ({marker.read_text().strip()}); "
                             "use another --reuse directory")
    else:
        if work.exists() and any(work.iterdir()):
            raise SystemExit(f"{work} holds an unfinished fixture; remove it or use another --reuse directory")
        root.mkdir(parents=True)
        store.initialize(root)
        catalog.initialize(root)
        fixture(inbox, size, padding=padding, mixed=None if kind == "text" else kind)
        first = cohort.ingest(root, inbox, system="bench", actor="bench")
        assert first["counts"]["outcome"]["extracted"] == size, first["counts"]["outcome"]
        marker.write_text(json.dumps(wanted, sort_keys=True))
    inbox_bytes = sum(path.stat().st_size for path in inbox.rglob("*") if path.is_file())
    result = {"files": size, "reused": built, "inbox_bytes": inbox_bytes,
              "replay": replay(cohort, root, inbox, size, repeats)}
    if result["replay"]["bytes_read"] is not None:
        result["replay"]["read_x_inbox"] = round(result["replay"]["bytes_read"] / inbox_bytes, 2)
    return result


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
    parser.add_argument("--replay", action="store_true",
                        help="ingest once, then time re-ingesting the unchanged inbox")
    parser.add_argument("--repeats", type=int, default=3, help="replays to time with --replay (median reported)")
    parser.add_argument("--padding", type=int, default=0,
                        help="filler lines (about 70 bytes) added to every fixture file with --replay")
    parser.add_argument("--fixture", choices=("text", "mixed", "mime"), default="text",
                        help="with --replay: plain markdown (default), a third MIME HTML and Gemini-style "
                             "transcripts, or all MIME HTML")
    parser.add_argument("--reuse", type=Path,
                        help="with --replay, keep and reuse the instance and inbox under this directory")
    parser.add_argument("--label", default="")
    parser.add_argument("--keep", type=Path, help="parent directory for fixtures (default: temporary)")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.tree.resolve()))
    os.chdir(args.tree.resolve())
    results = []
    for size in (int(s) for s in args.sizes.split(",")):
        if args.replay:
            if args.reuse:
                work = args.reuse / str(size)
                work.mkdir(parents=True, exist_ok=True)
                results.append(run_replay(size, work, args.repeats, args.padding, args.fixture))
            else:
                with tempfile.TemporaryDirectory(dir=args.keep) as temp:
                    results.append(run_replay(size, Path(temp), args.repeats, args.padding, args.fixture))
            print(json.dumps({"label": args.label, "partial": results[-1]}, sort_keys=True),
                  file=sys.stderr, flush=True)
            continue
        with tempfile.TemporaryDirectory(dir=args.keep) as temp:
            results.append(run_size(size, args.resume, Path(temp)))
        print(json.dumps({"label": args.label, "partial": results[-1]}, sort_keys=True),
              file=sys.stderr, flush=True)
    print(json.dumps({"label": args.label, "tree": str(args.tree.name), "python": sys.version.split()[0],
                      "results": results}, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
