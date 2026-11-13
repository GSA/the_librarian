"""One parse per document per ingest, with every changed-source/extractor re-check kept (Kata rqqq)."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from tools.catalog import catalog_store as catalog
from tools.extraction import extract
from tools.intake import adapters, runner, store, upload


def stage(operation, arguments, name):
    return {"id": name, "kind": "extraction", "operation": operation, "arguments": arguments}


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)
        extract.forget_parsed()
        self.addCleanup(extract.forget_parsed)
        source = Path(self.temp.name) / "notes.md"
        source.write_bytes(b"# Notes\nLine two\n")
        self.ack = upload.submit(self.root, source, upload.descriptor(
            submission_id="sub-1", format_name="text", name="notes.md",
            actor="test-runner", reason="Extraction reuse test"))
        self.receipt = self.ack["records"]["receipt_id"]
        self.original = self.root / self.ack["original"]["path"]

    def request(self):
        base = {"receipt": self.receipt, "format_name": "text"}
        raw = json.dumps({"version": 1, "correlation_id": "req-native", "correction_of": None,
                          "policy": {"version": 1, "sha256": upload.policy.for_root(self.root)[1]},
                          "retry": {"max_attempts": 2, "backoff_seconds": 0},
                          "stages": [stage("extract-prepare", base, "prepare"),
                                     stage("extract-register",
                                           {**base, "actor": "operator",
                                            "expected_state": catalog.status(self.root)["state_token"]},
                                           "register")]}).encode()
        store.stage(self.root, raw)

    def last_code(self, name):
        return store.inspect(self.root, "req-native")["stages"][{"prepare": 0, "register": 1}[name]]["attempts"][-1]["code"]


class ParseOnceTests(Case):
    def test_both_stages_of_one_document_parse_it_once(self):
        self.request()
        with patch.object(extract, "text", wraps=extract.text) as parse:
            runner.run(self.root, "req-native", "prepare")
            self.assertTrue(runner.run(self.root, "req-native", "register")["complete"])
        self.assertEqual(parse.call_count, 1)

    def test_each_stage_reads_the_catalog_and_source_twice_not_three_times(self):
        """Stage prepare and the execute guard are fresh reads; the guard's bundle is what ``run`` uses."""
        self.request()
        with patch.object(extract, "bundle", wraps=extract.bundle) as bundle:
            runner.run(self.root, "req-native", "prepare")
            runner.run(self.root, "req-native", "register")
        self.assertEqual(bundle.call_count, 4)

    def test_a_repeat_bundle_reuses_the_parse_only_for_identical_content_and_extractor(self):
        self.request()
        with patch.object(extract, "text", wraps=extract.text) as parse:
            first = extract.bundle(self.root, self.receipt, "text")
            again = extract.bundle(self.root, self.receipt, "text")
            self.assertEqual((first[2], first[3]), (again[2], again[3]))
            self.assertEqual(parse.call_count, 1)
            real = extract.fingerprint
            with patch.object(extract, "fingerprint", lambda r, f: real(r, f)[::-1]):
                extract.bundle(self.root, self.receipt, "text")
            self.assertEqual(parse.call_count, 2)
            self.original.write_bytes(b"# Notes\nLine 2!!\n")  # remembered parse must not serve new bytes
            with self.assertRaisesRegex(extract.ExtractionError, "catalog hash"):
                extract.bundle(self.root, self.receipt, "text")
            self.assertEqual(parse.call_count, 2)

    def test_a_failed_parse_is_never_remembered(self):
        self.request()
        with patch.object(extract, "text", side_effect=extract.ExtractionError("refused")) as parse:
            for _ in range(2):
                with self.assertRaises(extract.ExtractionError):
                    extract.bundle(self.root, self.receipt, "text")
        self.assertEqual(parse.call_count, 2)


