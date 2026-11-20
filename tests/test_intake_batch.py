"""Batched ingest (Kata herq): outcomes equal the one-at-a-time path; crashes and refusals are safe."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.catalog import catalog_store as catalog
from tools.diagnostics.ingest_benchmark import fixture
from tools.intake import adapters, cohort, cohort_ingest, runner, store, upload
from tools.intake.contracts import IntakeError, LedgerBusy
from tools.extraction import extract

ROOT = Path(__file__).resolve().parents[1]
FILES = 9


def make_instance(case, files=FILES):
    temp = tempfile.TemporaryDirectory()
    case.addCleanup(temp.cleanup)
    base = Path(temp.name)
    root, inbox = base / "instance", base / "inbox"
    root.mkdir()
    store.initialize(root)
    catalog.initialize(root)
    fixture(inbox, files, mixed=True)
    return root, inbox


def snapshot(root):
    """Everything an observer of an ingest can compare, minus revision numbers, times and run IDs."""
    home, db, _ = catalog.locations(root)
    with closing(catalog.connect(db)) as connection:
        connection.execute("BEGIN")
        current = catalog.inspect(connection)[1]
        versions = {}
        for row in connection.execute("SELECT record_id, count(*) FROM versions GROUP BY record_id"):
            versions[row[0]] = row[1]
    ledger = {}
    with closing(sqlite3.connect(store.locations(root)[2])) as c:
        for correlation_id, stage_id, status in c.execute("SELECT correlation_id,stage_id,status FROM stages"):
            ledger[(correlation_id, stage_id)] = status
    extractions = sorted(p.relative_to(root).as_posix() for p in (root / "data/extractions").rglob("*.json"))
    return {"records": current, "versions": versions, "ledger": ledger, "extractions": extractions}


def revisions(root):
    with closing(catalog.connect(catalog.locations(root)[1])) as c:
        return [dict(r) for r in c.execute("SELECT * FROM revisions ORDER BY revision")]


class Equivalence(unittest.TestCase):
    def test_a_batched_ingest_ends_in_the_state_a_one_at_a_time_ingest_ends_in(self):
        root_a, inbox_a = make_instance(self)
        root_b, inbox_b = make_instance(self)
        report_a = cohort.ingest(root_a, inbox_a, system="s", actor="a", batch_size=1)
        report_b = cohort.ingest(root_b, inbox_b, system="s", actor="a", batch_size=4)
        self.assertEqual(report_a["counts"], report_b["counts"])
        self.assertEqual(report_b["counts"]["outcome"]["extracted"], FILES)
        one, many = snapshot(root_a), snapshot(root_b)
        self.assertEqual(one["records"], many["records"])
        self.assertEqual(one["versions"], many["versions"])
        self.assertEqual(one["ledger"], many["ledger"])
        self.assertEqual(one["extractions"], many["extractions"])

    def test_every_batch_of_documents_commits_one_revision_per_stage(self):
        root, inbox = make_instance(self)
        cohort.ingest(root, inbox, system="s", actor="a", batch_size=4)
        # 9 documents in chunks of 4, 4, 1: a receipt revision and a registration revision per chunk
        self.assertEqual([r["record_count"] for r in revisions(root)], [16, 12, 16, 12, 4, 3])
        self.assertEqual(catalog.status(root, audit=True)["revision"], 6)

    def test_the_batch_size_one_path_keeps_two_revisions_per_document(self):
        root, inbox = make_instance(self, 3)
        cohort.ingest(root, inbox, system="s", actor="a", batch_size=1)
        self.assertEqual(len(revisions(root)), 6)

    def test_stage_evidence_names_the_revision_that_committed_each_document_and_the_stages_settle_together(self):
        root, inbox = make_instance(self, 6)
        cohort.ingest(root, inbox, system="s", actor="a", batch_size=3)
        rows = {}
        for request in store.list_requests(root):
            for stage in request["stages"]:
                evidence = stage["evidence"] or {}
                if "committed_revision" in evidence:
                    rows.setdefault(stage["id"], []).append(evidence["committed_revision"])
        self.assertEqual(sorted(set(rows["receipt"])), [1, 3])
        self.assertEqual(sorted(set(rows["register"])), [2, 4])
        self.assertEqual(sorted(rows["receipt"]), [1, 1, 1, 3, 3, 3])
        for revision in (r for r in revisions(root)):
            self.assertTrue(revision["request_id"].startswith("batch-"))

    def test_a_rerun_of_a_finished_ingest_writes_nothing(self):
        root, inbox = make_instance(self, 6)
        first = cohort.ingest(root, inbox, system="s", actor="a", batch_size=3)
        before = (snapshot(root), len(revisions(root)))
        again = cohort.ingest(root, inbox, system="s", actor="a", batch_size=3)
        self.assertEqual(first["counts"], again["counts"])
        self.assertEqual((snapshot(root), len(revisions(root))), before)

    def test_history_written_one_document_at_a_time_is_completed_by_a_batched_rerun_without_duplicates(self):
        root, inbox = make_instance(self, 6)
        cohort.ingest(root, inbox, system="s", actor="a", batch_size=1, limit=3)
        report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=4)
        self.assertEqual(report["counts"]["outcome"]["extracted"], 6)
        self.assertEqual(sorted(set(snapshot(root)["versions"].values())), [1, 2])
        # chunks of 4 and 2 over six documents: three were done, so one document is new in the first
        # chunk (a receipt and a registration revision) and two in the second (the same)
        self.assertEqual(len(revisions(root)), 6 + 4)
        catalog.status(root, audit=True)

    def test_a_non_positive_batch_size_is_refused_before_anything_is_written(self):
        root, inbox = make_instance(self, 2)
        for bad in (0, -1, True, 2.5):
            with self.assertRaises(IntakeError):
                cohort.ingest(root, inbox, system="s", actor="a", batch_size=bad)
        self.assertEqual(revisions(root), [])


def ledger_commits(root):
    """Commits the ledger took so far (SQLite's file change counter)."""
    return int.from_bytes(Path(store.locations(root)[2]).read_bytes()[24:28], "big")


class LedgerCommits(unittest.TestCase):
    """A chunk costs a constant number of ledger commits, whatever its size (Kata gfpd)."""

    def commits_for(self, files, batch_size):
        root, inbox = make_instance(self, files)
        before = ledger_commits(root)
        report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=batch_size)
        self.assertEqual(report["counts"]["outcome"]["failed"], 0)
        return ledger_commits(root) - before, root

    def test_the_commits_of_a_chunk_do_not_grow_with_its_size(self):
        small, _ = self.commits_for(4, 4)
        large, _ = self.commits_for(12, 12)
        self.assertEqual(small, large)
        # a text chunk: its requests staged, its receipts, prepares and registrations each run (intents
        # and attempts, evidence), and its documents recorded: nothing per document
        self.assertLessEqual(small, 12)

    def test_a_single_document_run_writes_its_intent_and_running_attempt_in_one_commit(self):
        root, inbox = make_instance(self, 1)
        before = ledger_commits(root)
        cohort.ingest(root, inbox, system="s", actor="a", batch_size=1)
        # three requests staged (one commit each) and three stages run (intent plus running attempt, then
        # evidence: it was three commits each before the intent and the attempt shared one), and the
        # document's row recorded once
        self.assertEqual(ledger_commits(root) - before, 3 + 3 * 2 + 1)

    def test_the_requests_of_a_chunk_are_the_same_as_staging_each_document_alone(self):
        root_a, inbox_a = make_instance(self, 6)
        root_b, inbox_b = make_instance(self, 6)
        cohort.ingest(root_a, inbox_a, system="s", actor="a", batch_size=1)
        cohort.ingest(root_b, inbox_b, system="s", actor="a", batch_size=6)
        raw = lambda root: dict(sqlite3.connect(store.locations(root)[2]).execute(
            "SELECT correlation_id, raw FROM requests WHERE correlation_id LIKE 'upload-%'").fetchall())
        # the retained records number their IDs across the chunk exactly as one-at-a-time staging does
        # (catalog state tokens differ between the two histories, so compare the records)
        ids = lambda root: {k: [r["id"] for r in json.loads(v)["stages"][0]["arguments"]["records"]]
                            for k, v in raw(root).items()}
        self.assertEqual(ids(root_a), ids(root_b))


