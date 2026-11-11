#!/usr/bin/env python3
"""Synthetic first-write-of-the-day profile: what the daily snapshot costs inside the catalog writer lock.

After decision 0015 a normal catalog write validates only its delta, but the first write of each
UTC day still takes the daily snapshot, whose copy, verification and retention replay the whole
history while the writer lock is held. This tool seeds a synthetic catalog of N upload-shaped
registrations once (``seed``), copies it for every trial and reports body-free JSON:

* ``profile``: lock hold of a first-of-day write split into the snapshot copy, every full
  ``inspect`` (labelled by its caller), snapshot hashing, retention and the rest; then the hold of
  a second same-day write, which still checks today's snapshot.
* ``contend``: several writer processes start together; one is the first writer of the day. Reports
  how long each writer waited, how many ``WriterBusy`` refusals it saw, and when it committed.

The UTC clock is shifted one day for the trial (the seed carries a full snapshot "from yesterday"), so no wall-clock
waiting is needed. Synthetic fixtures only; never point it at a real instance.

    python3 tools/diagnostics/catalog_daily_replay_benchmark.py seed --dir D --size 6400
    python3 tools/diagnostics/catalog_daily_replay_benchmark.py profile --dir D --samples 3
    python3 tools/diagnostics/catalog_daily_replay_benchmark.py contend --dir D --writers 3
"""
import argparse
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import statistics
import sys
import time
from datetime import timedelta


def _tree(args):
    tree = str(args.tree.resolve())
    sys.path.insert(0, tree)
    os.chdir(tree)
    return tree


def shift_clock(days=1):
    """Make this process believe it is ``days`` later, for the snapshot gate and receipts only."""
    from tools import backups
    real = backups.utcnow
    backups.utcnow = lambda: real() + timedelta(days=days)


def seed(directory, size):
    from tools.catalog import catalog_store as cs
    from tools.diagnostics.catalog_lock_benchmark import seed as seed_records
    from tools.intake import store
    root = Path(directory) / "instance"
    if root.exists():
        raise SystemExit("seed directory already holds an instance")
    root.mkdir(parents=True)
    store.initialize(root)
    cs.initialize(root)
    start = time.perf_counter()
    seed_records(root, size)
    seeded = time.perf_counter() - start
    start = time.perf_counter()
    state = cs.status(root, audit=True)  # one full replay; also writes the read-validation receipt
    audited = time.perf_counter() - start
    cs.backup(root)  # the previous day's full snapshot, as an installed instance would hold
    (Path(directory) / "seed.json").write_text(json.dumps({
        "registrations": size, "revision": state["revision"], "records": state["records"], "seed_s": round(seeded, 2),
        "audit_s": round(audited, 2), "db_bytes": (root / ".local/catalog/catalog.sqlite3").stat().st_size
        if (root / ".local/catalog/catalog.sqlite3").exists() else cs.locations(root)[1].stat().st_size}))
    print((Path(directory) / "seed.json").read_text())


def fresh_copy(directory, name):
    target = Path(directory) / name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(Path(directory) / "instance", target, symlinks=True)
    return target


def caller(depth=2):
    return sys._getframe(depth).f_code.co_name


def instrument(cs, snapshots, marks):
    """Time the phases of one ``submit``; calls are appended to ``marks['calls']`` as (name, label, seconds)."""
    saved = []

    def wrap(module, name, label=None):
        original = getattr(module, name)

        def timed(*args, **kwargs):
            start = time.perf_counter()
            who = caller()
            try:
                return original(*args, **kwargs)
            finally:
                marks["calls"].append((label or name, who, time.perf_counter() - start, start))
        saved.append((module, name, original))
        setattr(module, name, timed)

    for name in ("inspect", "file_hash", "_binding", "validate_delta", "_attest_commit", "seal", "verify_backup",
                 "_snapshot_in_transaction", "_daily_snapshot", "_trusted_state"):
        if hasattr(cs, name):
            wrap(cs, name)
    for name in ("prune", "has_snapshot_today", "_fresh_ok", "scan"):
        wrap(snapshots, name, "backups." + name)
    original_lock = cs.writer_lock

    def locked(*args, **kwargs):
        manager = original_lock(*args, **kwargs)

        class Hold:
            def __enter__(self):
                marks["waiting_from"] = time.perf_counter()
                manager.__enter__()
                marks["entered"] = time.perf_counter()
                marks["lock_wait"] = marks["entered"] - marks["waiting_from"]

            def __exit__(self, *exc):
                marks["hold"] = time.perf_counter() - marks["entered"]
                return manager.__exit__(*exc)
        return Hold()
    saved.append((cs, "writer_lock", original_lock))
    cs.writer_lock = locked

    def restore():
        for module, name, original in saved:
            setattr(module, name, original)
    return restore


def breakdown(marks):
    """Fold the recorded calls into body-free phases (ms); nested time is attributed to the innermost phase."""
    calls = marks["calls"]
    inspects = [(who, spent) for name, who, spent, _ in calls if name == "inspect"]
    by_caller = {}
    for who, spent in inspects:
        by_caller.setdefault(who, []).append(round(spent * 1000, 1))
    total = lambda name: sum(spent for n, _, spent, _ in calls if n == name)
    hashes = [spent for n, who, spent, _ in calls if n == "file_hash"]
    phases = {
        "full_inspects": len(inspects),
        "inspect_total": round(sum(s for _, s in inspects) * 1000, 1),
        "inspect_by_caller_ms": by_caller,
        "file_hash_calls": len(hashes),
        "file_hash_total": round(sum(hashes) * 1000, 1),
        "daily_snapshot_total": round(total("_daily_snapshot") * 1000, 1),
        "snapshot_take_total": round(total("_snapshot_in_transaction") * 1000, 1),
        "verify_backup_calls": sum(1 for n, *_ in calls if n == "verify_backup"),
        "verify_backup_total": round(total("verify_backup") * 1000, 1),
        "retention_prune": round(total("backups.prune") * 1000, 1),
        "has_snapshot_today": round(total("backups.has_snapshot_today") * 1000, 1),
        "binding": round(total("_binding") * 1000, 1),
        "validate_delta": round(total("validate_delta") * 1000, 1),
        "attest": round(total("_attest_commit") * 1000, 1),
        "seal_calls": sum(1 for n, *_ in calls if n == "seal"),
        "seal_total": round(total("seal") * 1000, 1),
    }
    return phases


