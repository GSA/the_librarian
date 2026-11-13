"""The review-event store refuses a stored contract that differs from the installed one.

Every assertion goes through public store entry points (`status`, `append`, `backup`, `restore`,
`read_snapshot`, the CLI), so the tests hold whether or not validation results are cached.
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests import test_kit
from tests.support import clone_release
from tools.review_events import event_reader
from tools.review_events import event_store as events
from tools.review_events import review_events as contract

ROOT = Path(__file__).resolve().parents[1]
REFUSAL = "Stored contract differs from installed contract; explicit migration required"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ReviewEventContractRefusal(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "config").mkdir()
        self.state = events.initialize(self.root)
        self.db = events.locations(self.root)[1]

    def pending(self):
        return test_kit.WorkspaceCase.pending(self)

    def retag_stored_hash(self, value, *, keep_identity=False):
        """Rewrite the stored contract_hash as an out-of-band edit would, keeping the triggers intact."""
        before = self.db.stat()
        with closing(sqlite3.connect(self.db, isolation_level=None)) as connection:
            connection.execute("DROP TRIGGER immutable_metadata_update")
            connection.execute("UPDATE metadata SET value=? WHERE key='contract_hash'", (value,))
            connection.execute(events.trigger_sql("metadata", "update"))
        if keep_identity:
            os.utime(self.db, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = self.db.stat()
            self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                             (after.st_ino, after.st_size, after.st_mtime_ns))

    def assert_every_entry_point_refuses(self, cli=True):
        """Reads, writes, backups and the CLI all refuse, and the refused store is not modified."""
        before = digest(self.db)
        with self.assertRaisesRegex(contract.ReviewError, REFUSAL):
            events.status(self.root)
        with self.assertRaisesRegex(contract.ReviewError, REFUSAL):
            event_reader.read_snapshot(self.root)
        with self.assertRaisesRegex(contract.ReviewError, REFUSAL):
            events.backup(self.root)
        with self.assertRaisesRegex(contract.ReviewError, REFUSAL):
            events.append(self.root, events.bundle([self.pending()]), "test-runner", "req-refused",
                          self.state["revision"], self.state["state_token"])
        self.assertEqual(digest(self.db), before)
        for command in ("status", "audit") if cli else ():
            result = subprocess.run([sys.executable, "-m", "tools.review_events.event_store", "--root",
                                     str(self.root), command], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertIn(REFUSAL, result.stderr + result.stdout, command)
            self.assertNotIn("Traceback", result.stderr, command)

    def assert_only_empty_reinitialization_proceeds(self, snapshots_refused=False):
        """Init never overwrites; a fresh empty store replaces a moved-aside refused one."""
        original = self.state["store_id"]
        snapshots = sorted(events.locations(self.root)[2].glob("*.sqlite3"))
        self.assertTrue(snapshots)
        with self.assertRaisesRegex(contract.ReviewError, "Store already exists; init never overwrites history"):
            events.initialize(self.root)
        for snapshot in snapshots if snapshots_refused else ():
            # Snapshots taken under the old contract are not a way around the refusal either.
            with self.assertRaisesRegex(contract.ReviewError, REFUSAL):
                events.verified_snapshot(snapshot, self.root)
            with self.assertRaisesRegex(contract.ReviewError, REFUSAL):
                events.restore(self.root, snapshot, self.state["state_token"])
        refused = self.root / "refused-store.sqlite3"
        shutil.move(self.db, refused)
        fresh = events.initialize(self.root)
        self.assertEqual((fresh["events"], fresh["revision"]), (0, 0))
        self.assertNotEqual(fresh["store_id"], original)
        self.assertEqual(events.status(self.root)["state_token"], fresh["state_token"])
        self.assertEqual(event_reader.read_snapshot(self.root).query()["records"], [])

    def test_matching_contract_is_accepted_before_and_after_a_populated_append(self):
        events.append(self.root, events.bundle([self.pending()]), "test-runner", "req-first",
                      self.state["revision"], self.state["state_token"])
        self.assertEqual(events.status(self.root)["events"], 1)
        self.assertEqual(len(event_reader.read_snapshot(self.root).query()["records"]), 1)

    def test_changed_stored_contract_hash_requires_explicit_migration(self):
        events.append(self.root, events.bundle([self.pending()]), "test-runner", "req-first",
                      self.state["revision"], self.state["state_token"])
        self.state = events.status(self.root)  # warm any per-process validation before the edit
        self.assertEqual(self.state["events"], 1)
        self.retag_stored_hash("0" * 64)
        self.assert_every_entry_point_refuses()
        self.assert_only_empty_reinitialization_proceeds()

    def test_stored_hash_edit_that_preserves_file_identity_is_still_refused(self):
        """Same inode, size and mtime: a cache keyed on file identity must not hide the edit."""
        for _ in range(2):
            events.status(self.root)
            event_reader.read_snapshot(self.root)
        stored = events.status(self.root)["contract_hash"]
        replacement = ("1" if stored[0] != "1" else "2") + stored[1:]
        self.retag_stored_hash(replacement, keep_identity=True)
        self.assert_every_entry_point_refuses()

    def test_missing_contract_hash_metadata_is_refused(self):
        with closing(sqlite3.connect(self.db, isolation_level=None)) as connection:
            connection.execute("DROP TRIGGER immutable_metadata_delete")
            connection.execute("DELETE FROM metadata WHERE key='contract_hash'")
            connection.execute(events.trigger_sql("metadata", "delete"))
        with self.assertRaisesRegex(contract.ReviewError, "Invalid store metadata"):
            events.status(self.root)

    def test_changed_installed_schema_requires_explicit_migration(self):
        schema = self.root / "installed-schema.json"
        shutil.copyfile(contract.SCHEMA, schema)
        with patch.object(contract, "SCHEMA", schema):
            self.assertEqual(events.status(self.root)["contract_hash"], self.state["contract_hash"])
            event_reader.read_snapshot(self.root)  # warm
            document = json.loads(schema.read_text())
            document["$id"] = document["$id"].rsplit(":", 1)[0] + ":2"
            schema.write_text(json.dumps(document, indent=2))
            self.assert_every_entry_point_refuses(cli=False)
            self.assert_only_empty_reinitialization_proceeds_under_new_schema()

    def test_installed_schema_edit_that_preserves_file_identity_is_still_refused(self):
        """An in-place same-size schema edit with the mtime restored must not reuse a cached acceptance."""
        schema = self.root / "installed-schema.json"
        shutil.copyfile(contract.SCHEMA, schema)
        with patch.object(contract, "SCHEMA", schema):
            for _ in range(2):
                events.status(self.root)
                event_reader.read_snapshot(self.root)
            before = schema.stat()
            raw = schema.read_bytes()
            self.assertIn(b"review-events:1", raw)
            with schema.open("r+b") as stream:  # same inode, same length
                stream.write(raw.replace(b"review-events:1", b"review-events:2"))
            os.utime(schema, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = schema.stat()
            self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                             (after.st_ino, after.st_size, after.st_mtime_ns))
            self.assert_every_entry_point_refuses(cli=False)

    def test_cli_refuses_after_the_installed_schema_changes(self):
        kit = self.root / "kit"
        kit.mkdir()
        clone_release(kit)
        data = self.root / "instance"
        data.mkdir()
        run = lambda *argv: subprocess.run([sys.executable, "-m", "tools.review_events.event_store",
                                            "--root", str(data), *argv], cwd=kit, capture_output=True, text=True)
        self.assertEqual(run("init").returncode, 0)
        self.assertEqual(run("status").returncode, 0)
        schema = kit / "tools/review_events/review-event-v1.schema.json"
        raw = schema.read_bytes()
        self.assertIn(b"review-events:1", raw)
        schema.write_bytes(raw.replace(b"review-events:1", b"review-events:2"))
        for command in ("status", "audit"):
            result = run(command)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertIn(REFUSAL, result.stderr + result.stdout, command)
            self.assertNotIn("Traceback", result.stderr, command)

    def assert_only_empty_reinitialization_proceeds_under_new_schema(self):
        self.assert_only_empty_reinitialization_proceeds(snapshots_refused=True)
        self.assertNotEqual(events.status(self.root)["contract_hash"], self.state["contract_hash"])


if __name__ == "__main__":
    unittest.main()