class ChunkStaging(unittest.TestCase):
    def test_one_document_is_read_and_published_before_the_next_is_read(self):
        root, inbox = make_instance(self, 4)
        events, staging = [], []
        read, publish, many = upload.read_file, upload.publish, upload.stage_submissions

        def tracked_read(*a, **k):
            if staging:
                events.append("read")
            return read(*a, **k)

        def tracked_publish(*a, **k):
            events.append("publish")
            return publish(*a, **k)
        with patch.object(upload, "stage_submissions", lambda *a, **k: staging.append(1) or many(*a, **k)), \
                patch.object(upload, "read_file", tracked_read), patch.object(upload, "publish", tracked_publish):
            cohort.ingest(root, inbox, system="s", actor="a", batch_size=4)
        # a document's bytes are published (original, then sidecar) before the next document is read
        self.assertEqual(events[:6], ["read", "publish", "publish", "read", "publish", "publish"])

    def test_a_document_whose_descriptor_is_refused_fails_alone(self):
        root, inbox = make_instance(self, 4)
        victim = next(p.relative_to(inbox).as_posix() for p in inbox.rglob("note-000001.*"))
        real = cohort_ingest._descriptor

        def refusing(entry, *args):
            if entry["relative"] == victim:
                raise IntakeError("invalid_origin")
            return real(entry, *args)
        with patch.object(cohort_ingest, "_descriptor", refusing):
            report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=4)
        outcomes = {e["relative"]: e for e in report["entries"] if "outcome" in e}
        self.assertEqual((outcomes[victim]["outcome"], outcomes[victim]["code"]), ("failed", "invalid_origin"))
        self.assertEqual(sum(e["outcome"] == "extracted" for e in outcomes.values()), 3)


