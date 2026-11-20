"""Intake ledger writers scale: bounded hand-off validation, bounded waits, short readers."""
import contextlib
from contextlib import closing
import fcntl
import io
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

from tests.support import default_policy_hash
from tools.intake import cohort, cohort_ingest, contracts, progress, runner, store

WAIT_ENV = "LIBRARIAN_INTAKE_LOCK_WAIT_SECONDS"


def request(correlation, **changes):
    value = {"version": 1, "correlation_id": correlation, "correction_of": None,
             "policy": {"version": 1, "sha256": default_policy_hash()},
             "retry": {"max_attempts": 2, "backoff_seconds": 0},
             "stages": [{"id": "delivery", "kind": "return", "operation": "await-ack",
                         "arguments": {"delivery_correlation": "delivery-one",
                                       "disposition_path": "disposition.json",
                                       "disposition_sha256": "b" * 64}}]}
    value.update(changes)
    return json.dumps(value).encode()


def writer(root, commands, results):
    """A long-lived writer process that stages one request per command and reports how many times
    it validated the whole ledger and checked its structure."""
    full, structure = [], []
    real_validate, real_check = store.validate, store.check_identity
    with mock.patch.object(store, "validate", side_effect=lambda *a: full.append(1) or real_validate(*a)), \
            mock.patch.object(store, "check_identity", side_effect=lambda *a: structure.append(1) or real_check(*a)):
        for correlation in iter(commands.get, None):
            before = (len(full), len(structure))
            store.stage(Path(root), request(correlation))
            results.put((correlation, len(full) - before[0], len(structure) - before[1]))


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