def one_write(root, number, label):
    from tools.catalog import catalog_store as cs
    from tools.diagnostics.catalog_lock_benchmark import registration
    from tools import backups as snapshots
    marks = {"calls": []}
    state = cs.status(root)["state_token"]  # outside the measurement: a reader's receipt refresh
    restore = instrument(cs, snapshots, marks)
    try:
        start = time.perf_counter()
        result = cs.submit(root, registration(number), actor="bench", request_id=f"{label}-{number}",
                           expected_state=state, reason="measure")
        total = time.perf_counter() - start
    finally:
        restore()
    return {"hold_ms": round(marks["hold"] * 1000, 1), "total_ms": round(total * 1000, 1),
            "took_snapshot": result["backup"] is not None, **breakdown(marks)}


def profile(args):
    from tools.catalog import catalog_store as cs
    shift_clock()
    meta = json.loads((Path(args.dir) / "seed.json").read_text())
    rows = []
    for k in range(args.samples):
        work = fresh_copy(args.dir, "work")
        root = work
        base = meta["registrations"] + 10 * k
        first = one_write(root, base, "first")
        second = one_write(root, base + 1, "second")
        # a third write whose request id repeats the first (idempotent retry) costs only the lookups
        rows.append({"first_of_day": first, "second_write": second})
        print(json.dumps(rows[-1], sort_keys=True), file=sys.stderr, flush=True)
        db = cs.locations(root)[1]
        backups = sorted(p.stat().st_size for p in cs.locations(root)[2].glob("*.sqlite3"))
        shutil.rmtree(work)
    med = lambda key, part: round(statistics.median(r[part][key] for r in rows), 1)
    summary = {"seed": meta, "samples": args.samples, "db_bytes": db_size(meta),
               "first_hold_ms": med("hold_ms", "first_of_day"), "second_hold_ms": med("hold_ms", "second_write"),
               "first": rows[0]["first_of_day"], "second": rows[0]["second_write"], "all": rows,
               "last_snapshot_bytes": backups[-1] if backups else None}
    print(json.dumps(summary, indent=1, sort_keys=True))


def db_size(meta):
    return meta.get("db_bytes")


def writer(tree, root, number, delay, count, start_at, queue):
    sys.path.insert(0, tree)
    os.chdir(tree)
    shift_clock()
    from tools.catalog import catalog_store as cs
    from tools.diagnostics.catalog_lock_benchmark import registration
    root = Path(root)
    while time.time() < start_at + delay:
        time.sleep(0.001)
    report = {"writer": number, "busy": 0, "stale": 0, "commits": [], "snapshot_by": False, "attempts": []}
    t0 = time.perf_counter()
    for index in range(count):
        request = f"w{number}-{index}"
        records = registration(2_000_000 + number * 1000 + index)
        tries = 0
        while True:
            tries += 1
            began = time.perf_counter()
            try:
                pinned = cs.status(root)["state_token"]
                result = cs.submit(root, records, actor="bench", request_id=request, expected_state=pinned,
                                   reason="contend", rebase=True)
                report["commits"].append(round(time.perf_counter() - t0, 3))
                report.setdefault("attempts_per_commit", []).append(tries)
                report["snapshot_by"] = report["snapshot_by"] or result["backup"] is not None
                break
            except cs.WriterBusy:
                report["busy"] += 1
                report["attempts"].append(("busy", round(time.perf_counter() - began, 2)))
            except cs.StaleState:
                report["stale"] += 1
    report["wall_s"] = round(time.perf_counter() - t0, 3)
    queue.put(report)


def contend(args):
    tree = str(args.tree.resolve())
    meta = json.loads((Path(args.dir) / "seed.json").read_text())
    work = fresh_copy(args.dir, "work-contend")
    root = work
    queue = multiprocessing.Queue()
    start_at = time.time() + 1.0
    procs = [multiprocessing.Process(target=writer, args=(tree, str(root), n, 0.0 if n == 0 else 0.3 + 0.2 * n,
                                                          args.per_writer, start_at, queue))
             for n in range(args.writers)]
    for proc in procs:
        proc.start()
    reports = sorted((queue.get() for _ in procs), key=lambda r: r["writer"])
    for proc in procs:
        proc.join()
    shutil.rmtree(work)
    print(json.dumps({"seed": meta, "writers": args.writers, "per_writer": args.per_writer, "reports": reports},
                     indent=1, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("seed", "profile", "contend"):
        sub = commands.add_parser(name)
        sub.add_argument("--dir", required=True, type=Path)
    commands.choices["seed"].add_argument("--size", type=int, required=True)
    commands.choices["profile"].add_argument("--samples", type=int, default=3)
    commands.choices["contend"].add_argument("--writers", type=int, default=3)
    commands.choices["contend"].add_argument("--per-writer", type=int, default=3)
    args = parser.parse_args(argv)
    _tree(args)
    if args.command == "seed":
        seed(args.dir, args.size)
    elif args.command == "profile":
        profile(args)
    else:
        contend(args)


if __name__ == "__main__":
    main()