class StageMany(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        store.initialize(self.root)
        catalog.initialize(self.root)

    def raw(self, name, parent=None):
        return store.request_json(self.root, name, [{
            "id": "prepare", "kind": "extraction", "operation": "extract-prepare",
            "arguments": {"receipt": "D1", "format_name": "text"}}], parent)

    def test_every_request_is_staged_in_one_commit_and_equals_staging_it_alone(self):
        before = ledger_commits(self.root)
        results = store.stage_many(self.root, [self.raw("one"), self.raw("two")])
        self.assertEqual(ledger_commits(self.root) - before, 1)
        self.assertEqual([r["correlation_id"] for r in results], ["one", "two"])
        self.assertEqual(results[0], store.inspect(self.root, "one"))
        self.assertEqual([s["status"] for s in results[1]["stages"]], ["staged"])

    def test_a_request_already_staged_with_the_same_bytes_is_not_written_again(self):
        store.stage(self.root, self.raw("one"))
        before = ledger_commits(self.root)
        self.assertEqual(store.stage_many(self.root, [self.raw("one")])[0]["correlation_id"], "one")
        self.assertEqual(ledger_commits(self.root), before)

    def test_a_refused_request_is_reported_alone_and_writes_nothing(self):
        store.stage(self.root, self.raw("one"))
        other = store.request_json(self.root, "one", [{"id": "prepare", "kind": "extraction",
                                   "operation": "extract-prepare", "arguments": {"receipt": "D2", "format_name": "text"}}])
        results = store.stage_many(self.root, [other, self.raw("two"), self.raw("three", "missing"), b"{}"])
        self.assertEqual([r.code if isinstance(r, IntakeError) else r["correlation_id"] for r in results],
                         ["correlation_conflict", "two", "unknown_correction_parent", "invalid_request"])
        self.assertEqual(store.inspect(self.root, "one")["stages"][0]["status"], "staged")
        self.assertIsNone(store.find(self.root, "three"))

    def test_a_correction_may_follow_its_parent_in_the_same_call(self):
        results = store.stage_many(self.root, [self.raw("parent"), self.raw("child", "parent")])
        self.assertEqual(results[1]["correction_of"], "parent")
        self.assertEqual(store.head(self.root, "parent")["correlation_id"], "child")

    def test_a_request_repeated_in_one_call_is_staged_once(self):
        results = store.stage_many(self.root, [self.raw("one"), self.raw("one")])
        self.assertEqual([r["correlation_id"] for r in results], ["one", "one"])
        self.assertEqual(len(store.list_requests(self.root)), 1)

    def test_a_failure_during_the_write_stages_none_of_them(self):
        with patch.object(store.docs, "repoint", side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(IntakeError):
                store.stage_many(self.root, [self.raw("parent"), self.raw("child", "parent")])
        self.assertEqual(store.list_requests(self.root), [])


class UnfinishedAttemptsAreNotCharged(unittest.TestCase):
    def test_a_kill_after_the_intent_and_attempt_commit_resumes_the_same_attempt(self):
        root, inbox = make_instance(self, 1)
        cohort.ingest(root, inbox, system="s", actor="a", batch_size=1, limit=0)
        script = ("import os, sys\nfrom pathlib import Path\nsys.path.insert(0, %r)\n"
                  "from tools.intake import adapters, cohort\n"
                  "adapters.execute = lambda *a, **k: os._exit(9)\n"
                  "cohort.ingest(Path(%r), Path(%r), system='s', actor='a', batch_size=1)\n") % (
                      str(ROOT), str(root), str(inbox))
        self.assertEqual(subprocess.run([sys.executable, "-c", script], cwd=ROOT).returncode, 9)
        running = [(r["correlation_id"], s["attempts"]) for r in store.list_requests(root)
                   for s in r["stages"] if s["status"] == "running"]
        self.assertEqual([(c.split("-")[0], len(a)) for c, a in running], [("upload", 1)])
        self.assertIsNone(running[0][1][0]["outcome"])
        report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=1)
        self.assertEqual(report["counts"]["outcome"]["extracted"], 1)
        attempts = [len(s["attempts"]) for r in store.list_requests(root) for s in r["stages"]]
        self.assertEqual(attempts, [1, 1, 1])  # the interrupted attempt finished; no second one was charged


class FailedDocumentDoesNotBlockItsBatch(unittest.TestCase):
    def test_a_file_changed_after_classification_fails_alone(self):
        root, inbox = make_instance(self, 6)
        real = cohort_ingest.classify
        victim = next(p.relative_to(inbox).as_posix() for p in inbox.rglob("note-000002.*"))

        def classify_then_change(*args):
            entries = real(*args)
            (inbox / victim).write_text("changed after the classification\n")
            return entries
        with patch.object(cohort_ingest, "classify", classify_then_change):
            report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=6)
        outcomes = {e["relative"]: e["outcome"] for e in report["entries"] if "outcome" in e}
        self.assertEqual(outcomes[victim], "changed-since-intake")
        self.assertEqual(sum(o == "extracted" for o in outcomes.values()), 5)
        self.assertEqual(len(revisions(root)), 2)  # the other five still share one receipt and one registration revision
        self.assertEqual(catalog.status(root, audit=True)["revision"], 2)

    def test_a_registration_another_writer_overlapped_is_blocked_alone_and_the_rest_commit(self):
        root, inbox = make_instance(self, 5)
        real = extract.register_batch
        seen = []

        def overlapping(root_, items, **kwargs):
            if not seen:
                seen.append(1)
                victim = items[1]["prepared"][1]  # the second document's original artifact
                rewritten = json.loads(json.dumps(victim))
                rewritten["data"]["reason"] = "Rewritten by another writer after the registrations were pinned."
                catalog.submit(root_, [rewritten], actor="other", request_id="other-writer", reason="t",
                               expected_state=catalog.status(root_)["state_token"])
            return real(root_, items, **kwargs)
        with patch.object(extract, "register_batch", overlapping):
            report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=5)
        outcomes = [e for e in report["entries"] if "outcome" in e]
        failed = [e for e in outcomes if e["outcome"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0]["stage"], failed[0]["code"]), ("register", "catalog-conflict"))
        self.assertEqual(sum(e["outcome"] == "extracted" for e in outcomes), 4)
        self.assertEqual(report["counts"]["outcome"]["failed"], 1)
        catalog.status(root, audit=True)

    def test_a_document_whose_extraction_check_fails_is_blocked_alone(self):
        root, inbox = make_instance(self, 5)
        real = extract.unchanged

        def failing(root_, value):
            if value["source"]["receipt_id"] == "D3":
                raise extract.ExtractionError("Source changed during extraction/registration")
            return real(root_, value)
        with patch.object(extract, "unchanged", failing):
            report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=5)
        outcomes = [e["outcome"] for e in report["entries"] if "outcome" in e]
        self.assertEqual(sorted(set(outcomes)), ["extracted", "failed"])
        self.assertEqual(outcomes.count("extracted"), 4)
        # the failed stage is blocked like any refused stage; its explicit replay, then a rerun, completes it alone
        blocked = [(r["correlation_id"], s["id"]) for r in store.list_requests(root)
                   for s in r["stages"] if s["status"] == "blocked"]
        self.assertEqual(len(blocked), 1)
        runner.replay(root, *blocked[0])
        before = len(revisions(root))
        again = cohort.ingest(root, inbox, system="s", actor="a", batch_size=5)
        self.assertEqual(again["counts"]["outcome"]["extracted"], 5)
        self.assertEqual(len(revisions(root)), before + 1)
        self.assertEqual(sorted(set(snapshot(root)["versions"].values())), [1, 2])


