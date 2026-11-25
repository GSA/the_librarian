"""Inline ``data:`` payloads: the reduced form a reviewer and the search index see."""
import hashlib
import unittest

from tools.context_index import inline_data


def uri(size, mime="image/png", params=";base64"):
    return f"data:{mime}{params}," + "A" * size


class ReduceTests(unittest.TestCase):
    def test_large_payload_becomes_a_placeholder_naming_mime_size_and_hash(self):
        text = "before " + uri(5000) + " after"
        reduced, images = inline_data.reduce_text(text)
        digest = hashlib.sha256(("A" * 5000).encode()).hexdigest()
        self.assertEqual(reduced, f"before [inline data omitted: image/png, 5000 bytes, sha256 {digest[:16]}] after")
        self.assertEqual(images, [{"mime": "image/png", "bytes": 5000, "sha256": digest}])

    def test_small_payload_and_plain_text_are_unchanged(self):
        for text in ("plain words", "see " + uri(inline_data.MIN_PAYLOAD - 1), "data: not a uri"):
            with self.subTest(text=text[:20]):
                self.assertEqual(inline_data.reduce_text(text), (text, []))

    def test_mime_less_and_parameterised_forms_reduce(self):
        for form in (uri(2000, params=";base64"), "data:," + "B" * 2000, uri(2000, "image/svg+xml", ";charset=utf-8;base64")):
            with self.subTest(form=form[:30]):
                reduced, images = inline_data.reduce_text("x " + form + " y")
                self.assertTrue(reduced.startswith("x [inline data omitted: ") and reduced.endswith("] y"))
                self.assertEqual(len(images), 1)

    def test_reduce_value_walks_nested_node_fields(self):
        node = {"text": "t " + uri(3000), "attributes": {"href": uri(1500, "image/gif")}, "n": 3}
        seen = []
        reduced = inline_data.reduce_value(node, seen)
        self.assertNotIn("AAAA", repr(reduced))
        self.assertEqual(reduced["n"], 3)
        self.assertEqual(sorted(i["mime"] for i in seen), ["image/gif", "image/png"])


class SummaryTests(unittest.TestCase):
    def records(self, *texts):
        return [{"line": n + 1, "text": text} for n, text in enumerate(texts)]

    def test_no_inline_data_means_no_summary(self):
        self.assertIsNone(inline_data.summary(self.records("a", "b")))

    def test_summary_counts_images_with_record_mime_bytes_and_hash(self):
        summary = inline_data.summary(self.records("a", uri(2000), "b " + uri(3000, "image/jpeg")))
        self.assertEqual((summary["count"], summary["bytes"]), (2, 5000))
        self.assertEqual([(i["record"], i["mime"], i["bytes"]) for i in summary["images"]],
                         [(1, "image/png", 2000), (2, "image/jpeg", 3000)])
        self.assertNotIn("more", summary)

    def test_summary_lists_a_bounded_number_of_images_and_says_how_many_more(self):
        summary = inline_data.summary(self.records(*[uri(1000 + n) for n in range(30)]))
        self.assertEqual((summary["count"], len(summary["images"]), summary["more"]), (30, 20, 10))
        self.assertEqual(summary["bytes"], sum(1000 + n for n in range(30)))


if __name__ == "__main__":
    unittest.main()
