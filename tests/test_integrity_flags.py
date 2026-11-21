"""A changed retained file flags its documents instead of failing the index (Kata ejm8)."""
from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
import os

from tests import test_workspace
from tests.support import WorkspaceCase
from tools.context_index import cli, confidence, flags, integrity
from tools.context_index import context_index as index
from tools.context_index import packets
from tools.intake import progress, progress_render


class FlagTests(WorkspaceCase):
    selected_text = test_workspace.WorkspaceTests.selected_text

    def setUp(self):
        super().setUp()
        self.prepared = self.selected_text()
        self.extraction = self.root / self.prepared["rendition"]
        self.original = self.root / "sources/notes.txt"
        self.rendition = self.prepared["rendition"]

    def audit(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = integrity.main(["--root", str(self.root), "audit", "--record", *extra])
        self.assertIn(code, (0, 1), err.getvalue())
        return json.loads(out.getvalue())

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--root", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def search(self, **kwargs):
        with closing(index.readonly(self.db)) as db:
            return index.search(db, "first", 10, None, root=self.root, **kwargs)

    def test_a_clean_recorded_audit_flags_nothing(self):
        report = self.audit()
        self.assertEqual((report["clean"], report["recorded"]), (True, True))
        status = json.loads(self.run_cli("status")[1])
        self.assertEqual(status["integrity"]["flagged_documents"], 0)
        self.assertEqual(status["integrity"]["audit"], "recorded")
        self.assertEqual(status["fresh"], True)

    def test_without_a_recorded_audit_status_says_so_instead_of_implying_clean(self):
        status = json.loads(self.run_cli("status")[1])
        self.assertEqual(status["integrity"]["audit"], "none-for-this-index")
        self.assertIn("audit --record", status["integrity"]["notice"])

    def test_a_changed_file_flags_its_document_and_every_other_document_still_serves(self):
        self.extraction.write_bytes(self.extraction.read_bytes() + b" ")
        self.audit()
        code, out, _ = self.run_cli("status")
        status = json.loads(out)
        self.assertEqual((code, status["fresh"]), (0, True), "a changed file is not a freshness failure")
        flagged = status["integrity"]
        self.assertEqual((flagged["flagged_documents"], flagged["mismatches"]), (1, 1))
        self.assertEqual(flagged["documents"][0]["path"], self.rendition)
        self.assertEqual(flagged["documents"][0]["files"][0]["problem"], "hash-differs")
        self.assertIn("re-ingest", flagged["resolve"])
        found = self.search()
        by_path = {hit["path"]: hit for hit in found["hits"]}
        self.assertIn("integrity-changed", by_path[self.rendition]["confidence"]["reasons"])
        self.assertIn("integrity-changed", found["confidence"]["reasons"])
        # The document is served with its indexed text, marked, and the unflagged documents are unmarked.
        code, out, _ = self.run_cli("get", self.rendition + "#line-1")
        self.assertEqual(code, 0)
        piece = json.loads(out)["pieces"][0]
        self.assertEqual(piece["confidence"]["level"], "low")
        self.assertEqual([r["code"] for r in piece["confidence"]["reasons"]], ["integrity-changed"])
        self.assertIn(self.rendition.split("/")[-1], piece["confidence"]["reasons"][0]["details"][0])
        code, out, err = self.run_cli("get", "documents/README.md#interfaces")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["pieces"][0]["confidence"]["level"], "normal")

    def test_a_changed_original_flags_the_documents_extracted_from_it(self):
        self.original.write_bytes(b"changed original")
        self.audit()
        with closing(index.readonly(self.db)) as db:
            flagged = flags.load(db)
        self.assertEqual(sorted(flagged.documents), [self.rendition])
        self.assertEqual(flagged.documents[self.rendition][0]["kind"], "original")

    def test_low_confidence_exclude_and_last_treat_a_flagged_document_like_any_low_one(self):
        self.audit()
        self.assertIn(self.rendition, [h["path"] for h in self.search(low_confidence="exclude")["hits"]])
        self.extraction.write_bytes(b"x")
        self.audit()
        with closing(index.readonly(self.db)) as db:
            kept = index.search(db, "first", 10, None, root=self.root, low_confidence="exclude")
            only = index.search(db, "first", 10, self.rendition, root=self.root, low_confidence="exclude")
            last = index.search(db, "first", 10, None, root=self.root, low_confidence="last")
        self.assertNotIn(self.rendition, [h["path"] for h in kept["hits"]])
        self.assertEqual((only["hits"], only["low_confidence_excluded"]), ([], 1))
        self.assertEqual(only["filtered"]["classification"], "low-confidence-excluded")
        self.assertEqual(last["hits"][-1]["path"], self.rendition)

    def test_a_clean_re_audit_clears_the_flag(self):
        good = self.extraction.read_bytes()
        self.extraction.write_bytes(good + b" ")
        self.audit()
        self.assertEqual(json.loads(self.run_cli("status")[1])["integrity"]["flagged_documents"], 1)
        self.extraction.write_bytes(good)
        self.audit()
        status = json.loads(self.run_cli("status")[1])
        self.assertEqual(status["integrity"]["flagged_documents"], 0)
        self.assertEqual(self.search()["confidence"]["level"], "normal")

    def test_a_rebuilt_index_does_not_inherit_the_old_report(self):
        good = self.extraction.read_bytes()
        self.extraction.write_bytes(good + b" ")
        self.audit()
        self.extraction.write_bytes(good)
        self.build()
        with closing(index.readonly(self.db)) as db:
            self.assertFalse(flags.load(db).recorded)

    def test_a_forged_foreign_or_unsafe_report_means_no_flags(self):
        self.extraction.write_bytes(b"x")
        self.audit()
        path = flags.location(self.db)
        saved = path.read_text()

        def flagged():
            with closing(index.readonly(self.db)) as db:
                return flags.load(db).recorded

        self.assertTrue(flagged())
        forged = json.loads(saved)
        forged["items"] = []
        path.write_text(json.dumps(forged))
        self.assertFalse(flagged(), "a changed report fails its checksum")
        path.write_text("not json")
        self.assertFalse(flagged())
        path.write_text(saved)
        os.chmod(path, 0o644)
        self.assertFalse(flagged(), "only a private report is trusted")
        os.chmod(path, 0o600)
        path.unlink()
        path.symlink_to(self.root / "elsewhere")
        self.assertFalse(flagged())

    def test_the_audit_does_not_record_for_an_index_replaced_while_it_ran(self):
        with closing(index.readonly(self.db)) as db:
            before = flags.identity(self.db)
            self.assertIsNotNone(db)
        self.build()
        self.assertFalse(flags.record(self.db, integrity.audit(self.root), "now", before))
        self.assertFalse(flags.location(self.db).exists())

    def test_recording_needs_a_built_index_and_audit_without_record_writes_no_report(self):
        out = io.StringIO()
        with redirect_stdout(out):
            integrity.main(["--root", str(self.root), "audit"])
        self.assertFalse(flags.location(self.db).exists())
        self.db.unlink()
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(integrity.main(["--root", str(self.root), "audit", "--record"]), 2)
        self.assertIn("Index missing", err.getvalue())

    def test_flagged_documents_are_listed_up_to_a_cap_with_the_rest_counted(self):
        items = [{"path": f"extractions/r{n}/a.json", "kind": "extraction", "problem": "missing",
                  "expected": "0" * 64, "found": None} for n in range(flags.LISTED + 5)]
        value = flags.Flags(True, "then", 100, len(items), {i["path"]: [i] for i in items}).summary()
        self.assertEqual((len(value["documents"]), value["documents_not_listed"]), (flags.LISTED, 5))
        long = flags.Flags(True, "then", 1, 1, {"p": [{"path": "x" * 1000, "kind": "original",
                                                      "problem": "missing", "expected": None,
                                                      "found": None}]})
        self.assertLess(len(long.summary()["documents"][0]["files"][0]["path"]), 1000)

    def test_get_many_labels_each_document_and_the_legend_explains_the_reason(self):
        self.extraction.write_bytes(b"x")
        self.audit()
        with closing(index.readonly(self.db)) as db:
            result = packets.packet_many(db, self.config, [self.rendition + "#line-1", "documents/README.md"],
                                         60000, root=self.root)
        labels = {d["path"]: d["confidence"]["level"] for d in result["documents"].values()}
        self.assertEqual(labels[self.rendition], "low")
        self.assertEqual(labels["documents/README.md"], "normal")
        self.assertIn("integrity-changed", result["confidence"]["reasons"])
        self.assertIn("integrity-changed", confidence.REASONS)

    def test_the_progress_panel_counts_and_lists_the_flagged_documents_with_the_resolution(self):
        self.extraction.write_bytes(b"x")
        self.audit()
        panel = progress.read_index(self.root)
        self.assertEqual((panel["fresh"], panel["integrity"]["flagged_documents"]), (True, 1))
        report = {"index": {**panel}}
        lines = progress_render.integrity_lines(panel["integrity"])
        self.assertIn("1 flagged documents", lines[0])
        self.assertTrue(any(self.rendition in line for line in lines))
        self.assertTrue(any("resolve:" in line for line in lines))
        self.assertEqual(progress_render.integrity_lines(None), [])
        self.assertIn("no audit recorded", progress_render.integrity_lines(
            flags.Flags().summary())[0])
        del report

    def test_a_changed_original_flags_only_its_own_renditions(self):
        """A changed file nothing selected relies on flags no document; a relied-on one flags only its own."""
        other = flags._documents(self.connection(), [
            {"path": "sources/unrelated.txt", "kind": "original", "problem": "missing"},
            {"path": "documents/other/submission.json", "kind": "sidecar", "problem": "missing",
             "receipt": "D-other"}])
        self.assertEqual(other, {})
        self.original.write_bytes(b"changed original")
        self.audit()
        status = json.loads(self.run_cli("status")[1])["integrity"]
        self.assertEqual(status["flagged_documents"], 1)
        self.assertTrue(all(d["files"] for d in status["documents"]))
        with closing(index.readonly(self.db)) as db:
            kept = index.search(db, "first", 10, None, root=self.root, low_confidence="exclude")
        self.assertTrue(kept["hits"], "unflagged documents still answer an excluding search")
        self.assertNotIn(self.rendition, [h["path"] for h in kept["hits"]])

    def connection(self):
        connection = index.readonly(self.db)
        self.addCleanup(connection.close)
        return connection

    def test_every_flagged_document_is_recorded_however_many_files_changed(self):
        """More mismatches than any detail cap still flag every affected document."""
        report = {"checked": 10, "mismatches": 3, "items": [
            {"path": self.rendition, "kind": "extraction", "problem": "missing", "expected": "a", "found": None}]
            + [{"path": f"sources/other{n}.txt", "kind": "original", "problem": "missing", "expected": "a",
                "found": None} for n in range(2)]}
        before = flags.identity(self.db)
        self.assertTrue(flags.record(self.db, report, "now", before))
        flagged = flags.load(self.connection())
        self.assertEqual(sorted(flagged.documents), [self.rendition])
        many = {"checked": 10, "mismatches": 9000, "items": [
            {"path": self.rendition, "kind": "extraction", "problem": "hash-differs", "expected": "a",
             "found": "b", "receipt": "D1"}] * 1}
        self.assertTrue(flags.record(self.db, many, "now", before))
        self.assertEqual(flags.load(self.connection()).mismatches, 9000)
        saved = json.loads(flags.location(self.db).read_text())
        self.assertNotIn("items", saved, "the report keeps documents, not one entry per changed file")