class MarkerScope(unittest.TestCase):
    """Marker idempotency applies to upload submissions only; other receipt updates still write."""

    def test_an_update_of_a_committed_receipt_that_keeps_its_original_artifact_is_written(self):
        root, inbox = make_instance(self, 2)
        cohort.ingest(root, inbox, system="s", actor="a", batch_size=2)
        records = catalog.read_records(root, limit=100)["records"]
        receipt = next(r for r in records if r["kind"] == "receipt")
        original = next(r for r in records if r["id"] == receipt["data"]["artifact_id"])
        updated = json.loads(json.dumps(receipt))
        updated["data"]["provenance"] = [{"path": "originals/uploads/x/extra.json", "sha256": None,
                                          "line_start": None, "line_end": None}]
        for batched in (False, True):
            store.stage(root, json.dumps({
                "version": 1, "correlation_id": "req-update-%s" % batched, "correction_of": None,
                "policy": {"version": 1, "sha256": upload.policy.for_root(root)[1]},
                "retry": {"max_attempts": 3, "backoff_seconds": 0},
                "stages": [{"id": "receipt", "kind": "receipt", "operation": "catalog-submit", "arguments": {
                    "records": [updated, original], "actor": "a", "request_id": "update-%s" % batched,
                    "reason": "Receipt update", "expected_state": catalog.status(root)["state_token"]}}]}).encode())
            before = len(revisions(root))
            if batched:
                runner.settle_batch(root, [("req-update-True", "receipt")])
            else:
                runner.settle(root, "req-update-False", "receipt")
            self.assertEqual(len(revisions(root)), before + 1, batched)
            if not batched:
                updated["data"]["provenance"][0]["path"] += "2"  # a different update for the second pass
        current = {r["id"]: r for r in catalog.read_records(root, limit=100)["records"]}
        self.assertEqual(current[receipt["id"]]["data"]["provenance"], updated["data"]["provenance"])

    def test_only_an_upload_registration_has_a_marker(self):
        records = catalog_records_for_upload()
        args = {"records": records, "request_id": "upload-sub"}
        self.assertEqual(adapters.receipt_marker(args), "A-upload-sub-original")
        self.assertIsNone(adapters.receipt_marker({**args, "request_id": "update-1"}))
        self.assertIsNone(adapters.receipt_marker({"records": records[:1], "request_id": "upload-sub"}))
        self.assertIsNone(adapters.receipt_marker({"records": records, "request_id": "upload-other"}))


