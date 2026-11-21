"""A running progress view must not make a concurrent writer fail its busy wait (Kata xeb8).

The review-event store is the one that failed: its writers waited on a reader's SHARED lock, and a
full replay (the cold ``progress`` read, and the index freshness check on every refresh) held that
lock for the whole O(history) validation. Replay time is stood in for by a delay inside
``validate_history``, injected only on the reading thread, so the test is independent of host speed.
"""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_review_event_memo import add
from tools.catalog import catalog_store as catalog
from tools.review_events import event_history, event_reader
from tools.review_events import event_store as events

WRITER_WAIT = 0.4  # seconds a writer waits for readers, far below the injected replay time
REPLAY = 1.5       # seconds the reader's validation takes


class WriterContention(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        catalog.initialize(self.root)
        events.initialize(self.root)
        event_history.clear_memo()
        self.addCleanup(event_history.clear_memo)
        for n in range(5):
            add(self.root, n)

    def writer_during(self, read):
        """Commit events from another thread, with a short busy wait, while ``read`` runs here."""
        reader, real = threading.get_ident(), event_history.validate_history
        committed, failures, running = [], [], threading.Event()
        stop = threading.Event()

        def slow(*args, **kwargs):
            if threading.get_ident() == reader:
                time.sleep(REPLAY)
            return real(*args, **kwargs)

        def write():
            number = 100
            while not stop.is_set():
                number += 1
                try:
                    add(self.root, number)
                    committed.append(number)
                except Exception as error:  # the failure under test, whatever its type
                    failures.append(f"{type(error).__name__}: {error}")
                running.set()
                time.sleep(0.02)

        with patch.object(events, "BUSY_TIMEOUT_SECONDS", WRITER_WAIT), \
                patch.object(event_history, "validate_history", slow):
            thread = threading.Thread(target=write)
            thread.start()
            running.wait(5)
            try:
                read()
            finally:
                stop.set()
                thread.join()
        return committed, failures

    def test_a_full_replay_read_does_not_fail_a_concurrent_writers_wait(self):
        committed, failures = self.writer_during(lambda: event_reader.read_snapshot(self.root, full=True))
        self.assertEqual(failures, [])
        self.assertGreaterEqual(len(committed), 10)  # kept committing through the whole replay

    def test_a_cold_progress_read_does_not_fail_a_concurrent_writers_wait(self):
        from tools.intake import progress

        def read():
            event_history.clear_memo()
            # A cold read is one with no receipt either (a new process after a commit by older code).
            (events.locations(self.root)[0] / ".local" / event_history.RECEIPT).unlink(missing_ok=True)
            self.assertTrue(progress.read_review_events(self.root)["available"])
        committed, failures = self.writer_during(read)
        self.assertEqual(failures, [])
        self.assertGreaterEqual(len(committed), 10)

    def test_a_writer_waits_out_a_readers_brief_hold_instead_of_failing(self):
        from contextlib import closing
        _, db, _ = events.locations(self.root)
        outcome = []

        def write():
            try:
                add(self.root, 900)  # its commit needs the reader gone; it used to fail at once
                outcome.append("committed")
            except Exception as error:
                outcome.append(f"{type(error).__name__}: {error}")
        with closing(events.connect(db)) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM events").fetchone()  # SHARED lock held
            thread = threading.Thread(target=write)
            thread.start()
            time.sleep(0.5)
            reader.execute("COMMIT")
            thread.join(10)
        self.assertEqual(outcome, ["committed"])


if __name__ == "__main__":
    unittest.main()
