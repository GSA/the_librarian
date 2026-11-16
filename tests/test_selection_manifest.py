"""The selection manifest is appended in constant time and stays byte-identical to a full rewrite (Kata m0tg)."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import policy
from tools.context_index import context_index as index
from tools.context_index import renditions
from tools.intake import selection_manifest as manifest
from tools.intake import standing

NAME = "config/selection.json"
CONTEXT = ["documents/context.md"]


def entry(n, fmt="text"):
    return {"path": f"data/extractions/.local/D{n}/{n:064x}/extraction.json", "sha256": f"{n:064x}",
            "receipt_id": f"D{n}", "format": fmt, "context": list(CONTEXT),
            "review_event_ids": [f"rev-{n}"]}


class ManifestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "documents").mkdir()
        (self.root / "documents/context.md").write_text("# Context\n")
        self.path = self.root / NAME
        manifest.forget()
        self.addCleanup(manifest.forget)

    def write(self, renditions_=()):
        raw = {"schema_version": 1, "audience": policy.for_root(self.root)[0]["audience"],
               "documents": [], "context_rules": {}, "renditions": list(renditions_)}
        self.path.write_text(json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    def full_rewrite(self, entries):
        raw = json.loads(self.path.read_text())
        raw["renditions"] = entries
        return (json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()

    def add(self, item):
        return standing.write_manifest(self.root, NAME, item)

    def disk(self):
        return json.loads(self.path.read_text())["renditions"]


class AppendEquivalence(ManifestCase):
    def test_an_append_leaves_the_exact_bytes_of_a_full_rewrite_and_returns_their_digest(self):
        self.write([entry(1)])
        for n in range(2, 8):
            expected = self.full_rewrite(self.disk() + [entry(n)])
            digest = self.add(entry(n))
            self.assertEqual(self.path.read_bytes(), expected)
            self.assertEqual(digest, index.digest(expected))
        self.assertEqual(manifest.view(self.root, NAME).digest(), index.digest(self.path.read_bytes()))
        index.load_manifest(self.path, self.root)

    def test_the_first_selection_of_an_empty_list_takes_the_full_rewrite_and_later_ones_append(self):
        self.write()
        self.assertIsNone(manifest.view(self.root, NAME).end)
        self.add(entry(1))
        self.assertIsNotNone(manifest.view(self.root, NAME).end)
        self.add(entry(2))
        self.assertEqual([e["receipt_id"] for e in self.disk()], ["D1", "D2"])

    def test_an_append_never_reads_the_whole_manifest_again(self):
        self.write([entry(n) for n in range(1, 30)])
        manifest.view(self.root, NAME)
        with patch.object(index, "load_manifest", side_effect=AssertionError("full read")):
            for n in range(30, 40):
                self.add(entry(n))
                self.assertEqual(manifest.view(self.root, NAME).count, n)
        self.assertEqual(len(self.disk()), 39)

    def test_a_retry_of_a_selected_entry_is_idempotent_and_a_conflict_is_refused(self):
        self.write([entry(1)])
        before = self.path.read_bytes()
        self.assertEqual(self.add(entry(1)), index.digest(before))
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(standing.Refusal) as caught:
            self.add({**entry(1), "sha256": "f" * 64})
        self.assertEqual(caught.exception.code, "selection-conflict")

    def test_capacity_is_refused_from_the_view(self):
        self.write([entry(1), entry(2)])
        small = type("R", (), {**vars(renditions), "MAX_SELECTIONS": 2})
        with patch.object(standing, "renditions", small):
            with self.assertRaises(standing.Refusal) as caught:
                self.add(entry(3))
        self.assertEqual(caught.exception.code, "selection-capacity")
        self.assertEqual(len(self.disk()), 2)

    def test_an_invalid_entry_is_refused_and_writes_nothing(self):
        self.write([entry(1)])
        before = self.path.read_bytes()
        for bad in ({**entry(2), "format": "pdf"}, {**entry(2), "sha256": "xyz"},
                    {**entry(2), "context": []}, {**entry(2), "path": "elsewhere/x.json"}):
            with self.assertRaises(index.IndexError_):
                self.add(bad)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(os.path.lexists(manifest.intent_path(self.root, NAME)))


class StaleViews(ManifestCase):
    def test_a_foreign_edit_is_never_served_from_the_view(self):
        self.write([entry(1), entry(2)])
        self.assertEqual(manifest.view(self.root, NAME).count, 2)
        self.write([entry(1), entry(2), entry(3)])  # another process rewrote the file
        self.assertEqual(manifest.view(self.root, NAME).count, 3)
        self.assertTrue(manifest.view(self.root, NAME).has(entry(3)["path"]))

    def test_a_same_size_edit_with_a_restored_mtime_is_seen_because_ctime_moves(self):
        self.write([entry(1)])
        manifest.view(self.root, NAME)
        before = os.stat(self.path)
        data = self.path.read_bytes()
        self.path.write_bytes(data.replace(b"rev-1", b"rev-9"))
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(os.stat(self.path).st_size, before.st_size)
        with patch.object(manifest.index, "load_manifest", wraps=index.load_manifest) as loaded:
            manifest.view(self.root, NAME)
        self.assertEqual(loaded.call_count, 1)

    def test_a_tampered_manifest_is_refused_by_the_next_read_and_never_appended_to(self):
        self.write([entry(1)])
        manifest.view(self.root, NAME)
        self.path.write_text(self.path.read_text().replace('"format": "text"', '"format": "pdf"'))
        with self.assertRaises(index.IndexError_):
            manifest.view(self.root, NAME)
        with self.assertRaises(index.IndexError_):
            self.add(entry(2))

    def test_a_manifest_that_is_not_canonical_is_appended_by_the_full_path(self):
        self.write([entry(1)])
        raw = json.loads(self.path.read_text())
        self.path.write_text(json.dumps(raw))  # valid, but not the canonical rendering
        self.assertIsNone(manifest.view(self.root, NAME).end)
        self.add(entry(2))
        self.assertEqual([e["receipt_id"] for e in self.disk()], ["D1", "D2"])
        self.assertIsNotNone(manifest.view(self.root, NAME).end)

    def test_a_symlinked_manifest_is_refused(self):
        self.write([entry(1)])
        link = self.root / "config/link.json"
        link.symlink_to(self.path)
        with self.assertRaises(index.IndexError_):
            manifest.view(self.root, "config/link.json")


class CrashRecovery(ManifestCase):
    def crash_after_intent(self, item):
        with patch.object(os, "pwrite", side_effect=OSError(5, "crash")):
            with self.assertRaises(OSError):
                self.add(item)

    def test_a_crash_before_the_manifest_write_is_rolled_forward_by_the_next_writer(self):
        self.write([entry(1)])
        before = self.path.read_bytes()
        self.crash_after_intent(entry(2))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(os.path.lexists(manifest.intent_path(self.root, NAME)))
        self.assertTrue(manifest.recover(self.root, NAME))
        self.assertEqual(self.path.read_bytes(), self.full_rewrite([entry(1), entry(2)]))
        self.assertFalse(os.path.lexists(manifest.intent_path(self.root, NAME)))

    def test_a_torn_write_is_completed_from_the_intent(self):
        self.write([entry(1)])
        self.crash_after_intent(entry(2))
        record = json.loads(manifest.intent_path(self.root, NAME).read_text())
        with open(self.path, "r+b") as stream:  # half of the new bytes reached the file
            stream.seek(record["offset"])
            stream.write(record["text"].encode()[:20])
        manifest.recover(self.root, NAME)
        self.assertEqual(self.path.read_bytes(), self.full_rewrite([entry(1), entry(2)]))

    def test_a_crash_after_the_write_leaves_only_a_harmless_intent(self):
        self.write([entry(1)])
        self.crash_after_intent(entry(2))
        manifest.recover(self.root, NAME)
        self.assertTrue(manifest.recover(self.root, NAME) is False)
        self.assertEqual(self.add(entry(2)), index.digest(self.path.read_bytes()))
        self.assertEqual(len(self.disk()), 2)

    def test_a_short_write_is_completed_and_a_stalled_one_keeps_the_intent(self):
        self.write([entry(1)])
        real = os.pwrite
        calls = []

        def short(descriptor, data, offset):
            calls.append(len(data))
            return real(descriptor, bytes(data)[:7], offset)  # at most seven bytes per call
        with patch.object(os, "pwrite", short):
            self.add(entry(2))
        self.assertGreater(len(calls), 1)
        self.assertEqual(self.path.read_bytes(), self.full_rewrite([entry(1), entry(2)]))
        self.assertFalse(os.path.lexists(manifest.intent_path(self.root, NAME)))
        with patch.object(os, "pwrite", lambda descriptor, data, offset: 0):
            with self.assertRaises(OSError):
                self.add(entry(3))
        self.assertTrue(os.path.lexists(manifest.intent_path(self.root, NAME)))  # nothing is lost
        manifest.recover(self.root, NAME)
        self.assertEqual(self.path.read_bytes(), self.full_rewrite([entry(1), entry(2), entry(3)]))

    def test_an_intent_for_a_file_that_changed_since_is_dropped_without_writing(self):
        self.write([entry(1)])
        self.crash_after_intent(entry(2))
        self.write([entry(1), entry(3)])
        before = self.path.read_bytes()
        manifest.recover(self.root, NAME)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(os.path.lexists(manifest.intent_path(self.root, NAME)))

    def test_a_torn_intent_is_dropped(self):
        self.write([entry(1)])
        manifest.intent_path(self.root, NAME).write_text('{"offset": 3, "pre')
        before = self.path.read_bytes()
        manifest.recover(self.root, NAME)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(os.path.lexists(manifest.intent_path(self.root, NAME)))


if __name__ == "__main__":
    unittest.main()