def catalog_records_for_upload():
    from tools.diagnostics.catalog_lock_benchmark import registration
    records = registration(0)
    for record in records:
        record["id"] = record["id"].replace("A-upload-0-", "A-upload-sub-")
        for key in ("artifact_id", "parent_id"):
            if record["data"].get(key):
                record["data"][key] = record["data"][key].replace("A-upload-0-", "A-upload-sub-")
    return records


class BatchWideFailures(unittest.TestCase):
    def test_extraction_lock_contention_fails_the_batch_retryably_instead_of_aborting_the_run(self):
        root, inbox = make_instance(self, 4)
        real = extract.register_batch
        calls = []

        def busy_once(root_, items, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise catalog.WriterBusy("Another catalog writer is active; retry later")
            return real(root_, items, **kwargs)
        with patch.object(extract, "register_batch", busy_once):
            report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=4)
        self.assertEqual(report["counts"]["outcome"]["extracted"], 4)
        self.assertEqual(report["counts"]["failed_by_code"], {})
        self.assertEqual(report["counts"]["retried_after_conflict"]["entries"], 4)

    def test_a_persistent_extraction_lock_is_recorded_per_stage_and_the_run_reports(self):
        root, inbox = make_instance(self, 3)
        with patch.object(extract, "register_batch", side_effect=catalog.WriterBusy("held")):
            report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=3)
        self.assertEqual(report["counts"]["outcome"]["received"], 3)
        self.assertEqual(report["counts"]["failed_by_code"], {"register/catalog-busy": 3})
        for request in store.list_requests(root):
            for stage in request["stages"]:
                self.assertNotEqual(stage["status"], "running")  # no attempt is left unfinished
        again = cohort.ingest(root, inbox, system="s", actor="a", batch_size=3)
        self.assertEqual(again["counts"]["outcome"]["extracted"], 3)


