#!/usr/bin/env python3
"""Scheduled full verification of the authority stores; reports every store that fails, changes nothing.

Reads trust the persisted validation receipts and never replay the whole history (Kata 91h5), so
this is the one routine that does: the catalog, the review events and the intake ledger are each
replayed in full, and the newest verified backup of the catalog and the review events is checked
against its receipt. A refusal is reported, not raised, so one damaged store never hides another.
The only writes are the receipts that record the new baseline after a store passes, and a store
that fails loses its review-event receipt so no later reader trusts it.

Exit status 0 when every store verified, 1 when any was flagged, 2 for a usage error. Output is one
body-free JSON object. Run it on a schedule while no intake run holds the catalog, for example::

    17 3 * * *  cd /path/to/instance && .venv/bin/python -m tools.store_audit --root . >> .local/store-audit.log 2>&1
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time

from tools import backups as snapshots


def _run(name, check):
    started = time.perf_counter()
    entry = {"store": name}
    try:
        entry.update(check())
        entry["ok"] = True
    except Exception as error:  # a store is flagged whatever the way it failed; the others still run
        entry["ok"] = False
        # The stores' own refusals are body-free by design; anything else is named by type only.
        entry["error"] = str(error) if type(error).__module__.startswith("tools.") else type(error).__name__
        entry["error_type"] = type(error).__name__
    entry["seconds"] = round(time.perf_counter() - started, 1)
    return entry


def _newest(backups, verify):
    """Verify the newest snapshot of a store whose backups are in ``backups``; None when there is none."""
    entries = [e for e in snapshots.scan(backups) if e.snapshot is not None]
    if not entries:
        return {"backup": None}
    newest = entries[-1]
    state = verify(newest.snapshot)
    return {"backup": {"snapshot": Path(newest.snapshot).name, "revision": state["revision"]}}


def audit(root, *, backups=True):
    """The report for the instance at ``root``: one entry per store, ``ok`` only if all verified."""
    from tools.catalog import catalog_store as catalog
    from tools.intake import store as ledger
    from tools.review_events import event_store as events

    def catalog_check():
        state = catalog.status(root, audit=True)
        result = {"revision": state["revision"], "records": state["records"], "state_token": state["state_token"]}
        if backups:
            result.update(_newest(catalog.locations(root)[2], catalog.verify_backup))
        return result

    def events_check():
        state = events.status(root, audit=True)
        result = {"revision": state["revision"], "events": state["events"], "state_token": state["state_token"]}
        if backups:
            result.update(_newest(events.locations(root)[2], lambda path: events.verified_snapshot(path, root)))
        return result

    def ledger_check():
        result = ledger.audit(root)
        return {"requests": result["requests"]}

    stores = [_run("catalog", catalog_check), _run("review-events", events_check)]
    try:
        present = ledger.locations(root)[2].exists()
    except Exception:
        present = True
    if present:
        stores.append(_run("intake-ledger", ledger_check))
    return {"command": "store-audit", "finished_at": datetime.now(timezone.utc).isoformat(),
            "ok": all(entry["ok"] for entry in stores), "stores": stores,
            "boundary": "Integrity audit of recorded hashes and contracts; not source verification, "
                        "and not a check of files under the instance's data roots."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--no-backups", action="store_true", help="Skip verifying the newest backup of each store")
    args = parser.parse_args(argv)
    try:
        report = audit(args.root, backups=not args.no_backups)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        print("store-audit: " + type(error).__name__, file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
