#!/usr/bin/env python3
"""Synthetic cost of binding a catalog validation receipt to the database file (Kata 2c1h).

For each catalog size it seeds a temporary instance (reusing ``catalog_lock_benchmark.seed``) and
reports body-free JSON:

* ``primitives_ms``: the building blocks of a binding on that file: ``serialize()``, SHA-256 of
  the serialized bytes, SHA-256 streamed from the file, BLAKE2b, and the file-identity read
  (``fstat`` plus the 100-byte SQLite header) that decision 0014 already uses for the ledger.
* ``read_hit_ms``: median time of ``catalog_store.validated_state`` when the receipt matches,
  i.e. what every validated read pays, measured in one process.
* ``read_scaling``: wall time and reads per second when ``--readers`` processes loop over
  ``validated_state`` at once (the case that grows with parallel intake).

Writer lock-hold breakdowns come from ``catalog_lock_benchmark.py``; run both with ``--tree`` to
compare checkouts. Synthetic fixtures only; never point this at a real instance.

    python3 tools/diagnostics/catalog_binding_benchmark.py --sizes 400,1600,6400 --readers 1,2,3
"""
import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from contextlib import closing


def timed(function, repeat, digits=2):
    spent = []
    for _ in range(repeat):
        start = time.perf_counter()
        function()
        spent.append(time.perf_counter() - start)
    return round(statistics.median(spent) * 1000, digits)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_blake2b(path):
    digest = hashlib.blake2b()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def identity(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        header = os.pread(fd, 100, 0)
    finally:
        os.close(fd)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, header[24:28].hex()]


def primitives(cs, root, repeat):
    home, db, _ = cs.locations(root)
    out = {}
    with closing(cs.connect(db)) as connection:
        connection.execute("BEGIN")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        out["serialize"] = timed(lambda: connection.serialize(), repeat)
        data = connection.serialize()
        out["sha256_of_serialized"] = timed(lambda: hashlib.sha256(data).hexdigest(), repeat)
        out["binding_total"] = timed(lambda: hashlib.sha256(connection.serialize()).hexdigest(), repeat)
        connection.execute("ROLLBACK")
    out["sha256_file_stream"] = timed(lambda: file_sha256(db), repeat)
    out["blake2b_file_stream"] = timed(lambda: file_blake2b(db), repeat)
    out["identity_read"] = timed(lambda: identity(db), repeat * 20, digits=4)
    return out, db.stat().st_size


def read_worker(tree, root, loops, start_at, queue):
    sys.path.insert(0, tree)
    from tools.catalog import catalog_store as cs
    home, db, _ = cs.locations(Path(root))
    while time.time() < start_at:
        time.sleep(0.001)
    begin = time.perf_counter()
    cpu = time.process_time()
    for _ in range(loops):
        with closing(cs.connect(db)) as connection:
            connection.execute("BEGIN")
            cs.validated_state(connection, home)
    queue.put((loops, time.perf_counter() - begin, time.process_time() - cpu))


def read_scaling(tree, root, readers, loops):
    results = []
    for count in readers:
        queue = multiprocessing.Queue()
        start_at = time.time() + 1.0
        procs = [multiprocessing.Process(target=read_worker, args=(tree, str(root), loops, start_at, queue))
                 for _ in range(count)]
        for proc in procs:
            proc.start()
        reports = [queue.get() for _ in procs]
        for proc in procs:
            proc.join()
        wall = max(report[1] for report in reports)
        total = sum(report[0] for report in reports)
        results.append({"readers": count, "reads": total, "wall_s": round(wall, 3),
                        "reads_per_s": round(total / wall, 1),
                        "cpu_s": round(sum(report[2] for report in reports), 3)})
    return results


def profile(tree, size, repeat, readers, loops, work):
    from tools.catalog import catalog_store as cs
    from tools.diagnostics.catalog_lock_benchmark import seed
    from tools.intake import store
    root = work / f"instance-{size}"
    root.mkdir()
    store.initialize(root)
    cs.initialize(root)
    seed(root, size)
    cs.status(root, audit=True)  # one full validation writes the receipt
    home, db, _ = cs.locations(root)
    prim, db_bytes = primitives(cs, root, repeat)

    def hit():
        with closing(cs.connect(db)) as connection:
            connection.execute("BEGIN")
            cs.validated_state(connection, home)
    hit()
    return {"registrations": size, "records": size * 4, "db_bytes": db_bytes,
            "primitives_ms": prim, "read_hit_ms": timed(hit, repeat),
            "read_scaling": read_scaling(tree, root, readers, loops)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--sizes", default="400,1600,6400")
    parser.add_argument("--readers", default="1,2,3")
    parser.add_argument("--loops", type=int, default=20, help="validated reads per reader process")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    tree = str(args.tree.resolve())
    sys.path.insert(0, tree)
    os.chdir(tree)
    readers = [int(n) for n in args.readers.split(",")]
    results = []
    for size in (int(s) for s in args.sizes.split(",")):
        with tempfile.TemporaryDirectory() as temp:
            results.append(profile(tree, size, args.repeat, readers, args.loops, Path(temp)))
        print(json.dumps(results[-1], sort_keys=True), file=sys.stderr, flush=True)
    print(json.dumps({"label": args.label, "results": results}, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