class RetriesAndBusyStores(unittest.TestCase):
    """The batched path keeps the single path's treatment of lost races and a busy ledger."""

    def test_a_catalog_busy_during_a_batch_is_retried_and_counted_per_entry(self):
        root, inbox = make_instance(self, 4)
        real = catalog.submit_batch
        calls = []

        def busy_once(root_, members, **kwargs):
            calls.append(len(members))
            if len(calls) == 2:  # the registration batch loses one race, before anything is written
                raise catalog.WriterBusy("Another catalog writer is active; retry later")
            return real(root_, members, **kwargs)
        with patch.object(catalog, "submit_batch", busy_once):
            report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=4)
        self.assertEqual(report["counts"]["outcome"]["extracted"], 4)
        self.assertEqual(report["counts"]["retried_after_conflict"], {"entries": 4, "succeeded": 4, "attempts": 4})
        self.assertEqual(report["counts"]["failed_by_code"], {})
        self.assertEqual(len(revisions(root)), 2)

    def test_a_busy_ledger_repeats_the_chunk_and_then_fails_visibly_without_stopping_the_run(self):
        root, inbox = make_instance(self, 4)
        real = upload.stage_submissions
        calls = []

        def busy_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise LedgerBusy()
            return real(*args, **kwargs)
        with patch.object(upload, "stage_submissions", busy_once):
            report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=4)
        self.assertEqual(report["counts"]["outcome"]["extracted"], 4)
        self.assertEqual(report["counts"]["retried_after_conflict"]["entries"], 4)
        with patch.object(upload, "stage_submissions", side_effect=LedgerBusy()):
            again = cohort.ingest(root, inbox, system="s2", actor="a", batch_size=4)
        self.assertEqual(again["counts"]["failed_by_code"], {"ledger/ledger_busy": 4})

    def test_a_blocked_registration_is_recoverable_by_recover_conflicts_like_any_other(self):
        root, inbox = make_instance(self, 2)
        legacy = catalog.CatalogError("Stale expected state; re-read before writing")
        real = catalog.submit_batch
        calls = []

        def failing(root_, members, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise legacy
            return real(root_, members, **kwargs)
        with patch.object(catalog, "submit_batch", failing):
            report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=2)
        self.assertEqual(report["counts"]["failed_by_code"], {"register/native-input": 2})
        self.assertEqual(runner.recover_conflicts(root)["candidates"], 2)
        self.assertEqual(runner.recover_conflicts(root, apply=True)["results"], {"succeeded": 2})
        self.assertEqual(cohort.ingest(root, inbox, system="s", actor="a", batch_size=2)["counts"]["outcome"]["extracted"], 2)


