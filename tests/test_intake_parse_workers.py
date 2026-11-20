"""Parallel parse workers for ingest (Kata q868): same output at any worker count, bounded, crash tolerant."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.extraction import extract
from tools.intake import cohort, cohort_ingest, parse_pool, store
from tools.intake.contracts import IntakeError

from test_intake_batch import make_instance, snapshot
from test_intake_cohort import CohortCase


def slow_then(value, delay):
    time.sleep(delay)
    return value


def crash_in_worker(value):
    os._exit(9)


REAL_JUDGE = cohort_ingest.judge


def crash_judge(name, raw, root, nbytes):
    """``cohort_ingest.judge`` that kills its worker for one file name."""
    if name == "notes.md":
        os._exit(9)
    return REAL_JUDGE(name, raw, root, nbytes)


REAL_PREPARSE = cohort_ingest.preparse


def crash_preparse(format_name, raw, settings):
    """``cohort_ingest.preparse`` that kills its worker (a spawned process; never the test) on one document."""
    if b"Synthetic note 3\n" in raw:
        os._exit(9)
    return REAL_PREPARSE(format_name, raw, settings)


def collect(workers, jobs, **limits):
    """Run ``jobs`` ((fn, args, size) tuples) through ``workers``; return the delivered (result, error) list."""
    delivered = []
    with parse_pool.Workers(workers, **limits) as pool:
        for fn, args, size in jobs:
            pool.submit(fn, args, size, lambda result, error: delivered.append((result, error)))
        pool.drain()
    return delivered


class PipelineTests(unittest.TestCase):
    def test_results_are_delivered_in_submission_order_whatever_finishes_first(self):
        jobs = [(slow_then, ("first", 0.6), 1), (slow_then, ("second", 0.0), 1), (slow_then, ("third", 0.0), 1)]
        self.assertEqual([r for r, _ in collect(3, jobs)], ["first", "second", "third"])

    def test_one_worker_runs_inline_and_gives_the_same_results(self):
        jobs = [(slow_then, (n, 0.0), 1) for n in range(5)]
        self.assertEqual(collect(1, jobs), collect(3, jobs))

    def test_a_failing_job_fails_alone_in_both_modes(self):
        jobs = [(slow_then, (1, 0.0), 1), (int, ("not a number",), 1), (slow_then, (3, 0.0), 1)]
        for workers in (1, 3):
            with self.subTest(workers=workers):
                (a, none_a), (b, error), (c, none_c) = collect(workers, jobs)
                self.assertEqual((a, none_a, c, none_c), (1, None, 3, None))
                self.assertIsNone(b)
                self.assertIsInstance(error, ValueError)

    def test_the_queue_is_bounded_by_count_and_by_bytes(self):
        for limits, expected in (({"max_pending": 3}, 3), ({"max_bytes": 25}, 2)):
            with self.subTest(limits=limits):
                submitted, peak = [], []
                with parse_pool.Workers(2, **limits) as pool:
                    for n in range(12):
                        submitted.append(n)
                        pool.submit(slow_then, (n, 0.0), 10, lambda r, e: peak.append(len(submitted) - len(peak)))
                    pool.drain()
                self.assertLessEqual(max(peak), expected + 1)
                self.assertEqual(len(peak), 12)

    def test_a_killed_worker_fails_its_job_alone_and_the_pool_keeps_working(self):
        jobs = [(slow_then, (n, 0.0), 1) for n in range(4)]
        jobs.insert(2, (crash_in_worker, (0,), 1))
        jobs += [(slow_then, (n, 0.0), 1) for n in range(4, 8)]
        out = collect(3, jobs)
        self.assertEqual([r for r, _ in out], [0, 1, None, 2, 3, 4, 5, 6, 7])
        self.assertIsInstance(out[2][1], parse_pool.WorkerDied)
        self.assertTrue(all(error is None for _, error in out[:2] + out[3:]))

    def test_a_pool_that_lost_a_worker_serves_later_submissions(self):
        delivered = []
        with parse_pool.Workers(2) as pool:
            pool.submit(crash_in_worker, (0,), 1, lambda r, e: delivered.append((r, e)))
            pool.drain()
            pool.submit(slow_then, ("again", 0.0), 1, lambda r, e: delivered.append((r, e)))
            pool.drain()
        self.assertIsInstance(delivered[0][1], parse_pool.WorkerDied)
        self.assertEqual(delivered[1], ("again", None))

    def test_a_worker_count_below_one_is_refused(self):
        for bad in (0, -1, True, 2.0):
            with self.subTest(bad=bad), self.assertRaises(IntakeError) as caught:
                parse_pool.Workers(bad)
            self.assertEqual(caught.exception.code, "invalid_concurrency")


class ClassifyEquivalence(CohortCase):
    def classified(self, workers, **kwargs):
        with parse_pool.Workers(workers) as pool:
            return cohort.classify(self.root, self.inbox, parse=pool, **kwargs)

    def test_classify_gives_the_same_entries_at_any_worker_count(self):
        self.assertEqual(self.classified(1), self.classified(3))
        self.assertEqual(self.classified(1), cohort.classify(self.root, self.inbox))

    def test_a_file_whose_worker_dies_is_unreadable_and_the_rest_are_classified_as_before(self):
        expected = {e["relative"]: e for e in self.classified(1)}
        with patch.object(cohort_ingest, "judge", crash_judge):
            got = {e["relative"]: e for e in self.classified(3)}
        victim = got.pop("a/notes.md")
        self.assertEqual((victim["disposition"], victim["code"]), ("unreadable", "parse-worker-died"))
        expected.pop("a/notes.md")
        self.assertEqual(got, expected)

    def test_workers_receive_bytes_and_names_never_a_path(self):
        seen = []
        real = parse_pool.Workers.submit

        def record(self_, fn, args, size, done):
            seen.append(args)
            return real(self_, fn, args, size, done)
        with patch.object(parse_pool.Workers, "submit", record):
            self.classified(3)
        self.assertTrue(seen)
        for args in seen:
            self.assertIsInstance(args[0], str)
            self.assertEqual(args[0], Path(args[0]).name)  # a bare file name, not a location
            self.assertIsInstance(args[1], bytes)


class IngestEquivalence(unittest.TestCase):
    def test_one_and_many_workers_end_in_the_same_report_catalog_and_ledger(self):
        root_a, inbox_a = make_instance(self, 14)
        root_b, inbox_b = make_instance(self, 14)
        report_a = cohort.ingest(root_a, inbox_a, system="s", actor="a", batch_size=5, parse_workers=1)
        report_b = cohort.ingest(root_b, inbox_b, system="s", actor="a", batch_size=5, parse_workers=3)
        self.assertEqual(report_b["counts"]["outcome"]["extracted"], 14)
        strip = lambda report: [{k: v for k, v in e.items() if k not in {"original", "rendition", "receipt_id"}}
                                for e in report["entries"]]
        self.assertEqual(report_a["counts"], report_b["counts"])
        self.assertEqual(strip(report_a), strip(report_b))
        one, many = snapshot(root_a), snapshot(root_b)
        for key in ("records", "versions", "ledger", "extractions"):
            self.assertEqual(one[key], many[key], key)

    def test_the_documents_are_parsed_in_the_workers_not_the_writer(self):
        for workers, parsed_here in ((1, True), (3, False)):
            with self.subTest(workers=workers):
                root, inbox = make_instance(self, 12)
                with patch.object(extract, "text", wraps=extract.text) as text:
                    cohort.ingest(root, inbox, system="s", actor="a", batch_size=6, parse_workers=workers)
                self.assertEqual(text.call_count > 0, parsed_here)

    def test_a_document_whose_worker_dies_fails_alone_and_a_rerun_completes_it(self):
        root, inbox = make_instance(self, 8)
        with patch.object(cohort_ingest, "preparse", crash_preparse):
            report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=8, parse_workers=3)
        failed = [e for e in report["entries"] if e.get("outcome") == "failed"]
        self.assertEqual([(e["stage"], e["code"]) for e in failed], [("prepare", "parse-worker-died")])
        self.assertEqual(report["counts"]["outcome"]["extracted"], 7)
        self.assertEqual(report["counts"]["failed_by_code"], {"prepare/parse-worker-died": 1})
        again = cohort.ingest(root, inbox, system="s", actor="a", batch_size=8, parse_workers=3)
        self.assertEqual(again["counts"]["outcome"]["extracted"], 8)

    def test_a_changed_source_is_never_served_a_worker_parse(self):
        root, inbox = make_instance(self, 6)
        victim = next(inbox.rglob("note-000002.*"))
        real = cohort_ingest.classify

        def classify_then_change(*args, **kwargs):
            entries = real(*args, **kwargs)
            victim.write_text("changed after the classification\n")
            return entries
        with patch.object(cohort_ingest, "classify", classify_then_change):
            report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=6, parse_workers=3)
        outcomes = {e["relative"]: e["outcome"] for e in report["entries"] if "outcome" in e}
        self.assertEqual(outcomes[victim.relative_to(inbox).as_posix()], "changed-since-intake")
        self.assertEqual(sum(o == "extracted" for o in outcomes.values()), 5)

    def test_an_invalid_worker_count_is_refused_before_anything_runs(self):
        root, inbox = make_instance(self, 2)
        for bad in (0, "2"):
            with self.subTest(bad=bad), self.assertRaises(IntakeError) as caught:
                cohort.ingest(root, inbox, system="s", actor="a", parse_workers=bad)
            self.assertEqual(caught.exception.code, "invalid_concurrency")
        self.assertEqual(store.list_requests(root), [])

    def test_a_small_inbox_never_starts_a_worker_process_unless_asked_to(self):
        root, inbox = make_instance(self, 6)
        with patch.object(parse_pool.Workers, "_start", side_effect=AssertionError("worker started")):
            report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=3)
        self.assertEqual(report["counts"]["outcome"]["extracted"], 6)

    def test_the_command_line_takes_parse_workers(self):
        args = cohort._parser().parse_args(["ingest", "--root", "r", "--inbox", "i", "--system", "s",
                                            "--actor", "a", "--parse-workers", "2"])
        self.assertEqual(args.parse_workers, 2)
        default = cohort._parser().parse_args(["qualify", "--root", "r", "--inbox", "i", "--system", "s",
                                               "--actor", "a", "--policy", "p", "--manifest", "m",
                                               "--models-config", "c", "--model", "m"])
        self.assertIsNone(default.parse_workers)  # sized from the CPUs when the run starts


if __name__ == "__main__":
    unittest.main()
