"""A briefly locked store must not end a cohort run, and catalog readers must not hold its lock long (Kata 8as8).

In rollback-journal mode a writer's commit holds PENDING while long readers finish, and meanwhile the
instance-identity probe fails with "database is locked", reported as an invalid store identity.
"""
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import policy
from tools.catalog import catalog_store as catalog
from tools.diagnostics.catalog_lock_benchmark import seed
from tools.intake import cohort_review, store_busy
from tools.intake.contracts import IntakeError


def locked():
    """The error chain a store reports when the identity probe meets a locked database."""
    try:
        try:
            raise sqlite3.OperationalError("database is locked")
        except sqlite3.OperationalError as error:
            raise ValueError("Invalid store identity at x: database is locked") from error
    except ValueError as error:
        try:
            raise IntakeError(str(error)) from error
        except IntakeError as wrapped:
            return wrapped


class FakeTime:
    def __init__(self):
        self.now, self.pauses = 0.0, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.pauses.append(seconds)
        self.now += seconds


class ReaderHold(unittest.TestCase):
    def test_batch_record_reads_release_the_catalog_lock_after_copying_the_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog.initialize(root)
            seed(root, 3)
            catalog.status(root)
            _, db, _ = catalog.locations(root)
            exclusive, real = [], catalog.current_record

            def record(connection, record_id, *args):
                # Runs while the joins' results are paged out: a writer must be able to commit now.
                if not exclusive:
                    with closing(sqlite3.connect(db, timeout=0, isolation_level=None)) as other:
                        try:
                            other.execute("BEGIN EXCLUSIVE")
                            other.execute("ROLLBACK")
                            exclusive.append(True)
                        except sqlite3.OperationalError as error:
                            exclusive.append(str(error))
                return real(connection, record_id, *args)

            with mock.patch.object(catalog, "current_record", side_effect=record):
                batch = catalog.read_records_by_receipt(root, ["D0", "D1"], limit=100)
            self.assertEqual(exclusive[:1], [True])
            for receipt in ("D0", "D1"):
                self.assertEqual(batch[receipt], catalog.read_records(root, receipt=receipt, limit=100))


class BoundedRetry(unittest.TestCase):
    def test_a_busy_store_is_waited_out_and_other_errors_are_raised_at_once(self):
        fake, calls = FakeTime(), []

        def flaky():
            calls.append(1)
            if len(calls) < 4:
                raise locked()
            return "done"
        self.assertEqual(store_busy.retry(flaky, seconds=10, clock=fake.clock, sleep=fake.sleep), "done")
        self.assertEqual(fake.pauses, [0.1, 0.2, 0.4])
        with self.assertRaises(ValueError) as caught:
            store_busy.retry(mock.Mock(side_effect=ValueError("Invalid store identity at x: mismatch")),
                             clock=fake.clock, sleep=fake.sleep)
        self.assertNotIsInstance(caught.exception, store_busy.StoreBusy)

    def test_a_store_busy_past_the_bound_is_reported_busy_not_as_an_invalid_identity(self):
        fake = FakeTime()
        with self.assertRaises(store_busy.StoreBusy) as caught:
            store_busy.retry(mock.Mock(side_effect=locked()), seconds=30, clock=fake.clock, sleep=fake.sleep)
        self.assertEqual((caught.exception.code, caught.exception.retryable), ("store_busy", True))
        self.assertLessEqual(fake.now, 30)
        self.assertEqual(max(fake.pauses), store_busy.LAST_PAUSE)

    def test_the_instance_probe_waits_out_a_locked_store(self):
        roots = {"intake_home": ".local/intake"}
        probe = mock.Mock(side_effect=[locked().__cause__, roots])
        with mock.patch.object(policy, "validate_instance_roots", probe), \
                mock.patch.object(store_busy.time, "sleep") as slept:
            self.assertEqual(store_busy.instance_roots("root"), roots)
        self.assertEqual((probe.call_count, slept.call_count), (2, 1))


class SettleRetry(unittest.TestCase):
    def run_review_all(self, review_and_select, concurrency):
        run = {"usage": Counter(), "snapshot": None, "policy_sha": "p", "manifest": "m"}
        entries = [{"rendition": r, "submission_id": "s", "rendition_sha256": r * 6} for r in ("r1", "r2")]
        with mock.patch.object(cohort_review, "_review_and_select", side_effect=review_and_select), \
                mock.patch.object(cohort_review, "_live_events", return_value=set()), \
                mock.patch.object(store_busy.time, "sleep"):
            cohort_review.review_all("root", entries, run, concurrency)
        return run, entries

    def test_a_busy_store_mid_settle_resumes_without_counting_usage_or_entry_changes_twice(self):
        attempts = Counter()

        def review_and_select(root, entry, run, prefetched=None):
            attempts[entry["rendition"]] += 1
            run["usage"].update(prompt_tokens=10)
            entry["model_call_skipped"] = False
            if entry["rendition"] == "r1" and attempts["r1"] == 1:
                entry["partial"] = True
                raise locked()
            entry["qualification"] = "qualified"
        run, entries = self.run_review_all(review_and_select, 1)
        self.assertEqual(attempts, Counter(r1=2, r2=1))
        self.assertEqual(run["usage"], Counter(prompt_tokens=20))
        self.assertEqual(entries[0], {"rendition": "r1", "submission_id": "s", "rendition_sha256": "r1" * 6,
                                      "model_call_skipped": False, "qualification": "qualified"})

    def test_a_prefetched_review_is_reused_after_a_busy_settle_and_a_failed_one_is_dropped(self):
        seen = []
        prefetched = mock.Mock(return_value={"status": "reviewed"})
        calls = Counter()

        def review_and_select(root, entry, run, fetched=None):
            calls[id(entry)] += 1
            seen.append(fetched)
            if calls[id(entry)] == 1:
                raise locked()
        with mock.patch.object(cohort_review, "_review_and_select", side_effect=review_and_select), \
                mock.patch.object(store_busy.time, "sleep"):
            cohort_review._settle("root", {}, {"usage": Counter()}, prefetched)
            failed = mock.Mock(side_effect=locked())
            cohort_review._settle("root", {}, {"usage": Counter()}, failed)
        self.assertEqual(seen, [prefetched, prefetched, failed, None])

    def test_a_concurrent_run_settles_through_the_same_retry(self):
        attempts = Counter()

        def review_and_select(root, entry, run, prefetched=None):
            attempts[entry["rendition"]] += 1
            if attempts[entry["rendition"]] == 1:
                raise locked()
        with mock.patch.object(cohort_review.store, "correlation_ids", return_value=set()), \
                mock.patch.object(cohort_review, "_free_room", return_value=0):
            self.run_review_all(review_and_select, 2)
        self.assertEqual(attempts, Counter(r1=2, r2=2))


if __name__ == "__main__":
    unittest.main()
