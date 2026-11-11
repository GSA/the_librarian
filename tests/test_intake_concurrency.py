"""Concurrent intake agents: lost catalog races retry instead of blocking."""
import errno
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests.support import default_policy_hash
from tools.catalog import catalog_store as catalog
from tools.intake import adapters, cohort, runner, store, upload
from tools.intake.contracts import IntakeError

ROOT = Path(__file__).resolve().parents[1]


def value(sid, name):
    return upload.descriptor(submission_id=sid, format_name="text", name=name,
                             actor="test-runner", reason="Concurrency test")


def agent(root, outside, prefix, count):
    """One intake agent in its own process submitting distinct files."""
    states = []
    for number in range(count):
        path = Path(outside) / ("%s-%d.txt" % (prefix, number))
        path.write_bytes(("%s %d\n" % (prefix, number)).encode())
        states.append(upload.submit(Path(root), path, value("%s-%d" % (prefix, number), path.name))
                      ["receipt"]["state"])
    return states


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        self.outside = Path(self.temp.name) / "outside"
        self.outside.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)

    def submit(self, name, sid):
        path = self.outside / name
        path.write_bytes(name.encode())
        return upload.submit(self.root, path, value(sid, name))


class CatalogRebaseTests(Case):
    def test_stale_pin_rebases_only_without_overlapping_writes(self):
        pinned = catalog.status(self.root)["state_token"]
        source = {"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}}
        other = {"id": "S2", "kind": "source", "data": {"category": "artifact", "provenance": []}}
        catalog.submit(self.root, [source], actor="a", request_id="one", reason="r",
                       expected_state=pinned)
        with self.assertRaises(catalog.StaleState):
            catalog.submit(self.root, [other], actor="a", request_id="two", reason="r",
                           expected_state=pinned)
        rebased = catalog.submit(self.root, [other], actor="a", request_id="two", reason="r",
                                 expected_state=pinned, rebase=True)
        self.assertEqual(rebased["rebased_from"], 0)
        with self.assertRaises(catalog.ConflictingWrite):
            catalog.submit(self.root, [source], actor="a", request_id="three", reason="r",
                           expected_state=pinned, rebase=True)
        with self.assertRaises(catalog.ConflictingWrite):
            catalog.submit(self.root, [other], actor="a", request_id="four", reason="r",
                           expected_state="0" * 64, rebase=True)

    def test_writer_waits_for_another_process_then_reports_busy(self):
        home = catalog.locations(self.root)[0]
        with catalog.writer_lock(home):
            pass
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,os,sys,time\n"
             "fd=os.open(sys.argv[1],os.O_RDWR)\nfcntl.flock(fd,fcntl.LOCK_EX)\n"
             "print('held',flush=True)\ntime.sleep(float(sys.argv[2]))", str(home / "writer.lock"), "0.6"],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with self.assertRaises(catalog.WriterBusy):
            with catalog.writer_lock(home, wait=0.05):
                pass
        started = time.monotonic()
        with catalog.writer_lock(home, wait=5):
            self.assertGreater(time.monotonic() - started, 0.1)


    def test_reader_waits_for_an_active_writers_journal_but_refuses_an_abandoned_one(self):
        home, db, _ = catalog.locations(self.root)
        journal = Path(str(db) + "-journal")
        journal.write_bytes(b"")
        journal.chmod(0o600)
        with self.assertRaises(catalog.CatalogError):
            catalog.status(self.root)
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,os,sys,time\n"
             "fd=os.open(sys.argv[1],os.O_RDWR)\nfcntl.flock(fd,fcntl.LOCK_EX)\n"
             "print('held',flush=True)\ntime.sleep(0.3)\nos.unlink(sys.argv[2])",
             str(home / "writer.lock"), str(journal)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        self.assertIn("state_token", catalog.status(self.root))

    def inject_writer_after_wait(self, hold_seconds):
        """A writer takes the lock and journals after the reader's wait, before its check."""
        import fcntl
        home, db, _ = catalog.locations(self.root)
        journal = Path(str(db) + "-journal")
        real = catalog._await_journal

        def late_writer(path):
            real(path)
            descriptor = os.open(home / "writer.lock", os.O_RDWR)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            journal.write_bytes(b"")
            journal.chmod(0o600)

            def finish():
                journal.unlink()
                os.close(descriptor)
            threading.Timer(hold_seconds, finish).start()
        return patch.object(catalog, "_await_journal", late_writer), journal

    def test_reader_waits_for_a_writer_that_journals_after_the_wait(self):
        injected, journal = self.inject_writer_after_wait(0.3)
        with injected:
            self.assertIn("state_token", catalog.status(self.root))
        self.assertFalse(journal.exists())

    def test_reader_gives_up_on_a_stuck_live_writer_as_busy_not_abandoned(self):
        injected, journal = self.inject_writer_after_wait(0.6)
        with injected, patch.object(catalog, "BUSY_TIMEOUT_SECONDS", 0.2):
            with self.assertRaises(catalog.WriterBusy):
                catalog.status(self.root)
        time.sleep(0.8)

    def test_reader_still_refuses_wal_and_shm_sidecars(self):
        _, db, _ = catalog.locations(self.root)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db) + suffix)
            sidecar.write_bytes(b"")
            sidecar.chmod(0o600)
            with self.assertRaises(catalog.CatalogError):
                catalog.status(self.root)
            sidecar.unlink()


