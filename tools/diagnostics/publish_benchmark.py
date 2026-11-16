#!/usr/bin/env python3
"""Synthetic cohort publish benchmark: wall time of one ``cohort.publish`` at N selections.

Builds a temporary instance holding ``N`` selected text renditions (catalog registrations, retained
extractions, one standing-authorized review event each, and the manifest, all written directly so
large sizes build quickly) and times, in a fresh process per size: ``load_manifest`` (selection
validation), ``renditions.inputs`` (one call, the dominant per-publish cost) and the first and the
unchanged ``cohort.publish`` (the second one reuses the saved publication) and, last, one
``standing_publication.write_manifest`` select (the whole-manifest rewrite every selection pays). Reports body-free JSON,
including how many times ``renditions.inputs`` ran per publish and the engine's peak RSS. Synthetic
fixtures only; never point it at a real instance.

    python3 tools/diagnostics/publish_benchmark.py --sizes 5000,10000,15000
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

CONTEXT = "documents/context.md"
POLICY = "config/standing-policy.local.json"
MANIFEST = "config/selection.local.json"
BATCH = 200
REVIEWER = "usai:bench-model"


def policy_file(root):
    value = {"schema": "standing-policy-v1", "policy_id": "cohort-standing", "version": 1, "status": "active",
             "reviewer": REVIEWER, "scope": {"receipts": ["D*"], "audiences": ["source"]},
             "budget": {"max_pages": 100, "max_records": 10000, "max_findings_bytes": 1048576},
             "selection": {"context": [CONTEXT]}}
    path = root / POLICY
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(root, count):
    """Write ``count`` selections (receipts D1..Dcount) directly; return the manifest path."""
    from tools.catalog import catalog_store as catalog
    from tools.diagnostics.catalog_lock_benchmark import seed as seed_catalog
    from tools.extraction import extract
    from tools.intake import store
    from tools.review_events import event_store as events
    store.initialize(root)
    catalog.initialize(root)
    events.initialize(root)
    (root / "config").mkdir(exist_ok=True)
    (root / "config/policy.local.json").write_text(json.dumps({"audience": "authorized-local"}))
    (root / "documents").mkdir()
    (root / CONTEXT).write_text("# Cohort Context\nSynthetic cohort context.\n")
    policy_sha = policy_file(root)
    home = extract.HOME
    fingerprint = extract.fingerprint(root, "text")
    engine = {"version": extract.VERSION, "sha256": fingerprint}
    from tools import policy as toolkit_policy
    settings = toolkit_policy.for_root(root)[0]
    plans, selections, event_rows = {}, [], []
    for n in range(1, count + 1):
        raw = f"# Note {n}\nSynthetic line {n}.\nSecond line.\n".encode()
        sid = f"bench-{n}"
        original = f"sources/intake/{sid}/note.md"
        (root / original).parent.mkdir(parents=True, exist_ok=True)
        (root / original).write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        identity = {"receipt_id": f"D{n}", "artifact_id": f"A-upload-{sid}-original", "source_ids": [f"S{n}"],
                    "path": original, "sha256": sha, "bytes": len(raw), "audience": "source", "handling_refs": []}
        run = extract.digest(extract.encoded({"source": identity, "extractor": engine, "format": "text"}))
        value = {"schema_version": extract.VERSION, "run_id": run, "extractor": engine, "source": identity,
                 "boundary": extract.BOUNDARY + " " + settings["handling"],
                 "payload": extract.parse("text", raw, root, ("text", sha, fingerprint))}
        path = root / home / f"D{n}" / run / "extraction.json"
        path.parent.mkdir(parents=True)
        data = extract.encoded(value)
        path.write_bytes(data)
        path.chmod(0o600)  # as extraction writes it, whatever the umask
        rendition = path.relative_to(root).as_posix()
        rendition_sha = hashlib.sha256(data).hexdigest()
        ref = extract.reference(rendition, rendition_sha)
        sidecar = [extract.reference(f"sources/intake/{sid}/submission.json", sha)]
        parent = {"id": identity["artifact_id"], "kind": "artifact", "data": {
            "path": original, "parent_id": None, "receipt_id": f"D{n}", "role": "original", "audience": "source",
            "coverage": "deferred", "reason": "Synthetic.", "presence": "present", "sha256": sha,
            "hash_basis": "observed", "bytes": len(raw), "extraction": "existing-rendition",
            "handling_refs": [], "provenance": sidecar + [ref]}}
        child = {"id": "A-extract-" + run, "kind": "artifact", "data": {
            "path": rendition, "parent_id": parent["id"], "receipt_id": f"D{n}", "role": "generated-rendition",
            "audience": "source", "coverage": "indexed", "reason": "Synthetic selection.", "presence": "present",
            "sha256": rendition_sha, "hash_basis": "observed", "bytes": len(data), "extraction": "not-required",
            "handling_refs": [], "provenance": [extract.reference(original, sha), ref]}}
        relation = {"id": "R-extract-" + run, "kind": "relationship", "data": {
            "from_id": child["id"], "to_id": parent["id"], "relation": "rendition-of", "scope": "Synthetic.",
            "basis": "reported", "provenance": [ref]}}
        plans[n] = [{"id": f"S{n}", "kind": "source", "data": {"category": "artifact", "provenance": sidecar}},
                    {"id": f"D{n}", "kind": "receipt", "data": {
                        "source_ids": [f"S{n}"], "artifact_id": parent["id"], "provenance": sidecar}},
                    parent, child, relation]
        event_id = f"rev-bench-{n}"
        event_rows.append({
            "event_id": event_id,
            "source": {"receipt_id": f"D{n}", "source_id": f"S{n}", "locator": rendition,
                       "parent_locator": original, "checksum": {"sha256": rendition_sha, "basis": "reported"}},
            "scope": {"content": ["text"], "unit": "file", "extent": "defined_scope",
                      "coordinates": "not_applicable", "ranges": [],
                      "description": "Complete exact text extraction rendition review."},
            "outcome": "reviewed",
            "counts": {"total": 1, "reviewed": 1, "pending": 0, "unreadable": 0, "dispositioned": 0},
            "reason": "Qualified under explicit standing policy; parser completion is not verification.",
            "reviewer": REVIEWER, "reviewed_on": "2026-01-01", "recorded_at": "2026-01-01T00:00:00+00:00",
            "evidence": [{"kind": "agent_statement", "pointer": None, "quote": q} for q in (
                f"Standing policy cohort-standing v1 (sha256 {policy_sha}) authorized this exact rendition review.",
                "Inspected all 3 text extraction records and 1 extraction warnings (findings sha256 " + "0" * 64 + ").")],
            "uncertainties": [], "handling": [settings["handling"]],
            "provenance": {"origin": "agent_review", "record": None, "mapping_version": None, "unmapped_fields": []},
            "relationships": [], "supersedes": [], "correction_reason": None})
        selections.append({"path": rendition, "sha256": rendition_sha, "receipt_id": f"D{n}", "format": "text",
                           "context": [CONTEXT], "review_event_ids": [event_id]})
    seed_catalog(root, count, lambda n: plans[n + 1])
    seed_events(root, event_rows)
    manifest = {"schema_version": 1, "audience": "authorized-local", "context_rules": {},
                "documents": [{"path": CONTEXT, "role": "context", "context": []}], "renditions": selections}
    path = root / MANIFEST
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def seed_events(root, rows):
    """Append ``rows`` in batches of ``BATCH`` events, as ``event_store.append`` stores them."""
    from tools.review_events import event_store as events
    home, db, _ = events.locations(root)
    with closing(events.connect(db, write=True)) as connection:
        state, _ = events.inspect(connection, root)
        connection.execute("BEGIN IMMEDIATE")
        revision, sequence, previous = state["revision"], state["events"], state["head_hash"]
        batch_previous = state["batch_head_hash"]
        for start in range(0, len(rows), BATCH):
            incoming = rows[start:start + BATCH]
            revision += 1
            batch = {"revision": revision, "request_id": f"req-bench-{revision}", "actor": REVIEWER,
                     "committed_at": events.now(), "input_hash": events.fingerprint(incoming),
                     "event_count": len(incoming), "previous_hash": batch_previous}
            batch["batch_hash"] = batch_previous = events.fingerprint(batch)
            connection.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?,?)", tuple(batch.values()))
            for event in incoming:
                sequence += 1
                row = {"sequence": sequence, "event_id": event["event_id"], "revision": revision,
                       "origin_key": None, "payload": events.canonical(event),
                       "payload_hash": events.fingerprint(event), "previous_hash": previous}
                row["entry_hash"] = previous = events.fingerprint(row)
                connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", tuple(row.values()))
        connection.execute("COMMIT")
    events.seal(db)


CHILD = r"""
import json, os, resource, sys, time
sys.path.insert(0, sys.argv[1]); os.chdir(sys.argv[1])
from pathlib import Path
from tools.context_index import context_index as index, renditions
from tools.intake import cohort
root = Path(sys.argv[2]); policy_path, manifest = sys.argv[3], sys.argv[4]
calls = [0]
real = renditions.inputs
def counted(*a, **k):
    calls[0] += 1
    return real(*a, **k)
