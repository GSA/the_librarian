"""Catalog receipts bind to file identity and never expire (Kata 91h5; docs/design/current-state.md, "What a catalog write validates")."""
from contextlib import closing
import datetime
import json
from pathlib import Path
import os
from unittest.mock import patch

from tools import backups as snapshots
from tools.catalog import catalog_store as catalog
from tools.catalog.contracts import CatalogError
from tests.test_catalog_delta_commit import Case
from tests.test_file_identity_locks import needs_dbpage
from tools.diagnostics.catalog_lock_benchmark import registration as catalog_records


class IdentityBinding(Case):
    def setUp(self):
        super().setUp()
        for n in range(3):
            self.write(n)

    def saved(self):
        return json.loads(self.receipt.read_text())

    def refused(self):
        with self.assertRaises(CatalogError):
            self.write(10)
        with self.assertRaises(CatalogError):
            catalog.status(self.root)

    def raw_edit(self, restore_mtime):
        """Same-size in-place edit of the file, outside SQLite and the writer."""
        before = os.stat(self.db)
        data = self.db.read_bytes()
        at = data.index(b"Synthetic.")
        fd = os.open(self.db, os.O_RDWR)
        os.pwrite(fd, b"Syntheticx", at)
        if restore_mtime:
            os.utime(fd, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.close(fd)

    def day_after(self):
        later = snapshots.utcnow() + datetime.timedelta(days=1)
        return patch.object(snapshots, "utcnow", lambda: later)

    @needs_dbpage
    def test_receipt_is_identity_bound_and_a_hit_replays_nothing(self):
        self.assertEqual(self.saved()["binding"]["version"], 2)
        calls, patched = self.writes_inspect()
        with patched, closing(catalog.connect(self.db)) as connection:
            connection.execute("BEGIN")
            catalog.validated_state(connection, self.home)
        self.assertEqual(calls, [])

    def test_raw_same_size_edit_is_refused(self):
        self.raw_edit(restore_mtime=False)
        self.refused()

    def test_raw_edit_with_restored_mtime_is_refused_because_utime_moves_ctime(self):
        self.raw_edit(restore_mtime=True)
        self.refused()

    def test_edit_through_sqlite_is_refused(self):
        self.mutate("UPDATE versions SET payload_hash='0' WHERE record_id='S0'")
        self.refused()

    @needs_dbpage
    def test_residual_identity_restoring_edit_is_served_until_an_audit_whatever_the_day(self):
        """Documented class (Kata 91h5 removed the daily expiry): an edit that restores size, mtime,
        ctime and counter is not seen by the identity on any day; the audit finds it."""
        self.raw_edit(restore_mtime=False)
        identity = self.saved()["binding"]["identity"]
        with patch.object(catalog, "file_identity", lambda path, connection: identity):
            self.assertEqual(catalog.status(self.root)["revision"], 3)
            with self.day_after():
                self.assertEqual(catalog.status(self.root)["revision"], 3)
            with self.assertRaises(CatalogError):
                catalog.status(self.root, audit=True)

    @needs_dbpage
    def test_a_new_utc_day_replays_nothing(self):
        calls, patched = self.writes_inspect()
        with patched, self.day_after():
            catalog.status(self.root)
            catalog.status(self.root)
            self.write(30)
        self.assertEqual(calls, [])

    @needs_dbpage
    def test_a_delta_commit_carries_the_audit_date_forward(self):
        yesterday = (snapshots.utcnow() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        with closing(catalog.connect(self.db)) as connection:
            connection.execute("BEGIN")
            catalog._write_receipt(self.home, catalog._binding(connection), catalog._head_state(connection),
                                   validated_on=yesterday)
        # The date records the last full replay for the audit report; it never makes the receipt a miss.
        calls, patched = self.writes_inspect()
        with patched:
            self.write(20)
        self.assertEqual(calls, [])
        self.assertEqual(self.saved()["validated_on"], yesterday)
        catalog.status(self.root, audit=True)
        self.assertEqual(self.saved()["validated_on"], catalog._today())  # a full replay (the audit) records its day

    def test_audit_always_replays(self):
        calls, patched = self.writes_inspect()
        with patched:
            catalog.status(self.root, audit=True)
            catalog.status(self.root, audit=True)
        self.assertEqual(len(calls), 2)

    @needs_dbpage
    def test_old_format_receipt_is_a_miss_not_a_refusal(self):
        legacy = {"binding": self.saved()["binding"], "state": self.saved()["state"]}
        legacy["checksum"] = catalog.fingerprint(legacy)
        self.receipt.write_text(catalog.canonical(legacy) + "\n")
        calls, patched = self.writes_inspect()
        with patched:
            self.assertEqual(catalog.status(self.root)["revision"], 3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.saved()["binding"]["version"], 2)

    def test_journal_present_falls_back_to_the_byte_binding(self):
        with patch.object(catalog, "file_identity", lambda path, connection: None):
            with closing(catalog.connect(self.db)) as connection:
                connection.execute("BEGIN")
                binding = catalog._binding(connection)
        self.assertEqual(binding["version"], 1)
        self.assertIn("snapshot_sha256", binding)

    @needs_dbpage
    def test_binding_changes_with_every_commit_and_is_stable_without_one(self):
        def identity():
            with closing(catalog.connect(self.db)) as connection:
                connection.execute("BEGIN")
                return catalog._binding(connection)["identity"]
        first, again = identity(), identity()
        self.assertEqual(first, again)
        self.write(30)
        self.assertNotEqual(identity(), first)


class ReviewRegressions(Case):
    def setUp(self):
        super().setUp()
        for n in range(3):
            self.write(n)

    @needs_dbpage
    def test_idempotent_retry_on_a_new_utc_day_replays_nothing(self):
        later = snapshots.utcnow() + datetime.timedelta(days=1)
        records = catalog_records(1)
        calls, patched = self.writes_inspect()
        with patched, patch.object(snapshots, "utcnow", lambda: later):
            for _ in range(2):
                again = catalog.submit(self.root, records, actor="a", request_id="r1", reason="test",
                                       expected_state="outdated")
                self.assertTrue(again["idempotent"])
        self.assertEqual(calls, [])

    def test_reader_opened_before_a_restore_never_binds_the_restored_file(self):
        snapshot = Path(catalog.backup(self.root)["snapshot"])
        self.write(20)  # the live head now differs from the snapshot
        old = catalog.connect(self.db)
        try:
            old.execute("BEGIN")
            catalog.restore(self.root, snapshot, expected_state=catalog.status(self.root)["state_token"])
            catalog.validated_state(old, self.home)  # sees the old file's state
            # The receipt on disk must not pair the restored file's identity with the old state.
            self.assertEqual(catalog.status(self.root)["revision"], 3)
            self.assertEqual(catalog.status(self.root)["revision"], 3)
        finally:
            old.close()