class CadenceTests(Case):
    """What a writer checks, and when (Kata qwtp); everything else is ``store.audit``."""

    def tamper_digest(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("update requests set request_sha256=? where correlation_id='seed-001'", ("0" * 64,))

    def test_alternating_writers_never_validate_in_full_and_check_structure_once_each(self):
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
        seen = {}
        for turn in range(6):
            name = "turn-%d" % turn
            queues[turn % 2].put(name)
            got, full, structure = results.get(timeout=60)
            seen[got] = (full, structure)
        # No writer repeats the O(ledger) pass; each process checks the file's structure on its first lock only.
        self.assertEqual(seen, {"turn-%d" % n: (0, 1 if n < 2 else 0) for n in range(6)})

    def test_a_read_only_refusal_costs_the_next_writer_nothing(self):
        self.seed(3)
        store.list_requests(self.root)
        with mock.patch.object(store, "validate", wraps=store.validate) as validate, \
                mock.patch.object(store, "check_identity", wraps=store.check_identity) as structure:
            with self.assertRaises(contracts.IntakeError) as caught:
                store.inspect(self.root, "no-such-request")
            self.assertEqual(caught.exception.code, "unknown_request")
            store.stage(self.root, request("after-probe"))
            store.list_requests(self.root)
        self.assertEqual((validate.call_count, structure.call_count), (0, 0))

    def test_a_writer_does_not_see_a_digest_edit_but_audit_does(self):
        self.seed(3)
        store.list_requests(self.root)
        self.tamper_digest()
        self.assertEqual(len(store.list_requests(self.root)), 3)
        store.stage(self.root, request("after-tamper"))
        with self.assertRaises(contracts.IntakeError) as caught:
            store.audit(self.root)
        self.assertEqual(caught.exception.code, "corrupt_request")

    def test_a_fresh_process_still_refuses_sqlite_level_corruption_and_a_wrong_identity(self):
        self.seed(3)
        store.forget_checked()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("delete from requests where correlation_id='seed-001'")
        with self.assertRaises(contracts.IntakeError) as caught:  # orphaned stage rows: foreign_key_check
            store.list_requests(self.root)
        self.assertEqual(caught.exception.code, "corrupt_store")
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("PRAGMA application_id=1")
        with self.assertRaises(contracts.IntakeError) as caught:
            store.list_requests(self.root)
        self.assertEqual(caught.exception.code, "incompatible_store")

    def test_a_passed_check_is_not_trusted_for_a_replaced_file_or_an_identity_change(self):
        self.seed(2)
        store.list_requests(self.root)
        backup = self.db.read_bytes()
        store.stage(self.root, request("later"))
        replacement = self.db.with_name("replacement")
        replacement.write_bytes(backup)
        replacement.chmod(0o600)
        os.replace(replacement, self.db)  # a different inode: structure is checked again
        with mock.patch.object(store, "check_identity", wraps=store.check_identity) as structure:
            store.list_requests(self.root)
            store.list_requests(self.root)
        self.assertEqual(structure.call_count, 1)
        # The application identity is read on every lock, not only on the first.
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("PRAGMA application_id=1")
        with self.assertRaises(contracts.IntakeError) as caught:
            store.list_requests(self.root)
        self.assertEqual(caught.exception.code, "incompatible_store")

    def test_a_ledger_bound_to_another_host_path_is_refused_until_relocated(self):
        self.seed(2)
        store.forget_checked()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("update requests set root='/elsewhere'")
        with self.assertRaises(contracts.IntakeError) as caught:
            store.list_requests(self.root)
        self.assertEqual(caught.exception.code, "corrupt_request")
        self.assertEqual(store.relocate(self.root)["updated_requests"], 2)
        self.assertEqual(len(store.list_requests(self.root)), 2)

    def test_a_failed_check_is_not_remembered(self):
        self.seed(2)
        store.forget_checked()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("delete from requests where correlation_id='seed-001'")
            connection.execute("PRAGMA foreign_keys=ON")
        for _ in range(2):
            with self.assertRaises(contracts.IntakeError) as caught:
                store.list_requests(self.root)
            self.assertEqual(caught.exception.code, "corrupt_store")

    def test_the_writer_lock_file_carries_no_hand_off(self):
        self.seed(2)
        store.list_requests(self.root)
        self.assertEqual(self.lock.stat().st_size, 0)

    def test_the_documented_residual_a_digest_edit_is_served_to_writers_until_audit(self):
        """Pins the trust model: an actor with write access to the file who keeps it structurally
        sound is not seen by writers (no attestation is consulted any more); ``audit`` refuses."""
        self.seed(2)
        store.list_requests(self.root)
        stamp = self.db.stat()
        self.tamper_digest()
        os.utime(self.db, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(len(store.list_requests(self.root)), 2)
        with self.assertRaises(contracts.IntakeError) as caught:
            store.audit(self.root)
        self.assertEqual(caught.exception.code, "corrupt_request")


class AuditTests(Case):
    def test_a_clean_ledger_audits_and_nothing_is_written(self):
        self.seed(7)
        before = (self.db.read_bytes(), self.lock.stat().st_size)
        with mock.patch.object(store, "AUDIT_PAGE", 3):
            self.assertEqual(store.audit(self.root), {"audited": True, "requests": 7})
        self.assertEqual((self.db.read_bytes(), self.lock.stat().st_size), before)
        self.assertFalse(os.path.lexists(f"{self.db}-journal"))

    def test_audit_runs_beside_a_held_writer_lock(self):
        self.seed(3)
        self.hold_lock()
        self.assertEqual(store.audit(self.root)["requests"], 3)

    def test_audit_refuses_each_rule_a_writer_used_to_enforce(self):
        edits = {
            "corrupt_request": ["UPDATE requests SET request_sha256='%s' WHERE correlation_id='seed-002'" % ("0" * 64),
                                "UPDATE requests SET root='/elsewhere'"],
            "corrupt_stage": ["UPDATE stages SET status='x'", "UPDATE stages SET cycle=-1"],
            "corrupt_artifact": ["UPDATE stages SET intent_sha256='%s'" % ("0" * 64), "UPDATE stages SET intent='[]'"],
            "corrupt_replay": ["UPDATE stages SET cycle=1"],
        }
        for expected, statements in edits.items():
            for statement in statements:
                with self.subTest(statement=statement):
                    self.setUp()
                    self.seed(4)
                    with closing(sqlite3.connect(self.db)) as connection, connection:
                        connection.execute(statement)
                        if "intent='[]'" in statement:
                            connection.execute("UPDATE stages SET intent_sha256=?", (contracts.digest(b"[]"),))
                    with self.assertRaises(contracts.IntakeError) as caught:
                        store.audit(self.root)
                    self.assertEqual(caught.exception.code, expected)

    def test_audit_retains_nothing_between_pages(self):
        self.seed(7)
        memos = []
        real = store.verify_page
        with mock.patch.object(store, "AUDIT_PAGE", 3), \
                mock.patch.object(store, "verify_page", side_effect=lambda rows, root, memo: (
                    memos.append(memo), real(rows, root, memo))[1]):
            store.audit(self.root)
        self.assertEqual(len(memos), 3)
        self.assertEqual(len({id(memo) for memo in memos}), 3)  # a fresh memo per page
        self.assertTrue(all(len(memo) == 0 for memo in memos))  # nothing kept as proven

    def test_audit_refuses_a_page_boundary_row(self):
        self.seed(6)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("update requests set request_sha256=? where correlation_id='seed-003'", ("0" * 64,))
        with mock.patch.object(store, "AUDIT_PAGE", 3), self.assertRaises(contracts.IntakeError) as caught:
            store.audit(self.root)
        self.assertEqual(caught.exception.code, "corrupt_request")

    def test_audit_refuses_structural_damage_and_a_missing_store(self):
        self.seed(2)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("PRAGMA application_id=1")
        with self.assertRaises(contracts.IntakeError) as caught:
            store.audit(self.root)
        self.assertEqual(caught.exception.code, "incompatible_store")
        self.db.unlink()
        with self.assertRaises(contracts.IntakeError) as caught:
            store.audit(self.root)
        self.assertEqual(caught.exception.code, "missing_store")

    def test_the_command_line_audits(self):
        from tools.intake import cli
        self.seed(2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--root", str(self.root), "audit"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"audited": True, "requests": 2})
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("update requests set request_sha256=?", ("0" * 64,))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--root", str(self.root), "audit"]), 1)
        self.assertEqual(json.loads(out.getvalue()), {"error": "corrupt_request"})

    def test_relocate_still_validates_the_whole_ledger(self):
        self.seed(2)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("update requests set request_sha256=? where correlation_id='seed-001'", ("0" * 64,))
        with self.assertRaises(contracts.IntakeError) as caught:
            store.relocate(self.root)
        self.assertEqual(caught.exception.code, "corrupt_request")


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
        with mock.patch.object(cohort_ingest, "_process", side_effect=process):
            self.assertEqual(cohort_ingest._settled(self.root, None, entry, "s", "a")["outcome"], "extracted")
            self.assertEqual(entry["conflicts"], 1)
        with mock.patch.object(cohort_ingest, "_process", side_effect=busy):
            failed = cohort_ingest._settled(self.root, None, {"relative": "b.md"}, "s", "a")
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
            report = cohort.ingest(self.root, inbox, system="inbox", actor="cohort-runner", batch_size=1)
        self.assertEqual(report["counts"]["outcome"].get("extracted"), 1)
        self.assertEqual(report["counts"]["retried_after_conflict"]["entries"], 1)
        with mock.patch.object(upload, "submit", side_effect=contracts.LedgerBusy()):
            again = cohort.ingest(self.root, self.root / "inbox", system="inbox2", actor="cohort-runner", batch_size=1)
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
        store.list_requests(self.root)  # the writer has checked the structure
        progress.forget()
        outcome = []
        real = store.verify_page

        def verify_while_writer_commits(rows, root, memo):
            if not outcome:
                outcome.append("running")
                try:
                    store.stage(self.root, request("during-read"))
                    outcome.append("committed")
                except contracts.IntakeError as error:
                    outcome.append(error.code)
            return real(rows, root, memo)
        with mock.patch.dict(os.environ, {WAIT_ENV: "0.3"}), \
                mock.patch.object(store, "verify_page", side_effect=verify_while_writer_commits):
            during = progress.read_ledger(self.root, timeout=1)
        self.assertEqual(outcome, ["running", "committed"])
        self.assertFalse(during["cached"])
        self.assertEqual(progress._cached, {}, "a pass that saw a commit must not be remembered")
        after = progress.read_ledger(self.root, timeout=1)
        self.assertEqual((during["overall"]["requests"], after["overall"]["requests"]), (3, 4))
        self.assertIn("during-read", [r["correlation_id"] for r in after["recent"]])
        self.assertFalse(after["cached"], "a pass that saw a commit must not be remembered")

    def test_progress_still_detects_a_tampered_ledger(self):
        self.seed(2)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("update requests set request_sha256=?", ("0" * 64,))
        with self.assertRaises(contracts.IntakeError) as caught:
            progress.read_ledger(self.root, timeout=1)
        self.assertEqual(caught.exception.code, "corrupt_request")


class CorrectionIndexTests(Case):
    LOOKUPS = (
        ("SELECT 1 FROM requests WHERE correction_of=?", ("seed-000",)),
        ("SELECT 1 FROM requests WHERE correction_of=? AND correlation_id!=?", ("seed-000", "seed-001")),
        ("SELECT correlation_id FROM requests WHERE correction_of=?", ("seed-000",)),
    )

    def seed_corrections(self):
        self.seed(3)
        store.stage(self.root, request("seed-fix", correction_of="seed-000"))

    def index(self):
        with closing(sqlite3.connect(self.db)) as c:
            return c.execute("SELECT sql FROM sqlite_master WHERE name='requests_correction'").fetchone()

    def drop_index(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("DROP INDEX requests_correction")

    def ledger(self):
        with closing(sqlite3.connect(self.db)) as c:
            return {t: c.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                    for t in ("requests", "stages", "attempts", "replays", "acknowledgments")}

    def test_every_correction_lookup_uses_the_index(self):
        self.seed_corrections()
        with store.locked(self.root) as c:
            for sql, args in self.LOOKUPS:
                plan = " ".join(row[3] for row in c.execute("EXPLAIN QUERY PLAN " + sql, args))
                self.assertIn("requests_correction", plan, sql)
                self.assertNotIn("SCAN", plan, sql)

    def test_a_new_ledger_has_the_index(self):
        self.assertIsNotNone(self.index())

    def test_a_ledger_without_the_index_gains_it_on_the_next_writer_without_losing_data(self):
        self.seed_corrections()
        self.drop_index()
        before = self.ledger()
        self.assertEqual(store.head(self.root, "seed-000")["correlation_id"], "seed-fix")
        self.assertIsNotNone(self.index())
        self.assertEqual(self.ledger(), before)
        with store.locked(self.root) as c:
            self.assertIn("requests_correction", " ".join(
                row[3] for row in c.execute("EXPLAIN QUERY PLAN " + self.LOOKUPS[0][0], self.LOOKUPS[0][1])))

    def test_the_upgrade_is_idempotent_and_an_indexed_ledger_is_not_rewritten(self):
        self.seed_corrections()
        self.drop_index()
        store.head(self.root, "seed-000")
        stamp = self.db.stat().st_mtime_ns
        store.head(self.root, "seed-000")
        store.head(self.root, "seed-000")
        self.assertEqual(self.db.stat().st_mtime_ns, stamp)

    def test_the_upgrade_leaves_the_ledger_sound_and_runs_no_full_validation(self):
        self.seed_corrections()
        self.drop_index()
        store.head(self.root, "seed-000")
        calls = []
        real = store.validate
        with mock.patch.object(store, "validate", side_effect=lambda *a: calls.append(1) or real(*a)):
            store.head(self.root, "seed-000")
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_a_reader_holding_the_ledger_delays_the_upgrade_to_a_retryable_busy(self):
        self.seed_corrections()
        self.drop_index()
        reader = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM requests").fetchall()
        with mock.patch.dict(os.environ, {WAIT_ENV: "0.2"}), self.assertRaises(contracts.IntakeError) as caught:
            store.head(self.root, "seed-000")
        self.assertEqual(caught.exception.code, "ledger_busy")
        self.assertIsNone(self.index())
        reader.execute("ROLLBACK")
        self.assertEqual(store.head(self.root, "seed-000")["correlation_id"], "seed-fix")
        self.assertIsNotNone(self.index())


if __name__ == "__main__":
    unittest.main()