class PublicationTests(WorkspaceCase):
    selected_text = test_workspace.WorkspaceTests.selected_text

    def test_a_build_refuses_every_changed_selected_file_at_once_and_keeps_the_prior_index(self):
        prepared = self.selected_text()
        prior = self.db.read_bytes()
        extraction = self.root / prepared["rendition"]
        extraction.write_bytes(extraction.read_bytes() + b" ")
        (self.root / "sources/notes.txt").write_bytes(b"changed original")
        with self.assertRaises(index.IndexError_) as refused:
            self.build()
        message = str(refused.exception)
        self.assertIn("Publication refused: 2 selected file(s)", message)
        self.assertIn("extraction " + prepared["rendition"] + ": hash-differs", message)
        self.assertIn("original sources/notes.txt: hash-differs", message)
        self.assertIn("expected ", message)
        self.assertEqual(self.db.read_bytes(), prior, "the prior index is preserved")
        with closing(index.readonly(self.db)) as db:
            self.assertEqual(index.search(db, "first", 5, None, root=self.root)["hits"][0]["path"],
                             prepared["rendition"])

    def test_a_missing_extraction_and_a_changed_original_are_both_named(self):
        prepared = self.selected_text()
        (self.root / prepared["rendition"]).unlink()
        (self.root / "sources/notes.txt").write_bytes(b"changed original")
        with self.assertRaises((index.IndexError_, ValueError)) as refused:
            self.build()
        message = str(refused.exception)
        self.assertIn("Publication refused: 2 selected file(s)", message)
        self.assertIn("extraction " + prepared["rendition"] + ": missing", message)
        self.assertIn("original sources/notes.txt: hash-differs", message)

    def test_other_publication_failures_keep_their_own_message(self):
        self.selected_text()
        self.config["renditions"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(index.IndexError_, "Selected rendition hash changed"):
            self.build()
