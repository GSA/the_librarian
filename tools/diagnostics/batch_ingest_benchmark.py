#!/usr/bin/env python3
"""Synthetic first-ingest benchmark for catalog registration batching (Kata herq).

Seeds a production-shaped instance with ``--seed`` documents already ingested (two catalog revisions and
three ledger requests each, written directly so large seeds build quickly), copies it, and ingests
``--files`` new synthetic documents through ``tools.intake.cohort.ingest``, once per ``--batch-sizes``
entry (1 is the one-revision-per-registration path; a tree without ``batch_size`` runs as 1). Reports body-free
JSON per run: wall and CPU ms per document, fsync and fdatasync calls per document (counted by a small
LD_PRELOAD shim compiled with gcc when available), catalog revisions written and bytes written per
document. Synthetic fixtures in temporary workspaces only; never point it at a real instance.

    nice python3 tools/diagnostics/batch_ingest_benchmark.py --seed 2000 --files 100 --batch-sizes 1,25,50

``--tree`` measures another checkout (for example an ``origin/main`` worktree) on the same seed.
``--keep-seed DIR`` keeps the seeded instance for later runs of the same size.
"""
import argparse
import ctypes
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing

SHIM = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
static unsigned long n_fsync, n_fdatasync;
static int (*r_fsync)(int); static int (*r_fdatasync)(int);
__attribute__((constructor)) static void init(void){ r_fsync=dlsym(RTLD_NEXT,"fsync"); r_fdatasync=dlsym(RTLD_NEXT,"fdatasync"); }
int fsync(int fd){ n_fsync++; return r_fsync(fd); }
int fdatasync(int fd){ n_fdatasync++; return r_fdatasync(fd); }
unsigned long shim_fsync(void){ return n_fsync; }
unsigned long shim_fdatasync(void){ return n_fdatasync; }
"""


def compile_shim(directory):
    """Path of a compiled counting shim, or None when no compiler is available."""
    source, library = directory / "shim.c", directory / "libshim.so"
    source.write_text(SHIM)
    try:
        subprocess.run(["gcc", "-shared", "-fPIC", "-O2", "-o", str(library), str(source), "-ldl"],
                       check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return library


def seed(root, documents):
    """Append ``documents`` ingested documents: two catalog revisions and three ledger requests each."""
    from tools import policy
    from tools.catalog import catalog_store as cs
    from tools.catalog.contracts import canonical, fingerprint, now
    from tools.diagnostics.catalog_lock_benchmark import registration
    from tools.intake import contracts
    policy_sha = policy.load()[1]
    home, db, _ = cs.locations(root)
    with closing(cs.connect(db, write=True)) as c:
        c.execute("BEGIN")
        state = cs.inspect(c)[0]
        revision, previous = state["revision"], state["head_hash"]

        def put(records, request_id):
            nonlocal revision, previous
            revision += 1
            batch = {"revision": revision, "request_id": request_id, "actor": "bench", "reason": "seed",
                     "committed_at": now(),
                     "input_hash": fingerprint({"actor": "bench", "reason": "seed", "records": records}),
                     "record_count": len(records), "previous_hash": previous}
            batch["revision_hash"] = previous = fingerprint(batch)
            c.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?)", tuple(batch.values()))
            for ordinal, record in enumerate(records):
                c.execute("INSERT INTO versions VALUES (?,?,?,?,?)",
                          (revision, ordinal, record["id"], canonical(record), fingerprint(record)))
                c.execute("INSERT INTO current_records VALUES (?,?,?) ON CONFLICT(record_id) DO UPDATE "
                          "SET revision=excluded.revision, ordinal=excluded.ordinal", (record["id"], revision, ordinal))
        for n in range(documents):
            records = registration(n)
            put(records, f"upload-seed{n}")
            original = json.loads(json.dumps(records[2]))
            sha = hashlib.sha256(f"run{n}".encode()).hexdigest()
            ref = {"path": f"data/extractions/.local/D{n}/{sha}/extraction.json", "sha256": sha,
                   "line_start": None, "line_end": None}
            original["data"]["provenance"].append(ref)
            original["data"].update(extraction="existing-rendition", coverage="deferred")
            child = {"id": "A-extract-" + sha, "kind": "artifact", "data": {
                "path": ref["path"], "parent_id": original["id"], "receipt_id": f"D{n}", "role": "generated-rendition",
                "audience": "source", "coverage": "deferred", "reason": "Machine text rendition; synthetic.",
                "presence": "present", "sha256": sha, "hash_basis": "observed", "bytes": 5000,
                "extraction": "not-required", "handling_refs": [], "provenance": [ref, ref]}}
            relation = {"id": "R-extract-" + sha, "kind": "relationship", "data": {
                "from_id": child["id"], "to_id": original["id"], "relation": "rendition-of",
                "scope": "Machine text-only rendition; synthetic.", "basis": "reported", "provenance": [ref]}}
            put([original, child, relation], f"extract-D{n}-{sha}")
        c.execute("COMMIT")
        cs.seal(db)
    base = {"version": 1, "correction_of": None, "policy": {"version": 1, "sha256": policy_sha},
            "retry": {"max_attempts": 3, "backoff_seconds": 0}}
    with closing(sqlite3.connect(root / ".local/intake/intake.sqlite3")) as connection, connection:
        def add(correlation, stage, kind, operation, arguments, intent, evidence):
            raw = json.dumps({**base, "correlation_id": correlation, "stages": [{
                "id": stage, "kind": kind, "operation": operation, "arguments": arguments}]}).encode()
            intent_text, evidence_text = json.dumps(intent, sort_keys=True), json.dumps(evidence, sort_keys=True)
            connection.execute("INSERT INTO requests VALUES (?,?,?,?,?)",
                               (correlation, None, raw, contracts.digest(raw), str(root)))
            connection.execute("INSERT INTO stages VALUES (?,?,?,?,?,?,?,?,?)",
                               (correlation, stage, 0, "succeeded", 0, intent_text, contracts.digest(intent_text.encode()),
                                evidence_text, contracts.digest(evidence_text.encode())))
            connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (correlation, stage, 0, 1, 1.0, 2.0, "succeeded", None, None, None))
        for n in range(documents):
            sid = hashlib.sha256(str(n).encode()).hexdigest()[:24]
            records = registration(n)
            add(f"upload-inbox-{sid}", "receipt", "receipt", "catalog-submit",
                {"records": records, "actor": "bench", "request_id": f"upload-inbox-{sid}",
                 "expected_state": "0" * 64, "reason": "Manual upload: synthetic"},
                {"operation": "catalog-submit"},
                {"boundary": "receipt-committed", "committed_revision": 2 * n + 1,
                 "request_id": f"upload-inbox-{sid}", "record_ids": [r["id"] for r in records],
                 "receipt_ids": [f"D{n}"], "committed_state": {"x": "y" * 300}, "backup": None})
            add(f"extract-inbox-{sid}", "prepare", "extraction", "extract-prepare",
                {"receipt": f"D{n}", "format_name": "text"},
                {"operation": "extract-prepare", "run_id": "r" * 64, "source": {"p": "x" * 300},
                 "rendition": "data/x", "extractor": {"v": 1, "sha256": "e" * 64}},
                {"boundary": "extraction-prepare", "run_id": "r" * 64, "rendition": "data/x",
                 "rendition_sha256": "s" * 64, "source_sha256": "s" * 64})
            add(f"register-inbox-{sid}", "register", "extraction", "extract-register",
                {"receipt": f"D{n}", "format_name": "text", "actor": "bench", "expected_state": "0" * 64},
                {"operation": "extract-register", "run_id": "r" * 64, "source": {"p": "x" * 300},
                 "rendition": "data/x", "extractor": {"v": 1, "sha256": "e" * 64}},
                {"boundary": "extraction-register", "run_id": "r" * 64, "rendition": "data/x",
                 "rendition_sha256": "s" * 64, "source_sha256": "s" * 64, "request_id": "q" * 50})


def build_seed(tree, destination, documents):
    sys.path.insert(0, str(tree))
    os.chdir(tree)
    from tools.catalog import catalog_store as catalog
    from tools.intake import store
    destination.mkdir(parents=True)
    store.initialize(destination)
    catalog.initialize(destination)
    start = time.time()
    seed(destination, documents)
    return round(time.time() - start, 1)


def measure(args):
    """Child process: ingest ``--files`` documents into a copy of the seed and print one JSON line."""
    sys.path.insert(0, str(args.tree))
    os.chdir(args.tree)
    from tools.catalog import catalog_store as catalog
    from tools.diagnostics.ingest_benchmark import fixture
    from tools.intake import cohort, store
    shim = None
    if args.shim:
        shim = ctypes.CDLL(args.shim)
        shim.shim_fsync.restype = shim.shim_fdatasync.restype = ctypes.c_ulong
    with tempfile.TemporaryDirectory(dir=args.work) as temp:
        temp = Path(temp)
        root, inbox = temp / "instance", temp / "inbox"
        shutil.copytree(args.seed_dir, root)
        with closing(sqlite3.connect(root / ".local/intake/intake.sqlite3")) as c, c:
            c.execute("UPDATE requests SET root=?", (str(root.resolve()),))
        fixture(inbox, args.files, seed="batch-bench", padding=args.padding, mixed="mixed")
        catalog.status(root, audit=True)  # the first write of a UTC day replays the history; not counted
        store.inspect(root, "upload-inbox-" + hashlib.sha256(b"0").hexdigest()[:24]) if args.seed else None
        revisions = lambda: sqlite3.connect(root / ".local/catalog/catalog.sqlite3").execute(
            "SELECT count(*) FROM revisions").fetchone()[0]
        before_revisions, before_io = revisions(), open("/proc/self/io").read()
        sync0 = (shim.shim_fsync(), shim.shim_fdatasync()) if shim else None
        start, cpu = time.perf_counter(), time.process_time()
        parameters = inspect.signature(cohort.ingest).parameters
        kwargs = {"batch_size": args.batch_size} if "batch_size" in parameters else {}
        report = cohort.ingest(root, inbox, system="bench", actor="bench", **kwargs)
        wall, cpu = time.perf_counter() - start, time.process_time() - cpu
        sync1 = (shim.shim_fsync(), shim.shim_fdatasync()) if shim else None
        io = dict(line.split(": ") for line in open("/proc/self/io").read().splitlines())
        io0 = dict(line.split(": ") for line in before_io.splitlines())
        files = args.files
        result = {"tree": args.tree.name, "batch_size": args.batch_size if kwargs else 1, "seed": args.seed,
                  "files": files, "outcome": {k: v for k, v in report["counts"]["outcome"].items() if v},
                  "ms_per_doc": round(wall / files * 1000, 1), "cpu_ms_per_doc": round(cpu / files * 1000, 1),
                  "fsync_per_doc": None if not shim else round((sync1[0] - sync0[0]) / files, 2),
                  "fdatasync_per_doc": None if not shim else round((sync1[1] - sync0[1]) / files, 2),
                  "catalog_revisions_per_doc": round((revisions() - before_revisions) / files, 3),
                  "kib_written_per_doc": round((int(io["write_bytes"]) - int(io0["write_bytes"])) / files / 1024, 1)}
        catalog.status(root, audit=True)  # the result must replay in full
        print("RESULT " + json.dumps(result, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--seed", type=int, default=2000, help="documents already ingested")
    parser.add_argument("--files", type=int, default=100, help="new documents to ingest")
    parser.add_argument("--batch-sizes", default="1,25", help="comma separated; 1 is one revision per registration")
    parser.add_argument("--padding", type=int, default=0, help="filler lines (about 70 bytes) per document")
    parser.add_argument("--keep-seed", type=Path, help="build or reuse the seeded instance under this directory")
    parser.add_argument("--work", type=Path, help="parent directory of the per-run copies")
    parser.add_argument("--seed-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--batch-size", type=int, default=1, help=argparse.SUPPRESS)
    parser.add_argument("--shim", help=argparse.SUPPRESS)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.tree = args.tree.resolve()
    if args.child:
        measure(args)
        return
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        shim = compile_shim(scratch)
        seed_dir = (args.keep_seed or scratch) / f"seed-{args.seed}"
        if not seed_dir.exists():
            print(f"seeded {args.seed} documents in {build_seed(Path(__file__).resolve().parents[2], seed_dir, args.seed)} s",
                  file=sys.stderr)
        results = []
        for size in (int(s) for s in args.batch_sizes.split(",")):
            command = [sys.executable, str(Path(__file__).resolve()), "--child", "--tree", str(args.tree),
                       "--seed", str(args.seed), "--files", str(args.files), "--padding", str(args.padding),
                       "--batch-size", str(size), "--seed-dir", str(seed_dir)]
            if args.work:
                command += ["--work", str(args.work)]
            environment = dict(os.environ, **({"LD_PRELOAD": str(shim)} if shim else {}))
            if shim:
                command += ["--shim", str(shim)]
            done = subprocess.run(command, env=environment, capture_output=True, text=True)
            line = next((l for l in done.stdout.splitlines() if l.startswith("RESULT ")), None)
            if line is None:
                raise SystemExit(done.stderr[-2000:])
            results.append(json.loads(line[7:]))
            print(line[7:], file=sys.stderr, flush=True)
    print(json.dumps({"results": results}, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
