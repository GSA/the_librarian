"""Intake ledger writers scale: bounded hand-off validation, bounded waits, short readers."""
from contextlib import closing
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from tools import policy
from tools.intake import cohort, contracts, progress, runner, store

WAIT_ENV = "LIBRARIAN_INTAKE_LOCK_WAIT_SECONDS"


def request(correlation, **changes):
    value = {"version": 1, "correlation_id": correlation, "correction_of": None,
             "policy": {"version": 1, "sha256": policy.load()[1]},
             "retry": {"max_attempts": 2, "backoff_seconds": 0},
             "stages": [{"id": "delivery", "kind": "return", "operation": "await-ack",
                         "arguments": {"delivery_correlation": "delivery-one",
                                       "disposition_path": "disposition.json",
                                       "disposition_sha256": "b" * 64}}]}
    value.update(changes)
    return json.dumps(value).encode()


def writer(root, commands, results):
    """A long-lived writer process that stages one request per command and reports how
    much full-ledger validation it performed."""
    calls = []
    real = store.validate
    with mock.patch.object(store, "validate", side_effect=lambda *a: calls.append(1) or real(*a)):
        for correlation in iter(commands.get, None):
            before = len(calls)
            store.stage(Path(root), request(correlation))
            results.put((correlation, len(calls) - before))


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        store.initialize(self.root)
        self.db = self.root / ".local/intake/intake.sqlite3"
        self.lock = self.root / ".local/intake/writer.lock"

    def seed(self, count):
        for number in range(count):
            store.stage(self.root, request("seed-%03d" % number))

    def hold_lock(self):
        descriptor = os.open(self.lock, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        self.addCleanup(os.close, descriptor)
        return descriptor


class HandOffTests(Case):
    def test_alternating_writers_do_not_revalidate_the_ledger_per_hand_off(self):
        self.seed(40)
        context = multiprocessing.get_context("spawn")
        results = context.Queue()
        queues, processes = [], []
        for _ in range(2):
            commands = context.Queue()
            process = context.Process(target=writer, args=(str(self.root), commands, results))
            process.start()
            queues.append(commands)
            processes.append(process)
            self.addCleanup(process.join, 30)
        self.addCleanup(lambda: [q.put(None) for q in queues])
        validations = {}
        for turn in range(6):
            name = "turn-%d" % turn
            queues[turn % 2].put(name)
            got, calls = results.get(timeout=60)
            validations[got] = calls
        # Each hand-off sees a foreign commit; none may repeat the O(ledger) pass.
        self.assertEqual(validations, {"turn-%d" % n: 0 for n in range(6)})

    def test_read_only_refusal_still_hands_off_the_attestation(self):
        # Cohort ingest probes for requests that do not exist yet; that refusal wrote nothing,
        # so the next writer must not repeat the O(ledger) validation because of it.
        self.seed(3)
        store.list_requests(self.root)
        with mock.patch.object(store, "validate", wraps=store.validate) as validate:
            with self.assertRaises(contracts.IntakeError) as caught:
                store.inspect(self.root, "no-such-request")
            self.assertEqual(caught.exception.code, "unknown_request")
            store.list_requests(self.root)
            store.stage(self.root, request("after-probe"))
            store.list_requests(self.root)
        self.assertEqual(validate.call_count, 0)

    def test_refusal_after_a_write_does_not_hand_off_the_attestation(self):
        self.seed(2)
        store.list_requests(self.root)
        with self.assertRaises(RuntimeError):
            with store.locked(self.root) as connection:
                with connection:
                    connection.execute("UPDATE stages SET status='blocked' WHERE correlation_id='seed-000'")
                raise RuntimeError("failed after committing")
        with mock.patch.object(store, "validate", wraps=store.validate) as validate:
            store.list_requests(self.root)
        self.assertEqual(validate.call_count, 1)

    def test_tampering_after_a_read_only_refusal_is_still_fully_validated(self):
        self.seed(3)
        with self.assertRaises(contracts.IntakeError):
            store.inspect(self.root, "no-such-request")
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("update requests set request_sha256=? where correlation_id='seed-001'", ("0" * 64,))
        with self.assertRaises(contracts.IntakeError) as caught:
            store.list_requests(self.root)
        self.assertEqual(caught.exception.code, "corrupt_request")

    def test_foreign_tampering_between_hand_offs_is_still_fully_validated(self):
        self.seed(3)
        store.list_requests(self.root)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("update requests set request_sha256=? where correlation_id='seed-001'", ("0" * 64,))
        with self.assertRaises(contracts.IntakeError) as caught:
            store.list_requests(self.root)
        self.assertEqual(caught.exception.code, "corrupt_request")

    def test_stale_attestation_is_not_trusted_after_the_ledger_is_replaced(self):
        self.seed(2)
        backup = self.db.read_bytes()
        store.stage(self.root, request("later"))
        self.db.write_bytes(backup)
        with mock.patch.object(store, "validate", wraps=store.validate) as validate:
            store.list_requests(self.root)
        self.assertEqual(validate.call_count, 1)


class BusyTests(Case):
    def test_bounded_lock_wait_yields_a_retryable_busy_code(self):
        self.seed(1)
        self.hold_lock()
        started = time.monotonic()
        with mock.patch.dict(os.environ, {WAIT_ENV: "0.2"}):
            with self.assertRaises(contracts.IntakeError) as caught:
                store.inspect(self.root, "seed-000")
        self.assertEqual(caught.exception.code, "ledger_busy")
        self.assertTrue(caught.exception.retryable)
        self.assertLess(time.monotonic() - started, 5)

    def test_explicit_wait_overrides_the_environment_and_bad_values_refuse(self):
        self.seed(1)
        self.hold_lock()
        with mock.patch.dict(os.environ, {WAIT_ENV: "600"}):
            with self.assertRaises(contracts.LedgerBusy):
                with store.locked(self.root, wait=0.05):
                    pass
        for bad in ("x", "-1", "nan"):
            with mock.patch.dict(os.environ, {WAIT_ENV: bad}), self.assertRaises(contracts.IntakeError) as caught:
                with store.locked(self.root):
                    pass
            self.assertEqual(caught.exception.code, "invalid_lock_wait")

    def test_lock_released_during_the_wait_is_acquired(self):
        self.seed(1)
        descriptor = self.hold_lock()
        timer = threading.Timer(0.3, lambda: fcntl.flock(descriptor, fcntl.LOCK_UN))
        timer.start()
        self.addCleanup(timer.join)
        with mock.patch.dict(os.environ, {WAIT_ENV: "10"}):
            self.assertEqual(store.inspect(self.root, "seed-000")["correlation_id"], "seed-000")

    def test_settle_retries_a_busy_ledger_within_budget_then_reports_it(self):
        store.stage(self.root, request("req-one"))
        real = store.locked
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise contracts.LedgerBusy()
            return real(*args, **kwargs)
        with mock.patch.object(store, "locked", side_effect=flaky), \
                mock.patch("tools.intake.adapters.prepare", return_value={"pinned": "x"}), \
                mock.patch("tools.intake.adapters.execute", return_value={"boundary": "return-pending"}):
            result = runner.settle(self.root, "req-one", "delivery")
        self.assertEqual(result["stages"][0]["status"], "return-pending")
        with mock.patch.object(store, "locked", side_effect=contracts.LedgerBusy()):
            with self.assertRaises(contracts.LedgerBusy):
                runner.settle(self.root, "req-one", "delivery")

    def test_cohort_retries_a_busy_entry_and_reports_exhaustion_without_aborting(self):
        entry = {"relative": "a.md", "format": "text"}
        busy = contracts.LedgerBusy()
        calls = []

        def process(*args):
            calls.append(1)
            if len(calls) == 1:
                raise busy
            return {"outcome": "extracted"}
        with mock.patch.object(cohort, "_process", side_effect=process):
            self.assertEqual(cohort._settled(self.root, None, entry, "s", "a")["outcome"], "extracted")
            self.assertEqual(entry["conflicts"], 1)
        with mock.patch.object(cohort, "_process", side_effect=busy):
            failed = cohort._settled(self.root, None, {"relative": "b.md"}, "s", "a")
        self.assertEqual((failed["outcome"], failed["code"], failed["category"]),
                         ("failed", "ledger_busy", "retryable"))

    def test_cohort_retries_a_busy_receipt_through_the_real_process_path(self):
        inbox = self.root / "inbox"
        inbox.mkdir()
        (inbox / "a.md").write_bytes(b"# a\n")
        from tools.catalog import catalog_store as catalog
        from tools.intake import upload
        catalog.initialize(self.root)
        real = upload.submit
        calls = []

        def busy_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise contracts.LedgerBusy()
            return real(*args, **kwargs)
        with mock.patch.object(upload, "submit", side_effect=busy_once):
            report = cohort.ingest(self.root, inbox, system="inbox", actor="cohort-runner")
        self.assertEqual(report["counts"]["outcome"].get("extracted"), 1)
        self.assertEqual(report["counts"]["retried_after_conflict"]["entries"], 1)
        with mock.patch.object(upload, "submit", side_effect=contracts.LedgerBusy()):
            again = cohort.ingest(self.root, self.root / "inbox", system="inbox2", actor="cohort-runner")
        self.assertEqual(again["counts"]["failed_by_code"], {"ledger/ledger_busy": 1})

    def test_invalid_explicit_waits_are_rejected_before_taking_the_lock(self):
        self.seed(1)
        self.hold_lock()
        for bad in (float("nan"), float("inf"), -1, "x", 3601):
            with self.subTest(wait=bad), self.assertRaises(contracts.IntakeError) as caught:
                with store.locked(self.root, wait=bad):
                    pass
            self.assertEqual(caught.exception.code, "invalid_lock_wait")

    def test_cli_reports_the_busy_code_as_retryable(self):
        from tools.intake import cli
        self.seed(1)
        self.hold_lock()
        with mock.patch.dict(os.environ, {WAIT_ENV: "0.05"}), mock.patch("sys.stdout") as out:
            self.assertEqual(cli.main(["--root", str(self.root), "list"]), 1)
        written = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(json.loads(written), {"error": "ledger_busy", "retryable": True})

    def test_transient_sqlite_lock_during_a_stage_is_busy_not_unavailable(self):
        store.stage(self.root, request("req-one"))
        with mock.patch.dict(os.environ, {WAIT_ENV: "0.2"}):
            reader = sqlite3.connect(self.db, isolation_level=None)
            self.addCleanup(reader.close)
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM requests").fetchall()
            with self.assertRaises(contracts.IntakeError) as caught:
                with store.locked(self.root) as c, c:
                    c.execute("UPDATE stages SET status='retryable'")
        self.assertEqual(caught.exception.code, "ledger_busy")


class ReaderTests(Case):
    def test_progress_reader_validating_does_not_block_a_writer_commit(self):
        self.seed(3)
        store.list_requests(self.root)  # attest the ledger so the writer does not validate
        outcome = []
        real = store.request_shape

        def validate_while_writer_commits(raw):
            if not outcome:
                outcome.append("running")
                try:
                    store.stage(self.root, request("during-read"))
                    outcome.append("committed")
                except contracts.IntakeError as error:
                    outcome.append(error.code)
            return real(raw)
        with mock.patch.dict(os.environ, {WAIT_ENV: "0.3"}), \
                mock.patch.object(store, "request_shape", side_effect=validate_while_writer_commits):
            requests, _, _ = progress.read_ledger(self.root, timeout=1)
        self.assertEqual(outcome, ["running", "committed"])
        self.assertIn("during-read", [r[0] for r in progress.read_ledger(self.root, timeout=1)[0]])
        self.assertEqual(len(requests), 3)

    def test_progress_still_detects_a_tampered_ledger(self):
        self.seed(2)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("update requests set request_sha256=?", ("0" * 64,))
        with self.assertRaises(contracts.IntakeError) as caught:
            progress.read_ledger(self.root, timeout=1)
        self.assertEqual(caught.exception.code, "corrupt_request")


if __name__ == "__main__":
    unittest.main()
