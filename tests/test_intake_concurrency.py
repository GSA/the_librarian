"""Concurrent intake agents: lost catalog races retry instead of blocking."""
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.catalog import catalog_store as catalog
from tools.intake import adapters, cohort, runner, store, upload

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
            "policy": {"version": 1, "sha256": policy.load()[1]},
            "retry": {"max_attempts": 3, "backoff_seconds": 1},
            "stages": [{"id": "receipt", "kind": "receipt", "operation": "catalog-submit", "arguments": {
                "records": records, "actor": "a", "request_id": "backoff", "reason": "r",
                "expected_state": catalog.status(self.root)["state_token"]}}]}).encode())
        with patch.object(runner, "SETTLE_MAX_WAIT", 5), \
                patch.object(adapters.catalog, "submit", side_effect=racing):
            result = runner.settle(self.root, "req-backoff", "receipt")
        self.assertEqual(result["stages"][0]["status"], "succeeded")
        self.assertEqual(len(result["stages"][0]["attempts"]), 2)


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
