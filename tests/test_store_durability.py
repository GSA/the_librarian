"""The durability claim: authority and ledger writes run with synchronous=FULL on a rollback journal.

``PRAGMA synchronous`` is per connection and never persisted, so it cannot be read back from the
file. These tests watch every connection the stores open through their normal write paths and read
the effective setting on it. Each connection starts at ``synchronous=OFF`` (as a build with a
weaker compile-time default would), so only code that explicitly sets FULL passes; dropping or
weakening a pragma fails here. (Kata 0dz3)
"""
from contextlib import closing, contextmanager
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests.test_intake import request
from tests.test_kit import WorkspaceCase
from tools.catalog import catalog_store as catalog
from tools.intake import store as intake
from tools.review_events import event_store as events

FULL = 2
WRITES = re.compile(r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", re.I)


class Watcher:
    """Connections opened while active, with the settings each held when it closed."""

    def __init__(self):
        self.seen = []

    @contextmanager
    def active(self):
        real = sqlite3.connect
        watcher = self

        def spy(*args, factory=sqlite3.Connection, **kwargs):
            class Watched(factory):
                wrote = False

                def close(self):
                    try:
                        record = {"database": Path(self.execute("PRAGMA database_list").fetchone()[2]).name,
                                  "synchronous": self.execute("PRAGMA synchronous").fetchone()[0],
                                  "journal_mode": self.execute("PRAGMA journal_mode").fetchone()[0],
                                  "wrote": self.wrote}
                    except sqlite3.Error:
                        record = None
                    super().close()
                    if record is not None:
                        watcher.seen.append(record)

            connection = real(*args, factory=Watched, **kwargs)

            def trace(statement):
                if WRITES.match(statement):
                    connection.wrote = True
            connection.set_trace_callback(trace)
            connection.execute("PRAGMA synchronous=OFF")  # a weaker default than the code must override
            return connection

        with patch.object(sqlite3, "connect", spy):
            yield self

    def writers(self):
        return [record for record in self.seen if record["wrote"]]


class DurabilityCase(unittest.TestCase):
    def assertDurable(self, watcher, expected_databases):
        writers = watcher.writers()
        self.assertTrue(writers, "no write connection was observed")
        self.assertEqual({record["database"] for record in writers} & expected_databases, expected_databases)
        for record in writers:
            self.assertEqual(record["synchronous"], FULL, record)
            self.assertEqual(record["journal_mode"], "delete", record)


class Meta(DurabilityCase):
    def test_a_connection_that_never_sets_full_is_caught(self):
        with tempfile.TemporaryDirectory() as directory, Watcher().active() as watcher:
            with closing(sqlite3.connect(Path(directory) / "weak.sqlite3")) as connection, connection:
                connection.execute("CREATE TABLE t(x)")
        record = watcher.writers()[0]
        self.assertEqual((record["synchronous"], record["journal_mode"]), (0, "delete"))
        with self.assertRaises(AssertionError):
            self.assertDurable(watcher, {"weak.sqlite3"})


class CatalogStore(DurabilityCase):
    def test_initialize_and_write_paths_run_with_full(self):
        with tempfile.TemporaryDirectory() as directory, Watcher().active() as watcher:
            root = Path(directory)
            catalog.initialize(root)
            state = catalog.status(root)["state_token"]
            catalog.submit(root, [{"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}}],
                           actor="t", request_id="req-durability", expected_state=state, reason="Durability test")
            catalog.backup(root)
            self.assertDurable(watcher, {catalog.locations(root)[1].name})
            self.assertTrue(any(r["database"].startswith("init-") for r in watcher.writers()))

    def test_the_write_connection_reports_full_and_refuses_a_foreign_journal_mode(self):
        with tempfile.TemporaryDirectory() as directory, Watcher().active():
            root = Path(directory)
            catalog.initialize(root)
            with closing(catalog.connect(catalog.locations(root)[1], write=True)) as connection:
                self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], FULL)
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")


class ReviewEventStore(DurabilityCase, WorkspaceCase):
    def test_initialize_and_append_run_with_full(self):
        with Watcher().active() as watcher:
            self.registration()
            state = events.initialize(self.root)
            events.append(self.root, events.bundle([self.pending()]), "test-runner", "req-durability",
                          0, state["state_token"])
            events.backup(self.root)
            self.assertDurable(watcher, {events.locations(self.root)[1].name})
            self.assertTrue(any(r["database"].startswith("init-") for r in watcher.writers()))

    def test_the_write_connection_reports_full_on_a_rollback_journal(self):
        with Watcher().active():
            self.registration()
            events.initialize(self.root)
            with closing(events.connect(events.locations(self.root)[1], write=True)) as connection:
                self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], FULL)
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")


class IntakeLedger(DurabilityCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_initialize_stage_and_relocate_run_with_full(self):
        with Watcher().active() as watcher:
            intake.initialize(self.root)
            intake.stage(self.root, request())
            intake.relocate(self.root)
            self.assertDurable(watcher, {intake.locations(self.root)[2].name})
        # The ledger is created, written under the lock, and rebound on relocation; all three ran.
        self.assertGreaterEqual(len(watcher.writers()), 2)

    def test_the_locked_writer_reports_full_on_a_rollback_journal(self):
        intake.initialize(self.root)
        with Watcher().active(), intake.locked(self.root) as connection:
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], FULL)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")


if __name__ == "__main__":
    unittest.main()