CRASH = """
import os, sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from tools.catalog import catalog_store as catalog
from tools.extraction import extract
from tools.intake import adapters, cohort, runner, upload, store
root, inbox = Path({root!r}), Path({inbox!r})
point, nth = {point!r}, {nth}
counter = {{"n": 0}}

def hit():
    counter["n"] += 1
    if counter["n"] == nth:
        os._exit(9)

def wrap(owner, name, before=None, after=None):
    real = getattr(owner, name)
    def wrapped(*args, **kwargs):
        if before: before(kwargs)
        result = real(*args, **kwargs)
        if after: after()
        return result
    setattr(owner, name, wrapped)

if point == "before-catalog-commit":      # inside the catalog transaction, before COMMIT
    def guard(kwargs):
        counter["n"] += 1
        if counter["n"] == nth:
            kwargs["precommit_check"] = lambda: os._exit(9)
    wrap(catalog, "submit_batch", before=guard)
elif point == "between-catalog-members":  # between two member inserts inside the transaction
    real_commit = catalog._commit_revision
    def commit(writer, incoming, **kwargs):
        counter["n"] += 1
        if counter["n"] == nth:
            kwargs["precommit_check"] = lambda: os._exit(9)
        return real_commit(writer, incoming, **kwargs)
    catalog._commit_revision = commit
elif point == "after-catalog-commit":     # committed, ledger evidence not yet saved
    wrap(adapters, "execute_batch", after=hit)
elif point == "after-requests-staged":    # a chunk's requests are one commit; killed once it landed
    wrap(store, "stage_many", after=hit)
elif point == "before-native-work":       # intents and running attempts committed, the effect not yet run
    wrap(adapters, "execute_batch", before=lambda kwargs: hit())
elif point == "after-ledger-update":      # everything of this batch is durable
    wrap(runner, "run_batch", after=hit)
elif point == "between-ledger-members":   # inside the ledger transaction that records the evidence
    real_save = runner.save_artifact
    def save(c, correlation, stage, key, value):
        real_save(c, correlation, stage, key, value)
        if key == "evidence":
            hit()
    runner.save_artifact = save
elif point == "between-retained-copies":  # between two documents' retained-copy publication
    wrap(upload, "publish", after=hit)
elif point == "between-completion-files": # after some documents' completion.json, before the rest
    wrap(extract, "finish_registration", after=hit)
elif point == "between-prepare-guards":   # after some documents' extraction was prepared
    wrap(extract, "run", after=hit)
cohort.ingest(root, inbox, system="s", actor="a", batch_size={size})
"""

POINTS = [
    ("before-catalog-commit", 1), ("before-catalog-commit", 2),
    ("between-catalog-members", 1), ("between-catalog-members", 2),
    ("after-catalog-commit", 1), ("after-catalog-commit", 2),
    ("after-ledger-update", 1), ("after-ledger-update", 2), ("after-ledger-update", 3),
    ("after-requests-staged", 1), ("after-requests-staged", 2), ("after-requests-staged", 3),
    ("before-native-work", 1), ("before-native-work", 2), ("before-native-work", 3),
    ("between-ledger-members", 3), ("between-ledger-members", 7),
    ("between-retained-copies", 3), ("between-completion-files", 2), ("between-prepare-guards", 4),
]


