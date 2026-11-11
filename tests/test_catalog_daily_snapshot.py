"""The first write of a UTC day replays the history once, not four times (Kata yxc2), at full strength."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tools import backups as snapshots
from tools.catalog import catalog_store as catalog
from tools.catalog.contracts import CatalogError
from tools.diagnostics.catalog_lock_benchmark import registration
from tools.intake import store


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)
        self.home, self.db, self.backups = catalog.locations(self.root)
        for n in range(3):
            self.write(n)

    def write(self, n):
        state = catalog.status(self.root)["state_token"]
        return catalog.submit(self.root, registration(n), actor="a", request_id=f"r{n}",
                              reason="test", expected_state=state)

    def new_day(self):
        for path in self.backups.iterdir():
            path.unlink()

    def snapshots(self):
        return sorted(self.backups.glob("*.sqlite3"))

    def counting(self):
        calls = []
        real = catalog.inspect

        def count(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)
        return calls, patch.object(catalog, "inspect", count)


class OneReplay(Case):
    def test_first_write_of_the_day_replays_the_history_once_with_a_receipt(self):
        self.new_day()
        calls, patched = self.counting()
        with patched:
            result = self.write(10)
        self.assertEqual(len(calls), 1)  # the live head; no copy, fresh-verify or retention replay
        self.assertIsNotNone(result["backup"])
        self.assertEqual(result["retention"]["refused"], None)

    def test_first_write_of_the_day_replays_once_without_a_receipt(self):
        self.new_day()
        pinned = catalog.status(self.root)["state_token"]
        (self.home / ".local" / "read-validation.json").unlink()
        calls, patched = self.counting()
        with patched:
            state = pinned
            result = catalog.submit(self.root, registration(10), actor="a", request_id="r10",
                                    reason="test", expected_state=state)
        self.assertEqual(len(calls), 1)  # submit's own replay stands for the snapshot's
        self.assertIsNotNone(result["backup"])

    def test_same_day_writes_replay_nothing(self):
        self.write(10)
        calls, patched = self.counting()
        with patched:
            self.write(11)
        self.assertEqual(calls, [])


class SnapshotStrength(Case):
    def test_snapshot_is_the_inspected_database_and_fully_verifies(self):
        self.new_day()
        before = self.db.read_bytes()
        result = self.write(10)
        path = Path(result["backup"]["snapshot"])
        self.assertEqual(path.read_bytes(), before)  # the pre-write state, byte for byte
        self.assertEqual(catalog.verify_backup(path)["revision"], 3)  # full replay still accepts it
        self.assertEqual(catalog.verify_backup(path), result["backup"]["state"])

    def test_receipt_matches_a_manual_snapshot_shape(self):
        self.new_day()
        daily = self.write(10)["backup"]
        manual = catalog.backup(self.root)
        read = lambda s: sorted(__import__("json").loads(Path(s["receipt"]).read_text()))
        self.assertEqual(read(daily), read(manual))

    def test_forged_receipt_over_a_tampered_catalog_still_refuses_the_first_write_of_the_day(self):
        with closing(sqlite3.connect(self.db, isolation_level=None)) as connection:
            for table in ("metadata", "revisions", "versions"):
                for action in ("update", "delete"):
                    connection.execute(f"DROP TRIGGER immutable_{table}_{action}")
            connection.execute("UPDATE versions SET payload=replace(payload,'Synthetic.','Edited.') WHERE record_id='S1'")
        os.chmod(self.db, 0o600)
        with closing(catalog.connect(self.db)) as connection:
            connection.execute("BEGIN")
            binding, state = catalog._binding(connection), catalog._head_state(connection)
        catalog._write_receipt(self.home, binding, state)
        self.new_day()
        with self.assertRaises(CatalogError):
            self.write(10)
        self.assertEqual(self.snapshots(), [])  # nothing published for a corrupt head

    def test_bytes_that_differ_after_writing_are_refused_and_nothing_is_published(self):
        self.new_day()
        revision = catalog.status(self.root)["revision"]
        with patch.object(catalog, "_partial_hash", return_value="0" * 64):
            with self.assertRaisesRegex(CatalogError, "Backup bytes mismatch"):
                self.write(10)
        self.assertEqual(self.snapshots(), [])
        self.assertEqual(list(self.backups.glob("*.partial")), [])
        self.assertEqual(catalog.status(self.root)["revision"], revision)

    def test_a_snapshot_changed_after_publication_does_not_verify(self):
        self.write(10)
        victim = self.snapshots()[0]
        raw = bytearray(victim.read_bytes())
        raw[-1] ^= 0xFF
        victim.write_bytes(bytes(raw))
        os.chmod(victim, 0o600)
        with self.assertRaises(CatalogError):
            catalog.verify_backup(victim, replay=False)  # the cheap check still compares size and hash
        with self.assertRaises(CatalogError):
            catalog.verify_backup(victim)

    def test_a_corrupt_older_snapshot_is_never_trusted_by_retention(self):
        """Retention verifies older snapshots in full; only the freshly written one skips the repeat."""
        calls = []
        real = catalog.verify_backup

        def spy(path, *args, **kwargs):
            calls.append((Path(path).name, kwargs.get("replay", True)))
            return real(path, *args, **kwargs)
        self.new_day()
        with patch.object(catalog, "verify_backup", spy):
            self.write(10)
        self.assertEqual([replay for _, replay in calls], [False])  # one cheap check, no full verification
        with patch.object(snapshots, "utcnow", lambda: snapshots.datetime.now(snapshots.timezone.utc)
                          + snapshots.timedelta(days=1)):
            calls.clear()
            with patch.object(catalog, "verify_backup", spy):
                self.write(11)
        # yesterday's snapshot is the candidate keeper only if the new one failed; it is not re-verified here
        self.assertEqual([replay for _, replay in calls], [False])


if __name__ == "__main__":
    unittest.main()
