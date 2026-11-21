"""A persisted review-event read-validation receipt lets a new process skip the full replay (Kata 91h5)."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from tools.review_events import event_history, event_reader
from tools.review_events import event_store as events
from tools.review_events import review_events as contract
from tests.test_file_identity_locks import needs_dbpage
from tests.test_review_event_memo import ROOT, MemoCase, add, event

CHILD = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "from tools.review_events import event_history, event_reader\n"
    "calls = []\n"
    "real = event_history.validate_history\n"
    "event_history.validate_history = lambda *a, **k: (calls.append(1), real(*a, **k))[1]\n"
    "snap = event_reader.read_snapshot(Path(sys.argv[1]), full=sys.argv[2] == 'full')\n"
    "found = [snap.event(i) is not None for i in sys.argv[3:]]\n"
    "print(json.dumps({'replays': len(calls), 'events': snap.state['events'], 'found': found,\n"
    "                  'last': snap.state['state_token']}))\n"
)


@needs_dbpage
class PersistedReceipt(MemoCase):
    def fresh_process(self, *ids, mode="delta"):
        result = subprocess.run([sys.executable, "-c", CHILD, str(self.root), mode, *ids], check=True,
                                cwd=ROOT, capture_output=True, text=True)
        return json.loads(result.stdout)

    def saved(self):
        return json.loads(self.receipt.read_text())

    def test_a_write_leaves_a_checksummed_private_receipt_for_the_new_bytes(self):
        add(self.root, 3)
        self.assertEqual(self.saved()["state"]["events"], 4)
        self.assertEqual(os.stat(self.receipt).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.receipt.parent).st_mode & 0o777, 0o700)
        self.assertEqual(self.saved()["binding"]["version"], 1)

    def test_a_new_process_reading_an_unchanged_store_replays_nothing(self):
        add(self.root, 3)
        seen = self.fresh_process("rev-memo-0", "rev-memo-3", "rev-memo-nope")
        self.assertEqual((seen["replays"], seen["events"], seen["found"]), (0, 4, [True, True, False]))

    def test_the_next_utc_day_does_not_replay_either(self):
        self.read()
        calls, patched = self.full_passes()
        with patched, self.day_after():
            self.new_process()
            self.assertEqual(self.read().state["events"], 3)
        self.assertEqual(calls, [])

    def test_a_new_process_verifies_only_the_events_past_the_remembered_head(self):
        self.read()
        saved = self.receipt.read_bytes()
        add(self.root, 3)
        add(self.root, 4)
        self.receipt.write_bytes(saved)  # as if the writers could not update it: it is two commits old
        os.chmod(self.receipt, 0o600)
        deltas = []
        real = event_history.validate_delta
        calls, patched = self.full_passes()
        self.new_process()
        with patched, patch.object(event_history, "validate_delta",
                                   lambda history, incoming, root=None, upto=None:
                                   (deltas.append((len(incoming), upto)), real(history, incoming, root, upto))[1]):
            self.assertEqual(self.read().state["events"], 5)
        self.assertEqual((calls, deltas), ([], [(2, 3)]))

    def test_lookups_in_a_new_process_equal_those_of_a_replayed_history(self):
        correction = event(self.root, 0, event_id="rev-memo-fix", supersedes=["rev-memo-0"],
                           correction_reason="Synthetic correction.",
                           recorded_at=(__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                                        + __import__("datetime").timedelta(seconds=5)).isoformat())
        state = events.status(self.root)
        events.append(self.root, event_history.bundle([correction]), "memo-tester", "req-memo-fix",
                      state["revision"], state["state_token"])
        self.new_process()
        stored = self.read()
        self.assertIsInstance(stored.history, event_history.StoredHistory)
        event_history.clear_memo()
        self.receipt.unlink()
        replayed = self.read()
        self.assertIsInstance(replayed.history, event_history.History)
        for event_id in ("rev-memo-0", "rev-memo-fix", "rev-memo-1", "nope"):
            self.assertEqual(stored.event(event_id), replayed.event(event_id))
            self.assertEqual(stored.superseded_by(event_id), replayed.superseded_by(event_id))
        self.assertEqual(stored.superseded_by("rev-memo-0"), "rev-memo-fix")
        self.assertEqual(stored.at("sources/memo-1.txt"), replayed.at("sources/memo-1.txt"))
        self.assertEqual((stored.events, stored.successors), (replayed.events, replayed.successors))

    def test_a_stored_history_never_shows_an_event_appended_after_its_state(self):
        self.new_process()
        before = self.read()
        add(self.root, 3)
        self.assertIsNone(before.event("rev-memo-3"))
        self.assertEqual(self.read().event("rev-memo-3")["event_id"], "rev-memo-3")

    def test_an_audit_replays_and_a_full_read_replays(self):
        self.read()
        self.new_process()
        calls, patched = self.full_passes()
        with patched:
            events.status(self.root, audit=True)
            self.assertEqual(len(calls), 1)
            self.read()
            self.assertEqual(len(calls), 1)
            self.new_process()
            event_reader.read_snapshot(self.root, full=True)
            self.assertEqual(len(calls), 2)

    def test_an_edit_the_identity_cannot_see_is_found_by_the_audit_and_then_by_every_read(self):
        self.read()
        with closing(events.connect(self.db)) as connection:
            connection.execute("BEGIN")
            identity = event_history.file_identity(self.db, connection)
        self.raw_edit(restore_mtime=False)
        with patch.object(event_history, "file_identity", lambda path, connection: identity):
            self.new_process()
            self.assertEqual(self.read().state["events"], 3)  # trusted: this is what the audit is for
            with self.assertRaisesRegex(contract.ReviewError, "Event payload hash mismatch"):
                events.status(self.root, audit=True)
            self.assertFalse(self.receipt.exists())
            self.new_process()
            self.refused("Event payload hash mismatch")

    def test_an_edit_through_the_filesystem_is_still_refused_at_once_by_a_new_process(self):
        self.read()
        self.raw_edit(restore_mtime=False)
        self.new_process()
        self.refused("Event payload hash mismatch")


@needs_dbpage
class ReceiptIsAMissNeverATrust(MemoCase):
    def setUp(self):
        super().setUp()
        self.read()
        self.good = self.receipt.read_bytes()

    def assert_replays(self):
        self.new_process()
        calls, patched = self.full_passes()
        with patched:
            self.assertEqual(self.read().state["events"], 3)
        self.assertEqual(len(calls), 1)

    def write(self, value):
        self.receipt.write_text(value if isinstance(value, str) else json.dumps(value))
        os.chmod(self.receipt, 0o600)

    def test_each_defect_costs_one_replay_and_never_blocks_a_read(self):
        record = json.loads(self.good)
        wrong_checksum = dict(record, checksum="0" * 64)
        other_state = json.loads(self.good)
        other_state["state"]["events"] = 2
        other_binding = json.loads(self.good)
        other_binding["binding"]["validator_sha256"] = "0" * 64
        for name, value in (("not json", "{not json"), ("empty", ""), ("a list", "[]"),
                            ("bad checksum", wrong_checksum), ("state edited", other_state),
                            ("other validator code", other_binding), ("oversize", " " * 5000)):
            with self.subTest(name):
                self.write(value)
                self.assert_replays()
                self.assertNotEqual(self.receipt.read_bytes(), b"")
                self.receipt.write_bytes(self.good)

    def test_a_receipt_for_other_code_runtime_or_contract_is_a_miss(self):
        for target, value in ((event_history, "_validator_sha256"), (event_history, "_runtime")):
            with self.subTest(value), patch.object(target, value, lambda: "changed" if value == "_validator_sha256" else ["x"]):
                self.new_process()
                calls, patched = self.full_passes()
                with patched:
                    self.read()
                self.assertEqual(len(calls), 1)
                self.receipt.write_bytes(self.good)

    def test_a_receipt_that_is_a_symlink_or_open_to_others_is_a_miss(self):
        os.chmod(self.receipt, 0o644)
        self.assert_replays()
        os.unlink(self.receipt)
        target = self.receipt.parent / "elsewhere.json"
        target.write_bytes(self.good)
        os.chmod(target, 0o600)
        os.symlink(target, self.receipt)
        self.assert_replays()

    def test_a_receipt_for_another_file_is_a_miss(self):
        other = self.root.parent / "other"
        other.mkdir()
        from tools.catalog import catalog_store as catalog
        catalog.initialize(other)
        events.initialize(other)
        other_home = events.locations(other)[0]
        (other_home / ".local").mkdir(mode=0o700, exist_ok=True)
        (other_home / ".local" / event_history.RECEIPT).write_bytes(self.good)
        os.chmod(other_home / ".local" / event_history.RECEIPT, 0o600)
        event_history.clear_memo()
        self.assertEqual(event_reader.read_snapshot(other).state["events"], 0)

    def test_a_restored_older_file_is_replayed_not_trusted(self):
        snapshot = Path(events.backup(self.root)["snapshot"])
        add(self.root, 3)
        newer = self.receipt.read_bytes()
        events.restore(self.root, snapshot, events.status(self.root)["state_token"])
        self.receipt.write_bytes(newer)
        os.chmod(self.receipt, 0o600)
        self.new_process()
        self.assertEqual(self.read().state["events"], 3)  # the restored store, found by its own bytes


if __name__ == "__main__":
    unittest.main()
