"""Production-shaped synthetic seed: DOCS docs -> 3 ledger requests each, 2 catalog revisions each
(registration 4 records; extract-register 3 records). Direct SQL, no real data."""
import hashlib, json, os, sys, time, sqlite3
from contextlib import closing
from pathlib import Path
sys.path.insert(0, os.getcwd())

def seed(root, docs, ledger_pad=True):
    from tools import policy
    from tools.catalog import catalog_store as cs
    from tools.catalog.contracts import canonical, fingerprint, now
    from tools.diagnostics.catalog_lock_benchmark import registration
    from tools.intake import contracts
    policy_sha = policy.load()[1]
    t0 = time.time()
    home, db, _ = cs.locations(root)
    with closing(cs.connect(db, write=True)) as c:
        c.execute("BEGIN"); st = cs.inspect(c)[0]
        rev, prev = st["revision"], st["head_hash"]
        def put(records, rid):
            nonlocal rev, prev
            rev += 1
            b = {"revision": rev, "request_id": rid, "actor": "bench", "reason": "seed", "committed_at": now(),
                 "input_hash": fingerprint({"actor": "bench", "reason": "seed", "records": records}),
                 "record_count": len(records), "previous_hash": prev}
            b["revision_hash"] = prev = fingerprint(b)
            c.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?)", tuple(b.values()))
            for o, r in enumerate(records):
                c.execute("INSERT INTO versions VALUES (?,?,?,?,?)", (rev, o, r["id"], canonical(r), fingerprint(r)))
                c.execute("INSERT INTO current_records VALUES (?,?,?) ON CONFLICT(record_id) DO UPDATE SET revision=excluded.revision, ordinal=excluded.ordinal", (r["id"], rev, o))
        for n in range(docs):
            recs = registration(n); put(recs, f"upload-s{n}")
            orig = json.loads(json.dumps(recs[2]))
            sha = hashlib.sha256(f"run{n}".encode()).hexdigest()
            ref = {"path": f"data/extractions/.local/D{n}/{sha}/extraction.json", "sha256": sha, "line_start": None, "line_end": None}
            orig["data"]["provenance"].append(ref); orig["data"]["extraction"] = "existing-rendition"; orig["data"]["coverage"] = "deferred"
            child = {"id": "A-extract-" + sha, "kind": "artifact", "data": {
                "path": ref["path"], "parent_id": orig["id"], "receipt_id": f"D{n}", "role": "generated-rendition",
                "audience": "source", "coverage": "deferred", "reason": "Machine text rendition; synthetic.",
                "presence": "present", "sha256": sha, "hash_basis": "observed", "bytes": 5000, "extraction": "not-required",
                "handling_refs": [], "provenance": [ref, ref]}}
            rel = {"id": "R-extract-" + sha, "kind": "relationship", "data": {"from_id": child["id"], "to_id": orig["id"],
                   "relation": "rendition-of", "scope": "Machine text-only rendition; synthetic.", "basis": "reported", "provenance": [ref]}}
            put([orig, child, rel], f"extract-D{n}-{sha}")
        c.execute("COMMIT"); cs.seal(db)
    t1 = time.time()
    if ledger_pad:
        base = {"version": 1, "correction_of": None, "policy": {"version": 1, "sha256": policy_sha},
                "retry": {"max_attempts": 3, "backoff_seconds": 0}}
        with closing(sqlite3.connect(root / ".local/intake/intake.sqlite3")) as con, con:
            def ins(corr, stage, kind, op, args, intent, evidence):
                raw = json.dumps({**base, "correlation_id": corr, "stages": [{"id": stage, "kind": kind, "operation": op, "arguments": args}]}).encode()
                i = json.dumps(intent, sort_keys=True); e = json.dumps(evidence, sort_keys=True)
                con.execute("INSERT INTO requests VALUES (?,?,?,?,?)", (corr, None, raw, contracts.digest(raw), str(root)))
                con.execute("INSERT INTO stages VALUES (?,?,?,?,?,?,?,?,?)", (corr, stage, 0, "succeeded", 0, i, contracts.digest(i.encode()), e, contracts.digest(e.encode())))
                con.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?)", (corr, stage, 0, 1, 1.0, 2.0, "succeeded", None, None, None))
            for n in range(docs):
                sid = hashlib.sha256(str(n).encode()).hexdigest()[:24]
                recs = registration(n)
                ins(f"upload-inbox-{sid}", "receipt", "receipt", "catalog-submit",
                    {"records": recs, "actor": "bench", "request_id": f"upload-inbox-{sid}", "expected_state": "0"*64, "reason": "Manual upload: synthetic"},
                    {"operation": "catalog-submit"}, {"boundary": "receipt-committed", "committed_revision": 2*n+1, "request_id": f"upload-inbox-{sid}",
                      "record_ids": [r["id"] for r in recs], "receipt_ids": [f"D{n}"], "committed_state": {"x": "y"*300}, "backup": None})
                ins(f"extract-inbox-{sid}", "prepare", "extraction", "extract-prepare", {"receipt": f"D{n}", "format_name": "text"},
                    {"operation": "extract-prepare", "run_id": "r"*64, "source": {"p": "x"*300}, "rendition": "data/x", "extractor": {"v": 1, "sha256": "e"*64}},
                    {"boundary": "extraction-prepare", "run_id": "r"*64, "rendition": "data/x", "rendition_sha256": "s"*64, "source_sha256": "s"*64})
                ins(f"register-inbox-{sid}", "register", "extraction", "extract-register",
                    {"receipt": f"D{n}", "format_name": "text", "actor": "bench", "expected_state": "0"*64},
                    {"operation": "extract-register", "run_id": "r"*64, "source": {"p": "x"*300}, "rendition": "data/x", "extractor": {"v": 1, "sha256": "e"*64}},
                    {"boundary": "extraction-register", "run_id": "r"*64, "rendition": "data/x", "rendition_sha256": "s"*64, "source_sha256": "s"*64, "request_id": "q"*50})
    return {"catalog_s": round(t1 - t0, 1), "ledger_s": round(time.time() - t1, 1)}

if __name__ == "__main__":
    from tools.catalog import catalog_store as catalog
    from tools.intake import store
    dest = Path(sys.argv[1]); docs = int(sys.argv[2])
    dest.mkdir(parents=True); store.initialize(dest); catalog.initialize(dest)
    print(seed(dest, docs))
    for p in ((".local/catalog/catalog.sqlite3"), (".local/intake/intake.sqlite3")):
        print(p, (dest / p).stat().st_size >> 20, "MB")
