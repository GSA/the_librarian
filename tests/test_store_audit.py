"""The scheduled audit is the one routine that replays every store in full (Kata 91h5)."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tools import store_audit
from tools.catalog import catalog_store as catalog
from tools.diagnostics.catalog_lock_benchmark import registration
from tools.intake import store
from tools.review_events import corrections, event_history, event_store as events
from tools.review_events import review_events as contract
from tests.test_file_identity_locks import needs_dbpage
from tests.test_review_event_memo import add


class AuditCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)
        events.initialize(self.root)
        event_history.clear_memo()
        self.addCleanup(event_history.clear_memo)
        state = catalog.status(self.root)["state_token"]
        catalog.submit(self.root, registration(0), actor="a", request_id="r0", reason="t", expected_state=state)
        add(self.root, 0)

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = store_audit.main(["--root", str(self.root), *args])
        return code, out.getvalue(), err.getvalue()

    def stores(self, report):
        return {entry["store"]: entry for entry in report["stores"]}


class Audit(AuditCase):
    def test_a_sound_instance_passes_with_a_body_free_report(self):
        code, out, _ = self.run_main()
        report = json.loads(out)
        self.assertEqual((code, report["ok"]), (0, True))
        self.assertEqual(sorted(self.stores(report)), ["catalog", "intake-ledger", "review-events"])
        self.assertTrue(all(entry["ok"] for entry in report["stores"]))
        self.assertIsNotNone(self.stores(report)["catalog"]["backup"])
        self.assertNotIn("Synthetic", out)

    def initialize_corrections(self):
        corrections.initialize(self.root)
        return corrections.locations(self.root)

    def test_a_claim_correction_store_is_audited_when_it_exists(self):
        self.assertNotIn("claim-corrections", self.stores(json.loads(self.run_main()[1])))  # optional store
        self.initialize_corrections()
        code, out, _ = self.run_main()
        entry = self.stores(json.loads(out))["claim-corrections"]
        self.assertEqual((code, entry["ok"], entry["records"], entry["revision"]), (0, True, 0, 0))
        self.assertIsNotNone(entry["backup"])
        self.assertNotIn("Synthetic", out)

    def test_a_damaged_claim_correction_store_is_flagged_and_the_others_still_run(self):
        _, db, _ = self.initialize_corrections()
        with closing(sqlite3.connect(db, isolation_level=None)) as connection:
            connection.execute("DROP TRIGGER immutable_metadata_update")
            connection.execute("UPDATE metadata SET value='x' WHERE key='authority'")
        os.chmod(db, 0o600)
        code, out, _ = self.run_main("--no-backups")
        flagged = self.stores(json.loads(out))
        self.assertEqual(code, 1)
        self.assertFalse(flagged["claim-corrections"]["ok"])
        self.assertTrue(flagged["catalog"]["ok"] and flagged["review-events"]["ok"])

    def test_a_corrupt_newest_claim_correction_backup_is_flagged(self):
        _, _, backups = self.initialize_corrections()
        newest = sorted(backups.glob("*.sqlite3"))[-1]
        raw = bytearray(newest.read_bytes())
        raw[-1] ^= 0xFF
        newest.write_bytes(bytes(raw))
        os.chmod(newest, 0o600)
        code, out, _ = self.run_main()
        flagged = self.stores(json.loads(out))
        self.assertEqual(code, 1)
        self.assertFalse(flagged["claim-corrections"]["ok"])
        self.assertTrue(flagged["catalog"]["ok"])

    def test_it_replays_every_store_in_full_whatever_the_receipts_say(self):
        calls = {"catalog": 0, "events": 0}
        real_catalog, real_events = catalog.inspect, event_history.validate_history

        def count_catalog(*args, **kwargs):
            calls["catalog"] += 1
            return real_catalog(*args, **kwargs)

        def count_events(*args, **kwargs):
            calls["events"] += 1
            return real_events(*args, **kwargs)
        catalog.status(self.root)
        events.status(self.root)
        with patch.object(catalog, "inspect", count_catalog), patch.object(event_history, "validate_history", count_events):
            code, _, _ = self.run_main("--no-backups")
        self.assertEqual(code, 0)
        self.assertGreaterEqual(calls["catalog"], 1)
        self.assertEqual(calls["events"], 1)

    @needs_dbpage
    def test_an_edit_the_receipts_cannot_see_is_flagged_and_the_other_stores_still_run(self):
        home, db, _ = catalog.locations(self.root)
        with closing(catalog.connect(db)) as connection:
            connection.execute("BEGIN")
            identity = catalog.file_identity(db, connection)
        data = db.read_bytes()
        at = data.index(b"Synthetic.")
        descriptor = os.open(db, os.O_RDWR)
        os.pwrite(descriptor, b"Syntheticx", at)
        os.close(descriptor)
        with patch.object(catalog, "file_identity", lambda path, connection: identity):
            self.assertEqual(catalog.status(self.root)["revision"], 1)  # a read trusts the receipt: by design
            code, out, _ = self.run_main()
        report = json.loads(out)
        self.assertEqual((code, report["ok"]), (1, False))
        flagged = self.stores(report)
        self.assertFalse(flagged["catalog"]["ok"])
        self.assertTrue(flagged["review-events"]["ok"] and flagged["intake-ledger"]["ok"])

    def test_a_damaged_review_event_store_is_flagged_and_no_read_trusts_it_afterwards(self):
        _, db, _ = events.locations(self.root)
        events.status(self.root)
        with closing(sqlite3.connect(db, isolation_level=None)) as connection:
            for table in ("metadata", "batches", "events", "corrections"):
                for action in ("update", "delete"):
                    connection.execute(f"DROP TRIGGER immutable_{table}_{action}")
            connection.execute("UPDATE events SET entry_hash='0' WHERE sequence=1")
        os.chmod(db, 0o600)
        code, out, _ = self.run_main("--no-backups")
        flagged = self.stores(json.loads(out))
        self.assertEqual(code, 1)
        self.assertFalse(flagged["review-events"]["ok"])
        event_history.clear_memo()
        with self.assertRaises(contract.ReviewError):
            events.status(self.root)  # a new process, whose only memory is the receipt, still refuses

    def test_a_corrupt_newest_backup_is_flagged(self):
        newest = sorted(catalog.locations(self.root)[2].glob("*.sqlite3"))[-1]
        raw = bytearray(newest.read_bytes())
        raw[-1] ^= 0xFF
        newest.write_bytes(bytes(raw))
        os.chmod(newest, 0o600)
        code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertFalse(self.stores(json.loads(out))["catalog"]["ok"])

    def test_it_writes_nothing_a_reader_would_not(self):
        before = {path: path.read_bytes() for path in (catalog.locations(self.root)[1], events.locations(self.root)[1])}
        self.run_main()
        self.assertEqual(before, {path: path.read_bytes() for path in before})


if __name__ == "__main__":
    unittest.main()
