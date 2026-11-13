"""Review-event validation is memoized by file identity, never trusted past a change (Kata r71k)."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import backups as snapshots
from tools import policy
from tools.catalog import catalog_store as catalog
from tools.review_events import event_reader
from tools.review_events import event_store as events
from tools.review_events import review_events as contract
from tests.test_file_identity_locks import needs_dbpage
from tests.test_intake_cohort import QualifyCase
from tools.intake import cohort

ROOT = Path(__file__).resolve().parents[1]
ACTOR = "memo-tester"


def event(root, n, **changes):
    """One native pending review of its own source, valid for the store's contract."""
    value = {
        "event_id": f"rev-memo-{n}",
        "source": {"receipt_id": "D1", "source_id": "S1", "locator": f"sources/memo-{n}.txt",
                   "parent_locator": None, "checksum": {"sha256": None, "basis": "unknown"}},
        "scope": {"content": ["text"], "unit": "file", "extent": "defined_scope",
                  "coordinates": "not_applicable", "ranges": [], "description": "Synthetic memo fixture."},
        "outcome": "pending",
        "counts": {"total": 1, "reviewed": 0, "pending": 1, "unreadable": 0, "dispositioned": 0},
        "reason": f"Synthetic reason number {n:06d} for the memo tests.",
        "reviewer": ACTOR, "reviewed_on": None,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "evidence": [{"kind": "agent_statement", "pointer": None, "quote": "Synthetic statement."}],
        "uncertainties": ["Synthetic fixture."],
        "handling": [policy.for_root(root)[0]["handling"]],
        "provenance": {"origin": "agent_review", "record": None, "mapping_version": None,
                       "unmapped_fields": []},
        "relationships": [], "supersedes": [], "correction_reason": None}
    value.update(changes)
    return value


def add(root, n):
    state = events.status(root)
    return events.append(root, events.bundle([event(root, n)]), ACTOR, f"req-memo-{n}",
                         state["revision"], state["state_token"])


class MemoCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        catalog.initialize(self.root)
        events.initialize(self.root)
        self.home, self.db, _ = events.locations(self.root)
        events.clear_memo()
        self.addCleanup(events.clear_memo)
        for n in range(3):
            add(self.root, n)

    def full_passes(self):
        """Count the full history validations a block of work performs."""
        calls = []
        real = events.validate_history

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)
        return calls, patch.object(events, "validate_history", counting)

    def read(self):
        return event_reader.read_snapshot(self.root)

    def refused(self, message=None):
        with self.assertRaises(contract.ReviewError) as caught:
            self.read()
        if message:
            self.assertIn(message, str(caught.exception))

    def day_after(self):
        later = snapshots.utcnow() + timedelta(days=1)
        return patch.object(snapshots, "utcnow", lambda: later)

    def raw_edit(self, restore_mtime):
        """Same-size in-place edit of the file, outside SQLite and the writer."""
        before = os.stat(self.db)
        data = self.db.read_bytes()
        at = data.index(b"Synthetic reason number 000001")
        descriptor = os.open(self.db, os.O_RDWR)
        os.pwrite(descriptor, b"Synthetic reason number 000009", at)
        if restore_mtime:
            os.utime(descriptor, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.close(descriptor)
        self.assertEqual(os.stat(self.db).st_size, before.st_size)


@needs_dbpage
class MemoReuse(MemoCase):
    def test_a_second_read_of_an_unchanged_store_validates_nothing_and_returns_the_same_history(self):
        first = self.read()
        calls, patched = self.full_passes()
        with patched:
            again = self.read()
            third = self.read()
        self.assertEqual(calls, [])
        self.assertEqual((again.state, again.events, again.successors),
                         (first.state, first.events, first.successors))
        self.assertEqual(third.state, first.state)

    def test_a_hit_returns_what_a_fresh_validation_returns(self):
        memoized = self.read()
        events.clear_memo()
        fresh = self.read()
        self.assertEqual((memoized.state, memoized.events), (fresh.state, fresh.events))

    def test_a_caller_cannot_poison_the_memo_through_the_returned_containers(self):
        first = self.read()
        first.events.clear()
        first.state["events"] = 99
        self.assertEqual(self.read().state["events"], 3)
        self.assertEqual(len(self.read().events), 3)

    def test_status_reuses_and_audit_always_replays(self):
        self.read()
        calls, patched = self.full_passes()
        with patched:
            events.status(self.root)
            self.assertEqual(calls, [])
            events.status(self.root, audit=True)
            events.status(self.root, audit=True)
        self.assertEqual(len(calls), 2)

    def test_an_audit_evicts_so_a_refusing_audit_leaves_nothing_to_serve(self):
        self.read()
        self.raw_edit(restore_mtime=False)
        identity = events.file_identity
        stored = {}
        with closing(events.connect(self.db)) as connection:
            connection.execute("BEGIN")
            stored["identity"] = identity(self.db, connection)
        with patch.object(events, "file_identity", lambda path, connection: stored["identity"]):
            with self.assertRaises(contract.ReviewError):
                events.status(self.root, audit=True)
            self.refused()

    def test_an_append_commits_a_new_identity_and_the_next_read_sees_it(self):
        self.read()
        add(self.root, 3)
        self.assertEqual(self.read().state["events"], 4)
        calls, patched = self.full_passes()
        with patched:
            self.assertEqual(self.read().state["events"], 4)
        self.assertEqual(calls, [])

    def test_an_append_validates_the_history_once_not_per_call(self):
        self.read()
        calls, patched = self.full_passes()
        with patched:
            add(self.root, 3)
        # One validation of existing + incoming; none for the pre-commit state or a retry read.
        self.assertLessEqual(len(calls), 2)

    def test_reuse_is_per_contract_and_a_changed_contract_is_a_miss_and_a_refusal(self):
        self.read()
        schema = events.store_schema(self.root)
        schema["description"] = "A different installed contract."
        with patch.object(events, "store_schema", lambda root=None: schema):
            self.refused("Stored contract differs")

    def test_a_journal_beside_the_file_falls_back_to_full_validation(self):
        self.read()
        calls, patched = self.full_passes()
        with patched, patch.object(events, "file_identity", lambda path, connection: None):
            self.read()
            self.read()
        self.assertEqual(len(calls), 2)

    def test_a_connection_not_opened_by_the_store_is_never_memoized(self):
        events.clear_memo()
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as raw:
            raw.row_factory = sqlite3.Row
            raw.execute("BEGIN")
            events.inspect(raw, self.root, reuse=True)
        calls, patched = self.full_passes()
        with patched:
            self.read()
        self.assertEqual(len(calls), 1)  # nothing the raw connection validated was kept

    def test_a_failed_validation_is_never_remembered(self):
        self.raw_edit(restore_mtime=False)
        self.refused()
        self.refused()


@needs_dbpage
class MemoTamper(MemoCase):
    def test_a_same_size_edit_is_refused_after_a_memoized_read(self):
        self.read()
        self.raw_edit(restore_mtime=False)
        self.refused("Event payload hash mismatch")

    def test_a_same_size_edit_with_restored_mtime_is_refused_because_utime_moves_ctime(self):
        self.read()
        self.raw_edit(restore_mtime=True)
        self.refused("Event payload hash mismatch")

    def test_a_swapped_file_is_not_served_from_the_memo(self):
        self.read()
        other = Path(self.temp.name) / "other"
        other.mkdir()
        catalog.initialize(other)
        events.initialize(other)
        for n in range(3):
            add(other, n + 100)
        _, other_db, _ = events.locations(other)
        replacement = self.db.with_name("swap.sqlite3")
        replacement.write_bytes(other_db.read_bytes())
        os.chmod(replacement, 0o600)
        os.replace(replacement, self.db)
        calls, patched = self.full_passes()
        with patched:
            swapped = self.read()
        self.assertEqual(len(calls), 1)
        self.assertEqual({e["event_id"] for e in swapped.events},
                         {"rev-memo-100", "rev-memo-101", "rev-memo-102"})

    def test_a_swapped_tampered_file_is_refused(self):
        self.read()
        copy = self.db.with_name("swap.sqlite3")
        copy.write_bytes(self.db.read_bytes())
        os.chmod(copy, 0o600)
        data = bytearray(copy.read_bytes())
        at = data.index(b"Synthetic reason number 000001")
        data[at:at + 30] = b"Synthetic reason number 000009"
        copy.write_bytes(bytes(data))
        os.replace(copy, self.db)
        self.refused("Event payload hash mismatch")

    def test_a_connection_opened_before_a_swap_is_never_bound_to_the_new_file(self):
        self.read()
        copy = self.db.with_name("swap.sqlite3")
        copy.write_bytes(self.db.read_bytes())
        os.chmod(copy, 0o600)
        with closing(events.connect(self.db)) as old:
            old.execute("BEGIN")
            os.replace(copy, self.db)
            calls, patched = self.full_passes()
            with patched:
                events.inspect(old, self.root, reuse=True)
            self.assertEqual(len(calls), 1)  # the old file's bytes are validated, not the path's memo

    def test_a_sqlite_edit_that_defeats_the_triggers_is_refused(self):
        self.read()
        with closing(sqlite3.connect(self.db, isolation_level=None)) as connection:
            for table in ("metadata", "batches", "events", "corrections"):
                for action in ("update", "delete"):
                    connection.execute(f"DROP TRIGGER immutable_{table}_{action}")
            connection.execute("UPDATE events SET payload_hash='0' WHERE sequence=1")
        os.chmod(self.db, 0o600)
        self.refused()

    def test_the_head_must_match_the_remembered_state(self):
        """A hit re-derives the chain heads from the file; a mismatch is a miss, not a served state."""
        self.read()
        with closing(events.connect(self.db)) as connection:
            connection.execute("BEGIN")
            identity = events.file_identity(self.db, connection)
        with closing(sqlite3.connect(self.db, isolation_level=None)) as connection:
            for table in ("metadata", "batches", "events", "corrections"):
                for action in ("update", "delete"):
                    connection.execute(f"DROP TRIGGER immutable_{table}_{action}")
            connection.execute("UPDATE events SET entry_hash='0' WHERE sequence=3")
        os.chmod(self.db, 0o600)
        with patch.object(events, "file_identity", lambda path, connection: identity):
            self.refused()

    def test_residual_identity_restoring_edit_is_served_until_an_audit_or_the_next_utc_day(self):
        """Documented class: an edit that restores size, mtime, ctime and counter and leaves the
        chain heads alone is seen only by the first full check of the next UTC day or an audit."""
        self.read()
        with closing(events.connect(self.db)) as connection:
            connection.execute("BEGIN")
            identity = events.file_identity(self.db, connection)
        self.raw_edit(restore_mtime=False)
        with patch.object(events, "file_identity", lambda path, connection: identity):
            self.assertEqual(self.read().state["events"], 3)
            with self.assertRaises(contract.ReviewError):
                events.status(self.root, audit=True)
            self.refused("Event payload hash mismatch")  # the refusing audit evicted the memo

    def test_residual_expires_on_the_next_utc_day(self):
        self.read()
        with closing(events.connect(self.db)) as connection:
            connection.execute("BEGIN")
            identity = events.file_identity(self.db, connection)
        self.raw_edit(restore_mtime=False)
        with patch.object(events, "file_identity", lambda path, connection: identity):
            self.assertEqual(self.read().state["events"], 3)
            with self.day_after():
                self.refused("Event payload hash mismatch")


@needs_dbpage
class MemoConcurrentWriter(MemoCase):
    CHILD = (
        "import sys\n"
        "from pathlib import Path\n"
        "from tests.test_review_event_memo import add\n"
        "add(Path(sys.argv[1]), int(sys.argv[2]))\n"
    )

    def test_a_commit_by_another_process_invalidates_the_memo(self):
        self.assertEqual(self.read().state["events"], 3)
        subprocess.run([sys.executable, "-c", self.CHILD, str(self.root), "7"], check=True, cwd=ROOT,
                       capture_output=True, text=True)
        calls, patched = self.full_passes()
        with patched:
            seen = self.read()
            self.assertEqual(len(calls), 1)
            self.assertEqual(seen.state["events"], 4)
            self.assertIn("rev-memo-7", {e["event_id"] for e in seen.events})
            self.read()
            self.assertEqual(len(calls), 1)

    def test_a_pass_that_overlaps_a_commit_is_not_remembered(self):
        """A commit between the identity taken before and after the pass stores nothing."""
        events.clear_memo()
        real = events.file_identity
        seen = []

        def moving(path, connection):
            seen.append(1)
            identity = real(path, connection)
            return identity if len(seen) == 1 else [*identity[:5], "ffffffff"]
        with patch.object(events, "file_identity", moving):
            self.read()
        calls, patched = self.full_passes()
        with patched:
            self.read()
        self.assertEqual(len(calls), 1)


class QualifyEquivalence(QualifyCase):
    """The memo changes cost only: the same run leaves the same manifest, event ids and report."""

    def run_qualify(self, memoized):
        events.clear_memo()
        real = events.inspect
        unmemoized = lambda connection, root=None, *, reuse=False: real(connection, root)
        transport, calls = self._transport(incomplete_text="Just text")
        with patch.object(events, "inspect", real if memoized else unmemoized):
            report = self._qualify(transport)
        manifest = (self.root / "config/selection.local.json").read_bytes()
        snapshot = event_reader.read_snapshot(self.root)
        ids = sorted(e["event_id"] for e in snapshot.events)
        # Extraction output embeds a run-specific value, so two runs of identical code already
        # differ in rendition hashes; everything else must match once hex digests are masked.
        mask = lambda value: re.sub(r"[0-9a-f]{12,64}", "H", json.dumps(value, sort_keys=True))
        return {"counts": report["counts"], "usage": report["usage"], "entries": mask(report["entries"]),
                "manifest": mask(json.loads(manifest)), "event_ids": mask(ids), "calls": len(calls),
                "revision": snapshot.state["revision"], "events": snapshot.state["events"]}

    def test_memoized_and_unmemoized_runs_leave_the_same_results(self):
        other = type(self)(self._testMethodName)  # a second, independent temporary instance
        other.setUp()
        self.addCleanup(other.doCleanups)
        with_memo = self.run_qualify(memoized=True)
        without = other.run_qualify(memoized=False)
        self.assertTrue(with_memo["event_ids"])
        self.assertEqual(with_memo["calls"], 6)
        self.assertEqual(with_memo, without)


if __name__ == "__main__":
    unittest.main()
