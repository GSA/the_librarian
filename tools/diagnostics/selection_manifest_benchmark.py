#!/usr/bin/env python3
"""Per-select cost of the shared selection manifest from 1k to 13k entries (synthetic, body-free).

Seeds an empty manifest in a temporary workspace, then adds ``--selections`` synthetic selections
through ``standing.write_manifest`` (what ``standing-select`` calls) and prints the mean milliseconds
per select for each window of ``--window`` selections, plus the same figure for the full read,
validate and rewrite path (``--full``) for comparison. Flat windows mean a constant-time append; use
``--tree`` to measure another checkout's tools package. Never point it at a real instance.

    python3 tools/diagnostics/selection_manifest_benchmark.py --selections 13000 --window 1000
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def entry(n):
    return {"path": f"data/extractions/.local/D{n}/{n:064x}/extraction.json", "sha256": f"{n:064x}",
            "receipt_id": f"D{n}", "format": "text", "context": ["documents/context.md"],
            "review_event_ids": [f"rev-{n}"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--selections", type=int, default=13000)
    parser.add_argument("--window", type=int, default=1000)
    parser.add_argument("--full", action="store_true", help="measure the full rewrite path instead")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.tree.resolve()))
    os.chdir(args.tree.resolve())
    from tools import policy
    from tools.context_index import renditions
    from tools.intake import standing
    renditions.MAX_SELECTIONS = max(renditions.MAX_SELECTIONS, args.selections + 1)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "config").mkdir()
        (root / "documents").mkdir()
        (root / "documents/context.md").write_text("# Context\n")
        manifest = root / "config/selection.json"
        manifest.write_text(json.dumps({"schema_version": 1, "audience": policy.for_root(root)[0]["audience"],
                                        "documents": [], "context_rules": {}, "renditions": []}))
        if args.full:
            from tools.intake import selection_manifest
            selection_manifest.append = lambda *a, **k: None
        windows, spent, began = [], 0.0, time.perf_counter()
        for n in range(1, args.selections + 1):
            start = time.perf_counter()
            standing.write_manifest(root, "config/selection.json", entry(n))
            spent += time.perf_counter() - start
            if n % args.window == 0:
                windows.append({"selections": n, "mean_ms_per_select": round(spent / args.window * 1000, 3)})
                spent = 0.0
        print(json.dumps({"mode": "full-rewrite" if args.full else "append", "windows": windows,
                          "total_s": round(time.perf_counter() - began, 2),
                          "bytes": manifest.stat().st_size}, sort_keys=True))


if __name__ == "__main__":
    main()
