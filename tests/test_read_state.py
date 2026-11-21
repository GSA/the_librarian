"""A read's second freshness check is skipped only when a cheap state proves nothing moved (Kata hep6)."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import time
from unittest.mock import patch

from tests import test_workspace
from tests.support import WorkspaceCase
from tools.catalog import catalog_store as catalog
from tools.catalog import storage
from tools.context_index import cli, proof_cache, read_state
from tools.review_events import event_store


class ReadStateCase(WorkspaceCase):
    selected_text = test_workspace.WorkspaceTests.selected_text

    def setUp(self):
        super().setUp()
        for module in (proof_cache, read_state):
            clock = patch.object(module, "_now_ns", lambda: time.time_ns() + 3 * 10 ** 9)
            clock.start()
            self.addCleanup(clock.stop)
        self.prepared = self.selected_text()

    def read(self, *argv, during=None):
        """Run the CLI; ``during`` runs after the result is assembled. Returns (code, stdout, stderr, checks)."""
        out, err = io.StringIO(), io.StringIO()
        real = cli.search

        def search(*args, **kwargs):
            result = real(*args, **kwargs)
            if during is not None:
                during()
            return result
        with patch.object(cli, "search", search), patch.object(cli, "freshness", wraps=cli.freshness) as check, \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--root", str(self.root), "--manifest", str(self.manifest),
                             "--db", str(self.db), *argv])
        return code, out.getvalue(), err.getvalue(), check.call_count


class SecondCheck(ReadStateCase):
    def test_a_warm_read_checks_freshness_once(self):
        code, out, _, checks = self.read("search", "last")
        self.assertEqual((code, checks), (0, 1))
        self.assertTrue(json.loads(out)["hits"])
        self.assertEqual(self.read("status")[3], 1)

    def test_racily_new_stores_get_the_full_second_check(self):
        with patch.object(read_state, "_now_ns", time.time_ns):
            code, _, _, checks = self.read("search", "last")
        self.assertEqual((code, checks), (0, 2))

    def test_a_first_check_that_had_to_prove_everything_gets_the_full_second_check(self):
        proof_cache.location(self.db).unlink()
        code, _, _, checks = self.read("search", "last")
        self.assertEqual((code, checks), (0, 2))

    def test_a_journal_beside_a_store_gets_the_full_second_check(self):
        self.assertIsNotNone(read_state.capture(self.root, self.db))
        journal = next(self.root.rglob("catalog.sqlite3")).with_name("catalog.sqlite3-journal")
        journal.write_bytes(b"")
        self.addCleanup(journal.unlink, missing_ok=True)
        self.assertIsNone(read_state.capture(self.root, self.db))

    def test_store_file_names_are_the_stores_names(self):
        self.assertEqual((read_state.CATALOG_DB, read_state.EVENTS_DB), (storage.DB_NAME, event_store.DB_NAME))


class ChangeDuringARead(ReadStateCase):
    def test_a_changed_manifest_is_refused(self):
        code, out, err, checks = self.read(
            "search", "last", during=lambda: self.manifest.write_text(self.manifest.read_text() + "\n"))
        self.assertEqual((code, out, checks), (1, "", 2))
        self.assertIn("became stale during read", err)

    def test_a_changed_indexed_document_is_refused(self):
        document = self.root / "documents/AGENTS.md"
        code, out, err, checks = self.read(
            "search", "last", during=lambda: document.write_text(document.read_text() + "\nchanged\n"))
        self.assertEqual((code, out, checks), (1, "", 2))
        self.assertIn("source changed: documents/AGENTS.md", err)

    def test_a_changed_policy_is_refused(self):
        def tighten():
            (self.root / "config/policy.local.json").write_text(json.dumps({"excluded_name_patterns": ["private"]}))
        code, out, err, checks = self.read("search", "last", during=tighten)
        self.assertEqual((code, out, checks), (1, "", 2))
        self.assertIn("became stale during read", err)

    @staticmethod
    def touch(path):
        time.sleep(0.05)  # ctime ticks coarsely; the capture's own age rule covers the same gap for real writes
        os.chmod(path, 0o600)
        os.utime(path)

    def test_a_store_that_moved_gets_the_full_second_check(self):
        for name, move in {
            "catalog touched": lambda: self.touch(next(self.root.rglob("catalog.sqlite3"))),
            "catalog committed": lambda: catalog.submit(
                self.root, [{"id": "S2", "kind": "source", "data": {"category": "artifact", "provenance": []}}],
                actor="test-runner", request_id="during-read", reason="Change during a read",
                expected_state=catalog.status(self.root)["state_token"]),
            "memo replaced": lambda: proof_cache.location(self.db).unlink(),
            "event store created": lambda: event_store.initialize(self.root),
        }.items():
            with self.subTest(name):
                self.assertEqual(self.read("search", "last", during=move)[3], 2)

    def test_a_selected_file_edited_during_one_read_is_found_by_the_next_read(self):
        extraction = self.root / self.prepared["rendition"]
        code, _, _, checks = self.read(
            "search", "last", during=lambda: extraction.write_bytes(extraction.read_bytes() + b" "))
        self.assertEqual((code, checks), (0, 1))
        code, _, err, _ = self.read("status")
        self.assertEqual(code, 1)
        self.assertIn("hash changed", err)
