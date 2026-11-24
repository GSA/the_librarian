"""The claim-corrections store follows the catalog and review-event backup rules (Kata 6jwh)."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import backups
from tools.catalog import catalog_store as catalog
from tools.review_events import corrections, event_store as events
from tools.review_events.review_events import ReviewError

ROOT = Path(__file__).resolve().parents[1]
DAY = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
CLAIM = "Synthetic claim."
TARGET = {"locator": "documents/a.md#heading", "path": "documents/a.md", "line_start": 1, "line_end": 2,
          "source_hash": "a" * 64, "receipt_id": "D1", "claim": CLAIM, "claim_sha256": corrections.claim_hash(CLAIM)}
QUESTION = {"type": "question", "question": "Synthetic question?", "answer": "Synthetic answer.",
            "answered_by": "synthetic-officer", "answered_on": "2024-05-01"}


class Clock:
    def __init__(self):
        self.value = DAY

    def __call__(self):
        return self.value

    def advance(self, **delta):
        self.value += timedelta(**delta)


def record(root, request_id):
    """Append one synthetic dispute; the index and catalog lookups are replaced by fixed values."""
    drafts = [{"kind": "dispute", "locator": TARGET["locator"], "claim": CLAIM, "summary": "Synthetic summary.",
               "source": QUESTION}]
    with patch.object(corrections, "_locate", lambda *args: dict(TARGET)):
        return corrections.append(root, drafts, "test-runner", request_id,
                                  corrections.status(root)["state_token"])


class Case(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.clock = Clock()
        patcher = patch.object(backups, "utcnow", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        catalog.initialize(self.root)
        events.initialize(self.root)
        corrections.initialize(self.root)
        self.home, self.db, self.backups = corrections.locations(self.root)

    def files(self, suffix=".sqlite3"):
        return sorted(self.backups.glob("*" + suffix))

    def run_backups(self, *args):
        done = subprocess.run([sys.executable, "-m", "tools.backups", "--root", str(self.root), *args],
                              cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)


class DailySnapshots(Case):
    def test_first_write_of_a_new_day_snapshots_once_and_old_ones_age_out(self):
        self.assertEqual(len(self.files()), 1)  # init covers the day
        self.assertIsNone(record(self.root, "req-0")["backup"])
        for day in range(1, 10):
            self.clock.advance(days=1)
            first = record(self.root, f"req-{day}")
            self.assertEqual(first["backup"]["revision"], 2 * day - 1)  # taken before the day's first write
            self.assertIsNone(record(self.root, f"req-{day}-again")["backup"])
        self.assertEqual(len(self.files()), 8)  # today plus the preceding 7 days
        self.assertEqual(len(self.files(".json")), 8)
        for path in self.files():
            corrections.verified_snapshot(path)

    def test_retention_never_deletes_the_last_verified_snapshot(self):
        self.clock.advance(days=30)
        record(self.root, "req-0")
        newest = self.files()[-1]
        raw = bytearray(newest.read_bytes())
        raw[-1] ^= 0xFF
        newest.write_bytes(bytes(raw))
        before = set(self.backups.iterdir())
        report = corrections.prune(self.root, apply=True)
        self.assertIn("no verified snapshot", report["refused"])
        self.assertEqual(set(self.backups.iterdir()), before)

    def test_corruption_then_retention_keeps_the_clean_snapshot(self):
        """A corruption copied into each new snapshot cannot age out the last clean one."""
        clean = self.files()[0]
        record(self.root, "req-0")
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("DROP TRIGGER immutable_corrections_update")
            connection.execute("UPDATE corrections SET payload_hash='0'")
            connection.commit()
        os.chmod(self.db, 0o600)
        for day in range(1, 10):
            self.clock.advance(days=1)
            with self.assertRaises(ReviewError):
                record(self.root, f"req-{day}")  # the damaged store refuses; nothing is deleted
        self.assertTrue(clean.exists())
        corrections.verified_snapshot(clean)


class OperatorTools(Case):
    def test_inventory_and_prune_dry_run_then_apply(self):
        record(self.root, "req-0")
        for _ in range(2):
            corrections.backup(self.root)
        inventory = corrections.backup_inventory(self.root)
        self.assertEqual((inventory["snapshots"], inventory["oldest_revision"]), (3, 0))
        dry = corrections.prune(self.root)
        self.assertEqual((dry["applied"], dry["deleted"]), (False, 2))
        self.assertEqual(len(self.files()), 3)
        applied = corrections.prune(self.root, apply=True)
        self.assertEqual((applied["applied"], applied["deleted"]), (True, 2))
        self.assertEqual(len(self.files()), 1)

    def test_prune_with_nothing_verified_deletes_nothing(self):
        self.clock.advance(days=30)
        report = corrections.prune(self.root, apply=True)
        self.assertIsNotNone(report["refused"])
        self.assertEqual(len(self.files()), 1)

    def test_tools_backups_cli_skips_a_store_that_was_never_initialized(self):
        self.db.unlink()
        self.assertEqual(set(self.run_backups("status")), {"command", "catalog", "review-events"})

    def test_tools_backups_cli_covers_the_store(self):
        self.assertEqual(set(self.run_backups("status")), {"command", "catalog", "review-events", "claim-corrections"})
        self.clock.value = datetime.now(timezone.utc)  # the subprocess uses the real clock
        corrections.backup(self.root)
        self.assertEqual(self.run_backups("status")["claim-corrections"]["snapshots"], 2)
        report = self.run_backups("prune")
        self.assertFalse(report["claim-corrections"]["applied"])
        only = self.run_backups("prune", "--store", "claim-corrections", "--apply")
        self.assertEqual(set(only), {"command", "claim-corrections"})
        self.assertEqual(only["claim-corrections"]["deleted"], 1)
        self.assertEqual(len(self.files()), 1)


class Durability(Case):
    def test_initialize_sets_synchronous_full_before_creating_the_schema(self):
        statements = []
        real = sqlite3.connect

        def spy(*args, **kwargs):
            connection = real(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection
        self.db.unlink()
        for path in self.backups.iterdir():
            path.unlink()
        with patch.object(sqlite3, "connect", spy):
            corrections.initialize(self.root)
        full = next(i for i, s in enumerate(statements) if s.replace(" ", "").lower() == "pragmasynchronous=full")
        create = next(i for i, s in enumerate(statements) if "CREATE TABLE" in s)
        self.assertLess(full, create)


if __name__ == "__main__":
    unittest.main()