renditions.inputs = counted
def timed(fn):
    start = time.perf_counter(); value = fn(); return round(time.perf_counter() - start, 2), value
cpu0 = time.process_time()
out = {}
out["load_manifest_s"], (config, _) = timed(lambda: index.load_manifest(root / manifest, root))
out["inputs_s"], _ = timed(lambda: real(root, config, index.rendition_api(root)))
calls[0] = 0
out["publish_s"], report = timed(lambda: cohort.publish(root, policy_path=policy_path, manifest=manifest))
out["inputs_calls"] = calls[0]
out["published"] = report["counts"]["published"]
calls[0] = 0
out["republish_s"], again = timed(lambda: cohort.publish(root, policy_path=policy_path, manifest=manifest))
out["republish_inputs_calls"] = calls[0]
from tools.intake import standing, standing_publication
last = dict(config["renditions"][-1])
last["path"] = last["path"].rsplit("/", 2)[0] + "/" + "f" * 64 + "/extraction.json"
start = time.perf_counter()
try:
    standing_publication.write_manifest(root, manifest, last)
    out["select_write_s"] = round(time.perf_counter() - start, 3)
except standing.Refusal as refusal:
    out["select_write_s"] = refusal.code  # the manifest is already at the cap
out["publish_cpu_s"] = round(time.process_time() - cpu0, 2)
out["peak_rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
print(json.dumps(out))
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2],
                        help="checkout whose tools package is measured (fixtures are always built by this one)")
    parser.add_argument("--sizes", default="5000,10000,15000")
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    tree = args.tree.resolve()
    from _harness import use_tree
    use_tree(Path(__file__).resolve().parents[2])
    for size in (int(n) for n in args.sizes.split(",")):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "instance"
            root.mkdir()
            start = time.perf_counter()
            build(root, size)
            built = time.perf_counter() - start
            done = subprocess.run([sys.executable, "-c", CHILD, str(tree), str(root), POLICY, MANIFEST],
                                  capture_output=True, text=True)
            if done.returncode:
                sys.exit(done.stderr[-2000:])
            print(json.dumps({"label": args.label, "selections": size, "build_s": round(built, 1),
                              **json.loads(done.stdout)}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
