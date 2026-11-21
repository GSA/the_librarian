"""Integrity audit and verified reads of retained files (Kata xv0n)."""
import hashlib
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests import test_workspace
from tools import policy
from tests.support import WorkspaceCase
from tools.catalog import catalog_store as catalog
from tools.context_index import context_index as index
from tools.context_index import forked, integrity


def tree(root):
    """Every path under ``root`` with its size and mtime: what a read-only command must leave alone."""
    found = {}
    for directory, names, files in os.walk(root):
        for name in names + files:
            path = os.path.join(directory, name)
            info = os.lstat(path)
            found[path] = (info.st_size, info.st_mtime_ns)
    return found


class IntegrityTests(WorkspaceCase):
    selected_text = test_workspace.WorkspaceTests.selected_text

    def setUp(self):
        super().setUp()
        self.prepared = self.selected_text()
        self.extraction = self.root / self.prepared["rendition"]
        self.original = self.root / "sources/notes.txt"

    def items(self, report):
        return sorted((item["path"], item["kind"], item["problem"]) for item in report["items"])

    def test_a_clean_instance_reports_clean_and_writes_nothing(self):
        before = tree(self.root)
        report = integrity.audit(self.root)
        self.assertTrue(report["clean"])
        self.assertEqual((report["mismatches"], report["items"]), (0, []))
        self.assertGreaterEqual(report["checked"], 2)
        self.assertEqual(tree(self.root), before)

    def test_with_no_catalog_receipt_the_audit_validates_and_still_writes_nothing(self):
        home = self.root / policy.validate_instance_roots(self.root)["catalog_home"]
        receipt = home / ".local/read-validation.json"
        self.assertTrue(receipt.exists())
        receipt.unlink()
        before = tree(self.root)
        self.assertTrue(integrity.audit(self.root)["clean"])
        self.assertEqual(integrity.read_verified(self.root, "sources/notes.txt"), self.original.read_bytes())
        self.assertEqual(tree(self.root), before)
        self.assertFalse(receipt.exists())
        with patch.object(catalog, "_write_receipt", side_effect=AssertionError("wrote a receipt")):
            self.assertTrue(integrity.audit(self.root)["clean"])

    def test_each_kind_of_change_is_reported_with_path_kind_expected_and_found(self):
        pinned = hashlib.sha256(self.original.read_bytes()).hexdigest()
        changed = b"First\r\n\xce\xb2\nlast"  # same size
        self.original.write_bytes(changed)
        self.extraction.write_bytes(self.extraction.read_bytes() + b" ")
        report = integrity.audit(self.root)
        self.assertFalse(report["clean"])
        self.assertEqual(self.items(report), sorted([
            ("sources/notes.txt", "original", "hash-differs"),
            (self.prepared["rendition"], "extraction", "hash-differs")]))
        entry = next(item for item in report["items"] if item["kind"] == "original")
        self.assertEqual((entry["expected"], entry["found"]), (pinned, hashlib.sha256(changed).hexdigest()))

    def test_missing_unreadable_symlinked_and_non_regular_files_are_reported(self):
        moved = self.extraction.with_name("moved.json")
        self.extraction.rename(moved)
        self.extraction.symlink_to(moved)
        self.assertEqual(self.items(integrity.audit(self.root)),
                         [(self.prepared["rendition"], "extraction", "symlink")])
        self.extraction.unlink()
        self.assertEqual(self.items(integrity.audit(self.root)),
                         [(self.prepared["rendition"], "extraction", "missing")])
        moved.rename(self.extraction)
        self.original.unlink()
        self.original.mkdir()
        self.assertEqual(self.items(integrity.audit(self.root)), [("sources/notes.txt", "original", "not-a-file")])

    def test_the_audit_lists_at_most_limit_mismatches_but_counts_them_all(self):
        self.original.write_bytes(b"x")
        self.extraction.write_bytes(b"y")
        report = integrity.audit(self.root, limit=1)
        self.assertEqual((report["mismatches"], report["listed"], len(report["items"])), (2, 1, 1))

    def test_forked_and_sequential_audits_agree(self):
        self.original.write_bytes(b"changed")
        sequential = integrity.audit(self.root)
        answers = []
        real = forked.run

        def recording(*args):
            answers.append(real(*args))
            return answers[-1]
        with patch.object(integrity, "PARALLEL_MIN", 1), patch.object(forked, "run", recording):
            parallel = integrity.audit(self.root, workers=2)
        self.assertIsNotNone(answers[0], "the workers did the audit")
        self.assertEqual(parallel, sequential)

    def test_a_broken_pool_falls_back_to_the_sequential_audit(self):
        self.original.write_bytes(b"changed")
        expected = integrity.audit(self.root)
        with patch.object(integrity, "PARALLEL_MIN", 1), \
                patch("concurrent.futures.ProcessPoolExecutor", side_effect=OSError("no processes")):
            self.assertEqual(integrity.audit(self.root, workers=2), expected)

    def test_a_changed_file_does_not_stop_reads_but_a_verified_read_refuses_it(self):
        self.original.write_bytes(b"changed")
        config, checksum = index.load_manifest(self.manifest, self.root)
        from contextlib import closing
        with closing(index.readonly(self.db)) as db:
            self.assertEqual(index.freshness(db, self.root, self.manifest, self.db, config, checksum, reuse=True), [])
        with self.assertRaises(integrity.IntegrityMismatch) as refused:
            integrity.read_verified(self.root, "sources/notes.txt")
        self.assertEqual(refused.exception.mismatch["problem"], "hash-differs")
        self.assertEqual(integrity.read_verified(self.root, self.prepared["rendition"]),
                         self.extraction.read_bytes())

    def test_a_verified_read_returns_the_pinned_bytes_and_refuses_unpinned_paths(self):
        self.assertEqual(integrity.read_verified(self.root, "sources/notes.txt"), self.original.read_bytes())
        (self.root / "sources/other.txt").write_bytes(b"not registered")
        for path in ("sources/other.txt", "sources/missing.txt", "../outside"):
            with self.subTest(path), self.assertRaises(index.IndexError_):
                integrity.read_verified(self.root, path)

    def test_the_audit_follows_the_catalog_when_a_path_is_re_pinned(self):
        """Document-update ingestion will re-pin a path through the catalog; the audit then expects the new hash."""
        self.original.write_bytes(b"updated by a controlled ingest")
        self.assertFalse(integrity.audit(self.root)["clean"])
        record = next(r for r in catalog.read_records(self.root, receipt="D1")["records"] if r["id"] == "A-input")
        record["data"]["sha256"] = hashlib.sha256(self.original.read_bytes()).hexdigest()
        record["data"]["bytes"] = len(self.original.read_bytes())
        catalog.submit(self.root, [record], actor="test-runner", request_id="repin-original",
                       expected_state=catalog.status(self.root)["state_token"], reason="Controlled update")
        self.assertTrue(integrity.audit(self.root)["clean"])

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = integrity.main(["--root", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_command_line_exit_status_is_zero_clean_one_mismatch_two_not_run(self):
        code, out, _ = self.run_cli("audit")
        self.assertEqual((code, json.loads(out)["clean"]), (0, True))
        self.original.write_bytes(b"changed")
        code, out, _ = self.run_cli("audit")
        self.assertEqual((code, json.loads(out)["items"][0]["path"]), (1, "sources/notes.txt"))
        code, out, _ = self.run_cli("check", "sources/notes.txt", self.prepared["rendition"])
        self.assertEqual((code, json.loads(out)["mismatches"]), (1, 1))
        empty = Path(self.root / "empty")
        empty.mkdir()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(integrity.main(["--root", str(empty), "audit"]), 2)
        self.assertIn("integrity:", err.getvalue())


class PinnedIdentityTests(WorkspaceCase):
    selected_text = test_workspace.WorkspaceTests.selected_text
    registration = test_workspace.WorkspaceTests.registration

    def setUp(self):
        super().setUp()
        self.prepared = self.selected_text()
        self.config, _ = index.load_manifest(self.manifest, self.root)
        self.api = index.rendition_api(self.root)

    def test_reviewed_selections_bind_the_review_state_through_the_non_full_read(self):
        from tests.support import ROOT  # noqa: F401  (the fixture root is a temporary instance)
        from types import SimpleNamespace
        from tools.review_events import event_reader
        self.config["renditions"][0]["review_event_ids"] = ["rev-pending"]
        calls = []

        def snapshot(token):
            def read(root, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(state={"state_token": token})
            return read
        identities = []
        for token in ("1" * 64, "1" * 64, "2" * 64):
            with patch.object(event_reader, "read_snapshot", snapshot(token)):
                identities.append(index.renditions.pinned_identity(self.root, self.config, self.api))
        self.assertEqual(identities[0], identities[1])
        self.assertNotEqual(identities[0], identities[2])
        self.assertFalse(any(call.get("full") for call in calls), "freshness must not ask for the full replay")
        self.assertEqual(len(calls), 3)

    def test_a_pin_the_catalog_contradicts_refuses_like_the_full_check(self):
        child = next(r for r in catalog.read_records(self.root, receipt="D1")["records"]
                     if r["data"]["role"] == "generated-rendition")
        child["data"]["sha256"] = "0" * 64
        catalog.submit(self.root, [child], actor="test-runner", request_id="bad-pin",
                       expected_state=catalog.status(self.root)["state_token"], reason="Contradicting pin")
        with self.assertRaisesRegex(index.IndexError_, "Rendition/source registration mismatch"):
            index.renditions.pinned_identity(self.root, self.config, self.api)
        with self.assertRaisesRegex(index.IndexError_, "Rendition/source registration mismatch"):
            index.renditions.inputs(self.root, self.config, self.api, keep=False)

    def test_a_selection_naming_an_unregistered_receipt_refuses(self):
        self.config["renditions"][0]["receipt_id"] = "D9"
        with self.assertRaises(index.IndexError_):
            index.renditions.pinned_identity(self.root, self.config, self.api)