class AgentTests(Case):
    def test_two_agent_processes_commit_every_receipt_once(self):
        context = multiprocessing.get_context("spawn")
        with context.Pool(2) as pool:
            results = pool.starmap(agent, [(str(self.root), str(self.outside), p, 6) for p in "ab"])
        self.assertEqual([s for states in results for s in states], ["committed"] * 12)
        records = catalog.read_records(self.root, limit=1000)["records"]
        receipts = [r for r in records if r["kind"] == "receipt"]
        self.assertEqual(len(receipts), 12)
        self.assertEqual(len({r["id"] for r in records}), len(records))

    def test_lost_race_is_retried_and_reported_separately_from_failures(self):
        real = catalog.submit
        calls = []

        def racing(*args, **kwargs):
            if not calls:
                calls.append(1)
                raise catalog.StaleState("Stale expected state; re-read before writing")
            return real(*args, **kwargs)
        with patch.object(adapters.catalog, "submit", side_effect=racing):
            ack = self.submit("one.txt", "sub-1")
        self.assertEqual(ack["receipt"]["state"], "committed")
        self.assertEqual(ack["receipt"]["conflicts"], 1)
        stage = store.inspect(self.root, ack["correlation_id"])["stages"][0]
        self.assertEqual([a["code"] for a in stage["attempts"]], ["catalog-stale-state", None])

    def test_cohort_counts_retried_conflicts_and_details_failures(self):
        inbox = Path(self.temp.name) / "inbox"
        inbox.mkdir()
        for name in ("a.md", "b.md"):
            (inbox / name).write_bytes(b"# " + name.encode() + b"\n")
        real = catalog.submit
        seen = set()

        def racing(root, records, **kwargs):
            if kwargs["request_id"] not in seen and kwargs["request_id"].startswith("extract-"):
                seen.add(kwargs["request_id"])
                raise catalog.WriterBusy("Another catalog writer is active; retry later")
            return real(root, records, **kwargs)
        with patch.object(adapters.extract.catalog, "submit", side_effect=racing):
            report = cohort.ingest(self.root, inbox, system="inbox", actor="cohort-runner")
        self.assertEqual(report["counts"]["outcome"].get("extracted"), 2)
        self.assertEqual(report["counts"]["retried_after_conflict"],
                         {"entries": 2, "succeeded": 2, "attempts": 2})
        self.assertEqual(report["counts"]["failed_by_code"], {})