class ChangedInputTests(Case):
    """A change after an earlier read still blocks, whether or not a parse was remembered."""

    def test_source_edited_between_prepare_and_register_blocks_registration(self):
        self.request()
        runner.run(self.root, "req-native", "prepare")  # the parse of the old bytes is now remembered
        self.original.write_bytes(b"# Notes\nLine 2!!\n")  # same size, different bytes
        before = catalog.status(self.root)["state_token"]
        result = runner.run(self.root, "req-native", "register")
        self.assertEqual(result["stages"][1]["status"], "blocked")
        self.assertEqual(self.last_code("register"), "native-input")
        self.assertEqual(catalog.status(self.root)["state_token"], before)

    def test_source_edited_after_intent_was_saved_blocks_the_execute_guard(self):
        self.request()
        real = adapters.execute

        def edit_then_execute(root, stage, intent, previous=None):
            self.original.write_bytes(b"# Notes\nLine 2!!\n")
            return real(root, stage, intent, previous)
        with patch.object(adapters, "execute", edit_then_execute):
            result = runner.run(self.root, "req-native", "prepare")
        self.assertEqual(result["stages"][0]["status"], "blocked")
        self.assertEqual(self.last_code("prepare"), "native-input")
        self.assertFalse((self.root / "data/extractions").exists()
                         and any((self.root / "data/extractions").rglob("extraction.json")))

    def test_extractor_changed_after_intent_was_saved_blocks_as_a_changed_identity(self):
        self.request()
        real_execute, real_print = adapters.execute, extract.fingerprint

        def change_extractor_then_execute(root, stage, intent, previous=None):
            with patch.object(extract, "fingerprint", lambda r, f: real_print(r, f)[::-1]):
                return real_execute(root, stage, intent, previous)
        with patch.object(adapters, "execute", change_extractor_then_execute):
            result = runner.run(self.root, "req-native", "prepare")
        self.assertEqual(result["stages"][0]["status"], "blocked")
        self.assertEqual(self.last_code("prepare"), "changed-extraction-identity")

    def test_catalog_identity_changed_after_intent_was_saved_blocks_as_a_changed_identity(self):
        """The audience is part of the pinned source identity; the guard sees the record's new value."""
        self.request()
        runner.run(self.root, "req-native", "prepare")
        original = catalog.read_records(self.root, receipt=self.receipt)["records"]
        record = next(r for r in original if r["id"] == self.ack["records"]["artifact_id"])
        changed = json.loads(json.dumps(record))
        changed["data"]["audience"] = "agent"
        catalog.submit(self.root, [changed], actor="test-runner", request_id="audience-change",
                       reason="Test change", expected_state=catalog.status(self.root)["state_token"])
        result = runner.run(self.root, "req-native", "register")
        self.assertEqual(result["stages"][1]["status"], "blocked")


class LockedRecheckTests(Case):
    """``run`` reuses a bundle it is given but still re-reads the source and extractor under the writer lock."""

    def bundle(self):
        self.request()
        return extract.bundle(self.root, self.receipt, "text")

    def test_source_edited_after_the_bundle_is_refused_before_publication(self):
        prepared = self.bundle()
        self.original.write_bytes(b"# Notes\nLine 2!!\n")
        with self.assertRaisesRegex(extract.ExtractionError, "Source changed during extraction"):
            extract.run(self.root, "prepare", self.receipt, "text", prepared=prepared)
        self.assertFalse(prepared[2].exists())

    def test_source_edited_during_publication_is_refused_after_it(self):
        prepared = self.bundle()
        real = extract.publish

        def edit_during_publish(path, raw):
            created = real(path, raw)
            self.original.write_bytes(b"# Notes\nLine 2!!\n")
            return created
        with patch.object(extract, "publish", edit_during_publish), \
                self.assertRaisesRegex(extract.ExtractionError, "Source changed during extraction"):
            extract.run(self.root, "prepare", self.receipt, "text", prepared=prepared)

    def test_extractor_changed_after_the_bundle_is_refused_under_the_lock(self):
        prepared = self.bundle()
        real = extract.fingerprint
        with patch.object(extract, "fingerprint", lambda r, f: real(r, f)[::-1]), \
                self.assertRaisesRegex(extract.ExtractionError, "Extractor changed during operation"):
            extract.run(self.root, "prepare", self.receipt, "text", prepared=prepared)

    def test_source_edited_before_registration_submits_nothing(self):
        self.bundle()
        extract.run(self.root, "prepare", self.receipt, "text")
        prepared = extract.bundle(self.root, self.receipt, "text")
        self.original.write_bytes(b"# Notes\nLine 2!!\n")
        before = catalog.status(self.root)["state_token"]
        with self.assertRaisesRegex(extract.ExtractionError, "Source changed during extraction"):
            extract.run(self.root, "register", self.receipt, "text", "operator", before, prepared=prepared)
        self.assertEqual(catalog.status(self.root)["state_token"], before)


if __name__ == "__main__":
    unittest.main()
