"""Zero-result absence classification and the body-free retrieval diagnostics log."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from tools.context_index import context_index as index
from tools.diagnostics import retrieval_log as diagnostics
from tests.support import WorkspaceCase


class AbsenceClassificationTests(WorkspaceCase):
    """A zero-result search must explain itself without quoting the corpus."""

    def corpus(self, documents):
        (self.root / "documents").mkdir(exist_ok=True)
        for name, text in documents.items():
            (self.root / "documents" / name).write_text(text)
        self.config["documents"] = [
            {"path": f"documents/{name}", "role": "toolkit-documentation", "context": []}
            for name in documents
        ]
        self.build()

    def absence(self, query, path=None):
        with closing(index.readonly(self.db)) as db:
            result = index.search(db, query, 5, path, root=self.root)
            self.assertEqual(result["hits"], [])
            return result["absence"]

    def test_absent_corpus_distinguishes_collection_gaps(self):
        self.corpus({"a.md": "# Alpha\n\nalphaonly material.\n"})
        absence = self.absence("zzgapone zzgaptwo")
        self.assertEqual(absence["classification"], "absent-corpus")
        self.assertEqual((absence["terms"], absence["absent_terms"]), (2, 2))
        self.assertEqual(absence["sections_matching_any"], 0)
        self.assertIn("not evidence that the subject does not exist", absence["caveat"])

    def test_partial_and_uncombined_are_separate_signals(self):
        self.corpus({"a.md": "# Alpha\n\nalphaonly material.\n",
                     "b.md": "# Beta\n\nbetaonly material.\n"})
        partial = self.absence("alphaonly zzgapone")
        self.assertEqual(partial["classification"], "absent-partial")
        self.assertEqual((partial["terms"], partial["absent_terms"]), (2, 1))
        uncombined = self.absence("alphaonly betaonly")
        self.assertEqual(uncombined["classification"], "present-uncombined")
        self.assertEqual(uncombined["absent_terms"], 0)
        self.assertEqual(uncombined["sections_matching_any"], 2)
        self.assertIn("retrieval or selection limit", uncombined["caveat"])

    def test_sections_matching_any_counts_distinct_sections_when_terms_overlap(self):
        self.corpus({"a.md": "# Alpha\n\nalphaonly betaonly material.\n"})
        partial = self.absence("alphaonly betaonly zzgapone")
        self.assertEqual(partial["classification"], "absent-partial")
        self.assertEqual((partial["terms"], partial["absent_terms"]), (3, 1))
        self.assertEqual(partial["sections_matching_any"], 1)

    def test_classification_respects_the_requested_path_scope(self):
        self.corpus({"a.md": "# Alpha\n\nalphaonly material.\n",
                     "b.md": "# Beta\n\nbetaonly material.\n"})
        absence = self.absence("alphaonly", "documents/b.md")
        self.assertEqual(absence["classification"], "absent-corpus")
        self.assertEqual(absence["scope"], "path")

    def test_absence_never_echoes_query_terms_or_source_text(self):
        self.corpus({"a.md": "# Alpha\n\nalphaonly confidential material.\n"})
        absence = self.absence("alphaonly zzsecretterm")
        serialized = json.dumps(absence)
        for leaked in ("zzsecretterm", "alphaonly", "confidential"):
            self.assertNotIn(leaked, serialized)
        self.assertEqual(set(absence), {"classification", "terms", "absent_terms",
                                        "sections_matching_any", "scope", "caveat"})

    def test_successful_search_carries_no_absence_report(self):
        self.corpus({"a.md": "# Alpha\n\nalphaonly material.\n"})
        with closing(index.readonly(self.db)) as db:
            result = index.search(db, "alphaonly", 5, None, root=self.root)
        self.assertTrue(result["hits"])
        self.assertNotIn("absence", result)


class RetrievalDiagnostics(unittest.TestCase):
    """The diagnostics log must refuse anything that is not a bounded, body-free count."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.log = diagnostics.Log(self.root, ".local/diagnostics/retrieval.jsonl")

    def stamps(self, minutes=0):
        completed = datetime(2026, 10, 22, 12, minutes, tzinfo=timezone.utc)
        return {"started_at": (completed - timedelta(milliseconds=5)).isoformat(),
                "completed_at": completed.isoformat(), "duration_ms": 5}

    def search_payload(self, hits, absence=None):
        payload = {"command": "search", "hits": [{"locator": "documents/a.md#x"}] * hits}
        if absence is not None:
            payload["absence"] = absence
        return payload

    def test_records_a_classified_empty_search(self):
        record = self.log.record_search(self.search_payload(0, {
            "classification": "absent-corpus", "terms": 2, "absent_terms": 2,
            "sections_matching_any": 0, "scope": "selection", "caveat": "ignored"}), **self.stamps())
        self.assertEqual(record["outcome"], "empty")
        self.assertEqual(record["classification"], "absent-corpus")
        self.assertNotIn("caveat", record)

    def test_refuses_query_text_and_unknown_fields(self):
        for extra in ({"query": "secret"}, {"locator": "documents/a.md#x"},
                      {"excerpt": "source text"}, {"terms": "two"}):
            with self.subTest(extra=extra), self.assertRaises(diagnostics.DiagnosticsError):
                self.log.write(run_id=str(uuid.uuid4()), operation="search",
                               outcome="empty", **self.stamps(), **extra)

    def test_refuses_contradictory_classifications(self):
        contradictions = [
            {"classification": "absent-corpus", "terms": 2, "absent_terms": 1, "scope": "selection"},
            {"classification": "present-uncombined", "terms": 2, "absent_terms": 2, "scope": "selection"},
            {"classification": "absent-partial", "terms": 2, "absent_terms": 2, "scope": "selection"},
            {"classification": "absent-corpus", "terms": 1, "absent_terms": 2, "scope": "selection"},
        ]
        for fields in contradictions:
            with self.subTest(fields=fields), self.assertRaises(diagnostics.DiagnosticsError):
                self.log.write(run_id=str(uuid.uuid4()), operation="search",
                               outcome="empty", **self.stamps(), **fields)

    def test_refuses_outcome_and_count_disagreement(self):
        with self.assertRaises(diagnostics.DiagnosticsError):
            self.log.write(run_id=str(uuid.uuid4()), operation="search", outcome="hit",
                           hits=0, **self.stamps())
        with self.assertRaises(diagnostics.DiagnosticsError):
            self.log.write(run_id=str(uuid.uuid4()), operation="search", outcome="empty",
                           hits=3, **self.stamps())

    def test_error_records_keep_class_names_not_messages(self):
        record = self.log.record_error("ToolError", **self.stamps())
        self.assertEqual(record["error_type"], "ToolError")
        with self.assertRaises(diagnostics.DiagnosticsError):
            self.log.record_error("Reader failed: /private/path", **self.stamps())

    def test_log_is_private_and_appended(self):
        self.log.record_search(self.search_payload(2), **self.stamps())
        self.log.record_search(self.search_payload(1), **self.stamps(1))
        self.assertEqual(stat.S_IMODE(self.log.path.stat().st_mode), 0o600)
        self.assertEqual(len(diagnostics.read_records(self.log.path)), 2)

    def test_report_separates_collection_from_findability(self):
        self.log.record_search(self.search_payload(0, {
            "classification": "absent-corpus", "terms": 2, "absent_terms": 2,
            "sections_matching_any": 0, "scope": "selection"}), **self.stamps())
        self.log.record_search(self.search_payload(0, {
            "classification": "present-uncombined", "terms": 2, "absent_terms": 0,
            "sections_matching_any": 4, "scope": "selection"}), **self.stamps(1))
        self.log.record_search(self.search_payload(3), **self.stamps(2))
        summary = diagnostics.report(self.log.path)
        self.assertEqual(summary["searches"], 3)
        self.assertEqual(summary["outcomes"], {"empty": 2, "hit": 1})
        self.assertEqual(summary["empty_classifications"],
                         {"absent-corpus": 1, "present-uncombined": 1})
        self.assertAlmostEqual(summary["collection_gap_rate"], 1 / 3, places=4)
        self.assertAlmostEqual(summary["findability_gap_rate"], 1 / 3, places=4)
        self.assertTrue(any("not store" in limit or "not stored" in limit
                            for limit in summary["limits"]))

    def test_pruning_is_explicit_and_reports_both_counts(self):
        for minute in range(3):
            self.log.record_search(self.search_payload(1), **self.stamps(minute))
        result = diagnostics.prune(self.log.path, 1)
        self.assertEqual((result["before"], result["after"], result["removed"]), (3, 1, 2))
        self.assertEqual(len(diagnostics.read_records(self.log.path)), 1)

    def test_prune_and_write_serialize_across_shared_lock_file(self):
        for minute in range(3):
            self.log.record_search(self.search_payload(1), **self.stamps(minute))
        entered_read = threading.Event()
        release_read = threading.Event()
        original_read_records = diagnostics.read_records

        def paused_read_records(path):
            records = original_read_records(path)
            entered_read.set()
            release_read.wait(timeout=5)
            return records

        writer_result = {}

        def concurrent_writer():
            entered_read.wait(timeout=5)
            writer_result["record"] = self.log.record_search(self.search_payload(2), **self.stamps(10))

        writer = threading.Thread(target=concurrent_writer)
        writer.start()
        with patch.object(diagnostics, "read_records", side_effect=paused_read_records):
            threading.Timer(0.05, release_read.set).start()
            result = diagnostics.prune(self.log.path, 1)
        writer.join(timeout=5)
        self.assertEqual((result["before"], result["after"], result["removed"]), (3, 1, 2))
        records = diagnostics.read_records(self.log.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1]["run_id"], writer_result["record"]["run_id"])

    def test_reads_are_counted_but_never_attributed(self):
        """A read is counted as a bare event, carrying no locator, excerpt or count."""
        record = self.log.record_read({"command": "get", "pieces": [
            {"locator": "documents/a.md#x", "text": "secret source text"}]}, **self.stamps())
        self.assertEqual((record["operation"], record["outcome"]), ("read", "hit"))
        self.assertEqual(set(record) - diagnostics.BASE, set())
        serialized = json.dumps(record)
        for leaked in ("documents/a.md", "secret source text", "locator", "pieces"):
            self.assertNotIn(leaked, serialized)

    def test_read_records_refuse_search_only_fields(self):
        """Term counts and absence classification describe a search; a read cannot carry them."""
        for extra in ({"hits": 1}, {"terms": 2}, {"scope": "selection"},
                      {"classification": "absent-corpus", "terms": 1,
                       "absent_terms": 1, "scope": "selection"}):
            with self.subTest(extra=extra), self.assertRaises(diagnostics.DiagnosticsError):
                self.log.write(run_id=str(uuid.uuid4()), operation="read",
                               outcome="hit", **self.stamps(), **extra)
        with self.assertRaises(diagnostics.DiagnosticsError):
            self.log.write(run_id=str(uuid.uuid4()), operation="read",
                           outcome="empty", **self.stamps())

    def test_report_counts_reads_without_deriving_a_ratio(self):
        """Raw search and read counts are reported; no quality ratio is computed from them."""
        self.log.record_search(self.search_payload(3), **self.stamps())
        self.log.record_search(self.search_payload(0, {
            "classification": "absent-corpus", "terms": 1, "absent_terms": 1,
            "sections_matching_any": 0, "scope": "selection"}), **self.stamps(1))
        self.log.record_read({"command": "get"}, **self.stamps(2))
        self.log.record_read({"command": "get-many"}, **self.stamps(3))
        summary = diagnostics.report(self.log.path)
        self.assertEqual((summary["searches"], summary["reads"]), (2, 2))
        self.assertEqual(summary["outcomes"], {"hit": 1, "empty": 1})
        self.assertNotIn("read_per_answered_search", summary)
        self.assertNotIn("read_per_answered_search", summary["interpretation"])
        self.assertTrue(any("No search-to-read ratio" in limit for limit in summary["limits"]))
        self.assertAlmostEqual(summary["collection_gap_rate"], 0.5, places=4)
        self.assertTrue(any("not a basis for pruning" in limit for limit in summary["limits"]))

    def test_rejects_unsafe_log_paths(self):
        for value in ("../escape.jsonl", "retrieval.txt"):
            with self.subTest(value=value), self.assertRaises(diagnostics.DiagnosticsError):
                diagnostics.log_path(self.root, value)


if __name__ == "__main__":
    unittest.main()