class SettleTests(Case):
    def test_settle_waits_out_backoff_and_completes_within_budget(self):
        real = catalog.submit
        calls = []

        def racing(*args, **kwargs):
            if not calls:
                calls.append(1)
                raise catalog.StaleState("Stale expected state; re-read before writing")
            return real(*args, **kwargs)
        import json
        from tools import policy
        records = [{"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}}]
        store.stage(self.root, json.dumps({
            "version": 1, "correlation_id": "req-backoff", "correction_of": None,
            "policy": {"version": 1, "sha256": default_policy_hash()},
            "retry": {"max_attempts": 3, "backoff_seconds": 1},
            "stages": [{"id": "receipt", "kind": "receipt", "operation": "catalog-submit", "arguments": {
                "records": records, "actor": "a", "request_id": "backoff", "reason": "r",
                "expected_state": catalog.status(self.root)["state_token"]}}]}).encode())
        with patch.object(runner, "SETTLE_MAX_WAIT", 5), \
                patch.object(adapters.catalog, "submit", side_effect=racing):
            result = runner.settle(self.root, "req-backoff", "receipt")
        self.assertEqual(result["stages"][0]["status"], "succeeded")
        self.assertEqual(len(result["stages"][0]["attempts"]), 2)


def stage_registration(root, correlation, *, max_attempts=3, backoff=0):
    import json
    from tools import policy
    records = [{"id": "S%d" % (sum(map(ord, correlation)) * 7 + len(correlation)), "kind": "source",
                "data": {"category": "artifact", "provenance": []}}]
    store.stage(root, json.dumps({
        "version": 1, "correlation_id": correlation, "correction_of": None,
        "policy": {"version": 1, "sha256": policy.for_root(root)[1]},
        "retry": {"max_attempts": max_attempts, "backoff_seconds": backoff},
        "stages": [{"id": "receipt", "kind": "receipt", "operation": "catalog-submit", "arguments": {
            "records": records, "actor": "a", "request_id": correlation, "reason": "r",
            "expected_state": catalog.status(root)["state_token"]}}]}).encode())


class ContentionBudgetTests(Case):
    """Lost catalog waits and stale pins wrote nothing, so they do not spend the fault budget."""

    def failing(self, *errors):
        real = catalog.submit
        pending = list(errors)

        def submit(*args, **kwargs):
            if pending:
                raise pending.pop(0)
            return real(*args, **kwargs)
        return patch.object(adapters.catalog, "submit", side_effect=submit)

    def codes(self, result):
        return [a["code"] or a["outcome"] for a in result["stages"][0]["attempts"]]

    def test_more_lost_waits_than_the_attempt_budget_still_settle(self):
        stage_registration(self.root, "req-busy")
        busy = catalog.WriterBusy("Another catalog writer is active; retry later")
        with self.failing(*[busy] * 5):
            result = runner.settle(self.root, "req-busy", "receipt")
        self.assertEqual(result["stages"][0]["status"], "succeeded")
        self.assertEqual(self.codes(result), ["catalog-busy"] * 5 + ["succeeded"])
        self.assertEqual(runner.conflicts(result["stages"][0]), 5)

    def test_stale_pins_do_not_spend_the_budget_either(self):
        stage_registration(self.root, "req-stale", max_attempts=1)
        stale = catalog.StaleState("Stale expected state; re-read before writing")
        with self.failing(stale, catalog.WriterBusy("busy"), stale):
            result = runner.settle(self.root, "req-stale", "receipt")
        self.assertEqual(result["stages"][0]["status"], "succeeded")
        self.assertEqual(self.codes(result), ["catalog-stale-state", "catalog-busy", "catalog-stale-state", "succeeded"])

    def test_contention_beyond_the_bound_leaves_the_stage_retryable_and_resumable(self):
        stage_registration(self.root, "req-long")
        busy = catalog.WriterBusy("busy")
        with patch.object(runner, "CONTENTION_RETRIES", 4), self.failing(*[busy] * 6):
            result = runner.settle(self.root, "req-long", "receipt")
            self.assertEqual(result["stages"][0]["status"], "retryable")
            self.assertEqual(self.codes(result), ["catalog-busy"] * 4)
            result = runner.settle(self.root, "req-long", "receipt")
        self.assertEqual(result["stages"][0]["status"], "succeeded")
        self.assertEqual(self.codes(result), ["catalog-busy"] * 6 + ["succeeded"])

    def test_a_lock_stuck_past_the_contention_budget_dead_letters_and_recovers(self):
        stage_registration(self.root, "req-stuck")
        busy = catalog.WriterBusy("busy")
        with patch.object(runner, "CONTENTION_BUDGET", 5), patch.object(runner, "CONTENTION_RETRIES", 100), \
                self.failing(*[busy] * 5):
            result = runner.settle(self.root, "req-stuck", "receipt")
            self.assertEqual((result["stages"][0]["status"], self.codes(result)),
                             ("dead-letter", ["catalog-busy"] * 5))
            with self.assertRaises(IntakeError):
                runner.run(self.root, "req-stuck", "receipt")
        applied = runner.recover_conflicts(self.root, apply=True)
        self.assertEqual((applied["candidates"], applied["results"]), (1, {"succeeded": 1}))

    def test_faults_still_spend_the_budget_between_lost_waits(self):
        stage_registration(self.root, "req-fault")
        busy = catalog.WriterBusy("busy")
        transient = OSError(errno.EAGAIN, "transient")
        with self.failing(busy, transient, busy, transient, busy, transient):
            result = runner.settle(self.root, "req-fault", "receipt")
        self.assertEqual(result["stages"][0]["status"], "dead-letter")
        self.assertEqual(self.codes(result), ["catalog-busy", "os-error", "catalog-busy", "os-error",
                                              "catalog-busy", "os-error"])

    def test_input_faults_and_overlapping_writes_still_block(self):
        for name, error, code in (("req-input", catalog.CatalogError("bad input"), "native-input"),
                                  ("req-overlap", catalog.ConflictingWrite("overlap"), "catalog-conflict")):
            stage_registration(self.root, name)
            with self.failing(catalog.WriterBusy("busy"), error):
                result = runner.settle(self.root, name, "receipt")
            self.assertEqual((result["stages"][0]["status"], self.codes(result)),
                             ("blocked", ["catalog-busy", code]))

    def test_single_run_after_lost_waits_is_not_an_attempt_limit(self):
        stage_registration(self.root, "req-run", max_attempts=1)
        busy = catalog.WriterBusy("busy")
        with self.failing(busy, busy):
            for _ in range(2):
                self.assertEqual(runner.run(self.root, "req-run", "receipt")["stages"][0]["status"], "retryable")
            self.assertEqual(runner.run(self.root, "req-run", "receipt")["stages"][0]["status"], "succeeded")

    def test_a_real_lock_held_for_more_waits_than_the_budget_does_not_dead_letter_the_stage(self):
        stage_registration(self.root, "req-held")
        home = catalog.locations(self.root)[0]
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,os,sys,time\n"
             "fd=os.open(sys.argv[1],os.O_RDWR)\nfcntl.flock(fd,fcntl.LOCK_EX)\n"
             "print('held',flush=True)\ntime.sleep(float(sys.argv[2]))", str(home / "writer.lock"), "1.1"],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with patch.object(catalog, "WRITER_WAIT_SECONDS", 0.2):
            result = runner.settle(self.root, "req-held", "receipt")
        self.assertEqual(result["stages"][0]["status"], "succeeded")
        self.assertGreater(self.codes(result).count("catalog-busy"), 3)


class RecoveryTests(Case):
    def test_recover_conflicts_replays_legacy_blocked_registrations(self):
        legacy = catalog.CatalogError("Stale expected state; re-read before writing")
        with patch.object(adapters.catalog, "submit", side_effect=legacy):
            ack = self.submit("one.txt", "sub-1")
        self.assertEqual((ack["receipt"]["state"], ack["receipt"]["code"]), ("blocked", "native-input"))
        self.submit("two.txt", "sub-2")
        report = runner.recover_conflicts(self.root)
        self.assertEqual((report["apply"], report["candidates"]), (False, 1))
        self.assertEqual(report["by_operation_code"], {"catalog-submit/native-input": 1})
        self.assertEqual(store.inspect(self.root, ack["correlation_id"])["stages"][0]["status"], "blocked")
        applied = runner.recover_conflicts(self.root, apply=True)
        self.assertEqual(applied["results"], {"succeeded": 1})
        self.assertEqual(runner.recover_conflicts(self.root)["candidates"], 0)

    def test_recover_conflicts_skips_extraction_registration_when_original_was_rewritten(self):
        inbox = Path(self.temp.name) / "inbox"
        inbox.mkdir()
        (inbox / "a.md").write_bytes(b"# a\n")
        real = catalog.submit

        def legacy(root, records, **kwargs):
            if kwargs["request_id"].startswith("extract-"):
                raise catalog.CatalogError("Stale expected state; re-read before writing")
            return real(root, records, **kwargs)
        with patch.object(adapters.extract.catalog, "submit", side_effect=legacy):
            report = cohort.ingest(self.root, inbox, system="inbox", actor="cohort-runner")
        self.assertFalse(report["counts"]["outcome"].get("extracted"))
        self.assertEqual(runner.recover_conflicts(self.root)["candidates"], 1)
        original = next(r for r in catalog.read_records(self.root, kind="artifact", limit=100)["records"]
                        if r["data"]["role"] == "original")
        rewritten = {**original, "data": {**original["data"], "reason": "Corrected after the failed registration."}}
        catalog.submit(self.root, [rewritten], actor="a", request_id="rewrite", reason="r",
                       expected_state=catalog.status(self.root)["state_token"])
        report = runner.recover_conflicts(self.root)
        self.assertEqual((report["candidates"], report["skipped"]), (0, {"conflicting-write": 1}))


if __name__ == "__main__":
    unittest.main()
