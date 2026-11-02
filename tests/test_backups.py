"""Daily snapshots and bounded retention shared by the catalog and review-event stores."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import backups, policy
from tools.catalog import catalog_store as catalog
from tools.review_events import event_store as events
from tests.test_kit import WorkspaceCase

DAY = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, value=DAY):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **delta):
        self.value += timedelta(**delta)


def source(identifier):
    return {"id": identifier, "kind": "source", "data": {"category": "artifact", "provenance": []}}


class DailySnapshots(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.clock = Clock()
        patcher = patch.object(backups, "utcnow", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        catalog.initialize(self.root)
        self.backups = catalog.locations(self.root)[2]

    def files(self, suffix=".sqlite3"):
        return sorted(self.backups.glob("*" + suffix))

    def write(self, name):
        state = catalog.status(self.root)["state_token"]
        return catalog.submit(self.root, [source(name)], actor="t", request_id="req-" + name,
                              expected_state=state, reason="Daily snapshot test")

    def test_many_writes_in_a_day_take_no_extra_snapshot(self):
        self.assertEqual(len(self.files()), 1)  # initialisation covers the day
        results = [self.write(f"S{i}") for i in range(5)]
        self.assertTrue(all(r["backup"] is None for r in results))
        self.assertEqual(len(self.files()), 1)

    def test_first_write_of_a_new_day_snapshots_the_prior_state_once(self):
        self.write("S1")
        self.clock.advance(days=1)
        first = self.write("S2")
        second = self.write("S3")
        self.assertIsNotNone(first["backup"])
        self.assertEqual(first["backup"]["state"]["revision"], 1)  # taken before the write
        self.assertIsNone(second["backup"])
        self.assertEqual(len(self.files()), 2)
        catalog.verify_backup(Path(first["backup"]["snapshot"]))

    def test_idempotent_retry_takes_no_snapshot_even_on_a_new_day(self):
        self.write("S1")
        self.clock.advance(days=1)
        retry = catalog.submit(self.root, [source("S1")], actor="t", request_id="req-S1",
                               expected_state="stale", reason="Daily snapshot test")
        self.assertTrue(retry["idempotent"])
        self.assertIsNone(retry["backup"])
        self.assertEqual(len(self.files()), 1)

    def test_corrupt_todays_snapshot_does_not_count(self):
        self.files()[0].write_bytes(b"damaged")
        os.chmod(self.files()[0], 0o600)
        self.assertIsNotNone(self.write("S1")["backup"])

    def test_same_length_corruption_does_not_satisfy_the_daily_gate(self):
        path = self.files()[0]
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 0xFF
        path.write_bytes(bytes(raw))
        self.assertIsNotNone(self.write("S1")["backup"])

    def test_snapshots_older_than_the_window_are_pruned_after_the_new_one(self):
        self.write("S1")
        for _ in range(9):
            self.clock.advance(days=1)
            self.write("S%d" % self.clock.value.day)
        names = [catalog.verify_backup(p) for p in self.files()]
        self.assertEqual(len(names), 8)  # today plus the preceding 7 days
        ages = {backups.scan(self.backups)[0].created}
        self.assertGreaterEqual(min(ages), self.clock.value - timedelta(days=7))
        self.assertEqual(len(self.files(".json")), 8)

    def test_failed_verification_of_the_new_snapshot_deletes_nothing(self):
        self.write("S1")
        self.clock.advance(days=30)
        before = set(self.backups.iterdir())
        with patch.object(catalog, "verify_backup", side_effect=catalog.CatalogError("bad")):
            result = self.write("S2")
        self.assertIsNotNone(result["backup"])
        self.assertIn("refused", result["retention"])
        self.assertTrue(before <= set(self.backups.iterdir()))

    def test_never_deletes_the_only_verified_snapshot(self):
        self.clock.advance(days=30)
        report = catalog.prune(self.root, apply=True)
        self.assertIsNotNone(report["refused"])
        self.assertEqual(len(self.files()), 1)

    def test_partial_leftovers_and_orphan_receipts_age_out(self):
        self.write("S1")
        old = self.clock.value - timedelta(days=9)
        leftover = self.backups / "x.partial"
        leftover.write_bytes(b"x")
        os.chmod(leftover, 0o600)
        os.utime(leftover, (old.timestamp(), old.timestamp()))
        self.clock.advance(days=1)
        self.write("S2")
        self.assertFalse(leftover.exists())

    def test_prune_defaults_to_dry_run_and_apply_keeps_one_verified_per_day(self):
        self.write("S1")
        for _ in range(2):
            self.clock.advance(days=1)
            self.write("S%d" % self.clock.value.day)
            catalog.backup(self.root)
            catalog.backup(self.root)
        total = len(self.files())
        dry = catalog.prune(self.root)
        self.assertFalse(dry["applied"])
        self.assertGreater(dry["deleted"], 0)
        self.assertEqual(len(self.files()), total)
        applied = catalog.prune(self.root, apply=True)
        self.assertEqual(applied["deleted"], dry["deleted"])
        self.assertEqual(len(self.files()), 3)
        self.assertEqual(len({backups.scan(self.backups)[i].created.date() for i in range(3)}), 3)
        self.assertEqual(catalog.prune(self.root, apply=True)["deleted"], 0)

    def test_prune_accepts_hand_pruned_legacy_state(self):
        self.write("S1")
        legacy = self.backups / "legacy-r00000001-post-write-abc"
        for suffix in (".sqlite3", ".json"):
            legacy.with_suffix(suffix).write_bytes(self.files(suffix)[0].read_bytes())
            os.chmod(legacy.with_suffix(suffix), 0o600)
        report = catalog.prune(self.root, apply=True)
        self.assertIsNone(report["refused"])
        self.assertEqual(catalog.backup_inventory(self.root)["snapshots"], 1)

    def test_protected_snapshot_survives_prune(self):
        self.write("S1")
        old = self.files()[0]
        self.clock.advance(days=30)
        self.write("S2")
        catalog.backup(self.root)
        self.assertFalse(old.exists())
        stale = self.files()[0]
        self.clock.advance(days=30)
        store_id = catalog.status(self.root)["store_id"]
        backups.prune(self.backups, store_id=store_id, verify=catalog.verify_backup,
                      errors=(catalog.CatalogError,), apply=True, protect=[stale])
        self.assertTrue(stale.exists())

    def test_restore_from_a_retained_snapshot(self):
        self.write("S1")
        snapshot = Path(catalog.backup(self.root)["snapshot"])
        expected = catalog.status(self.root)
        self.write("S2")
        restored = catalog.restore(self.root, snapshot, catalog.status(self.root)["state_token"])
        self.assertEqual(restored["state_token"], expected["state_token"])

    def test_inventory_reports_count_size_and_oldest(self):
        self.write("S1")
        report = catalog.backup_inventory(self.root)
        self.assertEqual(report["snapshots"], 1)
        self.assertGreater(report["total_bytes"], 0)
        self.assertEqual(report["oldest_revision"], 0)

    def test_cli_status_and_prune_dry_run(self):
        import subprocess, sys
        ROOT = Path(__file__).resolve().parents[1]
        events.initialize(self.root)
        def run(*args):
            done = subprocess.run([sys.executable, "-m", "tools.backups", "--root", str(self.root), *args],
                                  cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            return json.loads(done.stdout)
        self.assertEqual(run("status")["catalog"]["snapshots"], 1)
        report = run("prune")
        self.assertFalse(report["catalog"]["applied"])
        self.assertEqual(set(report), {"command", "catalog", "review-events"})

    def test_retention_window_policy(self):
        self.assertEqual(policy.backup_retention_days(self.root), 7)
        (self.root / "config").mkdir()
        local = self.root / "config/policy.local.json"
        for value, ok in ((30, True), (0, False), (-1, False), ("7", False), (True, False)):
            local.write_text(json.dumps({"backup_retention_days": value}))
            if ok:
                self.assertEqual(policy.backup_retention_days(self.root), value)
            else:
                with self.assertRaises(ValueError):
                    policy.backup_retention_days(self.root)


class ReviewEventSnapshots(WorkspaceCase):
    def test_review_events_follow_the_same_daily_rule(self):
        clock = Clock()
        with patch.object(backups, "utcnow", clock):
            self.registration()
            state = events.initialize(self.root)
            folder = events.locations(self.root)[2]
            one = self.pending()
            first = events.append(self.root, events.bundle([one]), "test-runner", "req-one",
                                  0, state["state_token"])
            self.assertIsNone(first["backup"])
            self.assertEqual(len(list(folder.glob("*.sqlite3"))), 1)
            clock.advance(days=1)
            two = {**one, "event_id": "rev-two", "source": {**one["source"], "locator": "sources/other.txt"}}
            second = events.append(self.root, events.bundle([two]), "test-runner", "req-two",
                                   1, first["state_token"])
            self.assertEqual(second["backup"]["revision"], 1)
            self.assertEqual(len(list(folder.glob("*.sqlite3"))), 2)
            retry = events.append(self.root, events.bundle([two]), "test-runner", "req-two",
                                  1, first["state_token"])
            self.assertIsNone(retry["backup"])
            clock.advance(days=30)
            report = events.prune(self.root)
            self.assertIsNotNone(report["refused"])
            events.backup(self.root)
            self.assertEqual(events.prune(self.root)["deleted"], 2)
            self.assertFalse(events.prune(self.root, apply=True)["applied"] is False)
            self.assertEqual(events.backup_inventory(self.root)["snapshots"], 1)


if __name__ == "__main__":
    unittest.main()
