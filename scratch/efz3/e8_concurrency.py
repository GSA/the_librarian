"""E8: qualify throughput vs concurrency with a stub transport that sleeps LAT seconds (no network). Shows where toolkit-serial work caps scaling."""
import json, os, sys, tempfile, time, shutil
from pathlib import Path
sys.path.insert(0, os.getcwd())
from tools.diagnostics.ingest_benchmark import fixture
from tools.intake import cohort, store
from tools.catalog import catalog_store as catalog
from tools.review_events import event_store as events
n, lat = int(sys.argv[1]), float(sys.argv[2]); concs = [int(x) for x in sys.argv[3].split(",")]
def mk(lat):
    def transport(model, messages):
        time.sleep(lat); p = json.loads(messages[-1]["content"])
        return {"content": json.dumps({"decision": "accept", "inspected_records": [r["i"] for r in p["records"]], "inspected_warnings": [w["ref"] for w in p["warnings"]],
                "contradictions": [], "missing_evidence": [], "explanation": "ok"}), "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    return transport
with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as t:
    t = Path(t); base, inbox = t/"base", t/"in"; base.mkdir()
    store.initialize(base); catalog.initialize(base); events.initialize(base)
    (base/"config").mkdir(); (base/"config/policy.local.json").write_text(json.dumps({"audience": "authorized-local"}))
    (base/"documents").mkdir(); (base/"documents/context.md").write_text("# Ctx\nctx\n")
    fixture(inbox, n, seed="c", padding=3)
    cohort.ingest(base, inbox, system="bench", actor="b")
    for c in concs:
        root = t/f"r{c}"; shutil.copytree(base, root)
        import sqlite3
        with sqlite3.connect(root/".local/intake/intake.sqlite3") as con: con.execute("UPDATE requests SET root=?", (str(root.resolve()),))
        t0 = time.perf_counter()
        rep = cohort.qualify(root, inbox, system="bench", actor="b", policy_path="config/standing-policy.local.json", manifest="config/selection.local.json",
                             model="m", transport=mk(lat), context=["documents/context.md"], concurrency=c)
        w = time.perf_counter()-t0
        print(json.dumps({"conc": c, "latency_s": lat, "docs": n, "wall_s": round(w,1), "docs_per_min": round(n/w*60,1), "ideal_per_min": round(c/lat*60,1), "q": rep["counts"]["qualification"]}), flush=True)