class CrashInjection(unittest.TestCase):
    """A process killed at any step leaves a state that a re-run completes, with no duplicate or lost document."""

    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        base = Path(temp.name)
        cls.clean_root, cls.clean_inbox = base / "instance", base / "inbox"
        cls.clean_root.mkdir()
        store.initialize(cls.clean_root)
        catalog.initialize(cls.clean_root)
        fixture(cls.clean_inbox, 5, mixed=True)
        cohort.ingest(cls.clean_root, cls.clean_inbox, system="s", actor="a", batch_size=5)
        cls.clean = snapshot(cls.clean_root)

    def crash(self, point, nth, size=5):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        root, inbox = base / "instance", base / "inbox"
        root.mkdir()
        store.initialize(root)
        catalog.initialize(root)
        fixture(inbox, 5, mixed=True)
        script = CRASH.format(repo=str(ROOT), root=str(root), inbox=str(inbox), point=point, nth=nth, size=size)
        process = subprocess.run([sys.executable, "-c", script], cwd=ROOT)
        self.assertEqual(process.returncode, 9, f"{point} #{nth} was never reached")
        journal = Path(str(catalog.locations(root)[1]) + "-journal")
        if journal.exists():
            # A killed writer's rollback journal needs the explicit recovery, as for any writer. A kill
            # before SQLite synced the journal leaves one with no valid header: nothing reached the
            # database file, SQLite never treats it as hot, and an operator removes it.
            try:
                catalog.migrate_indexes(root, recover=True)
            except catalog.CatalogError:
                self.assertEqual(journal.read_bytes()[:8], bytes(8))
                journal.unlink()
        return root, inbox

    def assert_completed_once(self, root, inbox):
        report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=5)
        self.assertEqual(report["counts"]["outcome"]["extracted"], 5, report["counts"])
        self.assertEqual(report["counts"]["outcome"]["failed"], 0)
        done = snapshot(root)
        self.assertEqual(done["records"], self.clean["records"])      # no lost, duplicate or divergent document
        self.assertEqual(done["versions"], self.clean["versions"])    # no record written twice
        self.assertEqual(done["ledger"], self.clean["ledger"])
        self.assertEqual(done["extractions"], self.clean["extractions"])
        self.assertEqual(catalog.status(root, audit=True)["records"], len(self.clean["records"]))
        again = (snapshot(root), len(revisions(root)))
        cohort.ingest(root, inbox, system="s", actor="a", batch_size=5)
        self.assertEqual((snapshot(root), len(revisions(root))), again)  # and a second re-run changes nothing

    def test_a_kill_at_each_step_is_completed_by_a_rerun(self):
        for point, nth in POINTS:
            with self.subTest(point=point, nth=nth):
                self.assert_completed_once(*self.crash(point, nth))

    def test_a_rerun_with_a_different_batch_size_also_completes(self):
        root, inbox = self.crash("after-catalog-commit", 1)
        report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=2)
        self.assertEqual(report["counts"]["outcome"]["extracted"], 5)
        self.assertEqual(snapshot(root)["records"], self.clean["records"])
        self.assertEqual(snapshot(root)["versions"], self.clean["versions"])

    def test_a_kill_after_the_receipt_commit_reports_the_committed_revision_on_the_rerun(self):
        root, inbox = self.crash("after-catalog-commit", 1)
        before = len(revisions(root))
        cohort.ingest(root, inbox, system="s", actor="a", batch_size=5)
        receipts = [s["evidence"]["committed_revision"] for r in store.list_requests(root)
                    for s in r["stages"] if s["id"] == "receipt"]
        self.assertEqual(set(receipts), {1})
        self.assertEqual(len(revisions(root)), before + 1)  # only the registration batch was missing


class Contention(unittest.TestCase):
    def test_two_processes_ingesting_distinct_inboxes_into_one_instance_commit_every_document_once(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        root = base / "instance"
        root.mkdir()
        store.initialize(root)
        catalog.initialize(root)
        for name in ("one", "two"):
            fixture(base / name, 8, seed=name, mixed=True)
        script = ("import sys, json\nfrom pathlib import Path\nsys.path.insert(0, %r)\n"
                  "from tools.intake import cohort\n"
                  "r = cohort.ingest(Path(%r), Path(sys.argv[1]), system=sys.argv[2], actor='a', batch_size=4)\n"
                  "print(json.dumps(r['counts']['outcome']))\n" % (str(ROOT), str(root)))
        processes = [subprocess.Popen([sys.executable, "-c", script, str(base / name), name],
                                      cwd=ROOT, stdout=subprocess.PIPE, text=True) for name in ("one", "two")]
        outputs = [p.communicate(timeout=900)[0] for p in processes]
        for output in outputs:
            counts = json.loads(output.strip().splitlines()[-1])
            self.assertEqual(counts["failed"], 0, counts)
            self.assertEqual(counts["extracted"], 8)
        # a re-run of the same inboxes (every document already present) finds nothing to do
        done = snapshot(root)
        self.assertEqual(sum(1 for r in done["records"].values() if r["kind"] == "receipt"), 16)
        self.assertEqual(sorted(set(done["versions"].values())), [1, 2])
        catalog.status(root, audit=True)
        sources = [r["data"]["source_ids"][0] for r in done["records"].values() if r["kind"] == "receipt"]
        self.assertEqual(len(set(sources)), 16)


if __name__ == "__main__":
    unittest.main()
