#!/usr/bin/env python3
"""Per-entry serial cost of the review-event store from 1k to 13k events (synthetic, body-free).

Seeds a temporary instance with synthetic pending review events in batches, then measures what one
qualify entry costs on top of ``--events`` events: the validated read (``event_reader.read_snapshot``),
an index lookup of the rendition's events, and a one-event ``event_store.append``. It prints the mean
milliseconds per entry for each window of ``--window`` entries added after reaching each size in
``--sizes``. Flat windows mean the cost does not depend on the history. ``--no-delta`` replays the
whole history on every read and append (the behaviour before the delta check) for comparison. Use
``--tree`` to measure another checkout. Never point it at a real instance.

    python3 tools/diagnostics/review_event_scale_benchmark.py --sizes 1000 4000 13000
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

ACTOR = "benchmark-reviewer"


def make(root, policy, n):
    return {
        "event_id": f"rev-scale-{n}",
        "source": {"receipt_id": "D1", "source_id": "S1", "locator": f"sources/scale-{n}.txt",
                   "parent_locator": None, "checksum": {"sha256": None, "basis": "unknown"}},
        "scope": {"content": ["text"], "unit": "file", "extent": "defined_scope",
                  "coordinates": "not_applicable", "ranges": [], "description": "Synthetic scale fixture."},
        "outcome": "pending",
        "counts": {"total": 1, "reviewed": 0, "pending": 1, "unreadable": 0, "dispositioned": 0},
        "reason": f"Synthetic reason number {n:06d} for the scale benchmark.",
        "reviewer": ACTOR, "reviewed_on": None,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "evidence": [{"kind": "agent_statement", "pointer": None, "quote": "Synthetic statement."}],
        "uncertainties": ["Synthetic fixture."], "handling": [policy.for_root(root)[0]["handling"]],
        "provenance": {"origin": "agent_review", "record": None, "mapping_version": None,
                       "unmapped_fields": []},
        "relationships": [], "supersedes": [], "correction_reason": None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 4000, 13000])
    parser.add_argument("--window", type=int, default=50)
    parser.add_argument("--batch", type=int, default=250, help="events per seeding append (at most 256)")
    parser.add_argument("--no-delta", action="store_true", help="replay the whole history on every call")
    args = parser.parse_args(argv)
    from _harness import use_tree
    use_tree(args.tree)
    from tools import policy
    from tools.catalog import catalog_store as catalog
    from tools.review_events import event_history, event_reader, event_store as events
    if args.no_delta:
        real = event_history.view

        def replaying(connection, root=None, *, reuse=False, full=False):
            return real(connection, root, reuse=reuse, full=True)
        event_history.view = replaying
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        catalog.initialize(root)
        events.initialize(root)
        total, rows = 0, []

        def add(count):
            nonlocal total
            while count > 0:
                size = min(args.batch, count)
                state = events.status(root)
                items = [make(root, policy, total + k) for k in range(size)]
                events.append(root, event_history.bundle(items), ACTOR, f"req-scale-{total}",
                              state["revision"], state["state_token"])
                total, count = total + size, count - size

        for size in args.sizes:
            add(size - total)
            spent = {"read_ms": 0.0, "lookup_ms": 0.0, "append_ms": 0.0}
            for _ in range(args.window):
                start = time.perf_counter()
                snapshot = event_reader.read_snapshot(root)
                spent["read_ms"] += time.perf_counter() - start
                start = time.perf_counter()
                snapshot.at(f"sources/scale-{total // 2}.txt")
                snapshot.event(f"rev-scale-{total // 2}")
                spent["lookup_ms"] += time.perf_counter() - start
                state = snapshot.state
                start = time.perf_counter()
                events.append(root, event_history.bundle([make(root, policy, total)]), ACTOR, f"req-scale-{total}",
                              state["revision"], state["state_token"])
                spent["append_ms"] += time.perf_counter() - start
                total += 1
            rows.append({"events": size, **{k: round(v / args.window * 1000, 3) for k, v in spent.items()}})
        print(json.dumps({"mode": "full-replay" if args.no_delta else "delta", "windows": rows}, sort_keys=True))


if __name__ == "__main__":
    main()
