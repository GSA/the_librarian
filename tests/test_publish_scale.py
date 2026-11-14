"""Publishing a cohort-sized selection manifest stays linear and bounded (Kata jysk).

Synthetic selections in temporary workspaces only. The diagnostic that builds them
(``tools/diagnostics/publish_benchmark.py``) is the fixture, so the numbers it prints and these tests
describe the same shape.
"""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import policy
from tools.context_index import context_index as index, renditions
from tools.diagnostics import publish_benchmark as bench
from tools.intake import cohort
from tools.review_events import event_reader


class PublishScaleTests(unittest.TestCase):
    @classmethod
    def build(cls, count):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name) / "instance"
        root.mkdir()
        bench.build(root, count)
        return temp, root

    def count_calls(self, count, target, name):
        temp, root = self.build(count)
        self.addCleanup(temp.cleanup)
        config, _ = index.load_manifest(root / bench.MANIFEST, root)
        calls = [0]
        real = getattr(target, name)

        def counted(*args, **kwargs):
            calls[0] += 1
            return real(*args, **kwargs)
        with mock.patch.object(target, name, counted):
            selected, _ = renditions.inputs(root, config, index.rendition_api(root))
        self.assertEqual(len(selected), count)
        return calls[0]

    def test_policy_is_read_a_constant_number_of_times_per_inputs_call(self):
        small = self.count_calls(8, policy, "for_root")
        large = self.count_calls(24, policy, "for_root")
        self.assertEqual(small, large)

    def test_review_event_index_is_built_once_per_snapshot(self):
        small = self.count_calls(8, event_reader.Snapshot, "_index_events")
        large = self.count_calls(24, event_reader.Snapshot, "_index_events")
        self.assertEqual((small, large), (1, 1))

    def test_snapshot_index_follows_the_events_it_holds(self):
        first = {"event_id": "rev-a"}
        snapshot = event_reader.Snapshot({"state_token": "t" * 64}, [first], {})
        self.assertIs(snapshot.event_index()["rev-a"], first)
        second = {"event_id": "rev-b"}
        snapshot.events.append(second)
        self.assertIs(snapshot.event_index()["rev-b"], second)
        snapshot.events = [second]
        self.assertEqual(set(snapshot.event_index()), {"rev-b"})

    def test_a_cohort_sized_manifest_is_accepted_and_one_more_than_the_cap_is_not(self):
        self.assertGreaterEqual(renditions.MAX_SELECTIONS, 20000)
        api = index.rendition_api()
        api.validate_context = lambda context: None
        api.eligible = lambda path: True
        api.canonical_locator = lambda value: value

        def selections(count):
            return [{"path": f"data/extractions/.local/D{n + 1}/{'a' * 64}/extraction.json",
                     "sha256": "b" * 64, "receipt_id": f"D{n + 1}", "format": "text",
                     "context": ["documents/context.md"]} for n in range(count)]
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        renditions.validate({"renditions": selections(renditions.MAX_SELECTIONS)}, api, root=root)
        with self.assertRaises(index.IndexError_):
            renditions.validate({"renditions": selections(renditions.MAX_SELECTIONS + 1)}, api, root=root)

    def test_publish_serves_every_selection(self):
        temp, root = self.build(12)
        self.addCleanup(temp.cleanup)
        report = cohort.publish(root, policy_path=bench.POLICY, manifest=bench.MANIFEST)
        self.assertEqual(report["counts"], {"published": 1, "selections": 12})
        self.assertEqual(report["publication"], {"status": "succeeded", "available": True})
        with index.readonly(root / cohort.DEFAULT_INDEX) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM rendition_documents").fetchone()[0], 12)


if __name__ == "__main__":
    unittest.main()
