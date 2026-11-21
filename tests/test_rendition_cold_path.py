"""Cold-path rewrites of per-extraction checks keep their exact verdicts (Kata 17b0)."""
import copy
import random
import re
import threading
import unittest
from unittest.mock import patch

from tests.support import WorkspaceCase
from tests import test_workspace
from tools.context_index import context_index as index
from tools.context_index import renditions


def reference_lines(source_raw, offset):
    """The regular-expression split this check used before ``bytes.splitlines``."""
    lines = []
    for number, match in enumerate(re.finditer(b".*?(?:\\r\\n|\\r|\\n|\\Z)", source_raw[offset:]), 1):
        raw_line = match.group()
        if not raw_line:
            continue
        text = raw_line.decode("utf-8")
        terminator = ("crlf" if text.endswith("\r\n") else "lf" if text.endswith("\n")
                      else "cr" if text.endswith("\r") else "none")
        lines.append({"line": number, "text": text, "terminator": terminator,
                      "byte_start": offset, "byte_end": offset + len(raw_line)})
        offset += len(raw_line)
    return lines


def reference_mapping(payload, source_raw, engine):
    """``validate_text_mapping`` as it was before the cold-path rewrite."""
    if type(payload["bom_bytes"]) is not int or payload["bom_bytes"] not in {0, 3}:
        engine.fail("Invalid text BOM declaration")
    if (payload["bom_bytes"] == 3) != source_raw.startswith(b"\xef\xbb\xbf"):
        engine.fail("Text BOM declaration does not match original bytes")
    if any(not isinstance(record, dict) or not isinstance(record.get("text"), str)
           or type(record.get("line")) is not int or type(record.get("terminator")) is not str
           or type(record.get("byte_start")) is not int or type(record.get("byte_end")) is not int
           for record in payload["lines"]):
        engine.fail("Invalid text line field types")
    if payload["lines"] != reference_lines(source_raw, payload["bom_bytes"]):
        engine.fail("Invalid text physical line mapping")
    expected_end = bool(payload["lines"] and payload["lines"][-1]["terminator"] != "none")
    if type(payload["ends_with_terminator"]) is not bool or payload["ends_with_terminator"] != expected_end:
        engine.fail("Invalid text byte mapping or final terminator state")


def verdict(function, payload, raw):
    try:
        function(copy.deepcopy(payload), raw, index.rendition_api())
        return None
    except index.IndexError_ as error:
        return str(error)


def outcome(function, raw, offset):
    try:
        return function(raw, offset)
    except UnicodeDecodeError as error:
        return "undecodable", error.reason


class CanonicalLines(unittest.TestCase):
    def test_named_cases_match_the_reference_split(self):
        cases = [b"", b"\n", b"\r", b"\r\n", b"a", b"a\n", b"a\r\nb\rc\nd", b"\r\r\n\n\r", b"\n\n",
                 "caf\u00e9\r\n\u03b2\u2028\x0b\x0c\x1c\x85z".encode(), b"\xef\xbb\xbfabc\r\n", b"a\r\r\nb"]
        for raw in cases:
            for offset in (0, 3 if raw.startswith(b"\xef\xbb\xbf") else 0):
                with self.subTest(raw=raw, offset=offset):
                    self.assertEqual(outcome(renditions._canonical_lines, raw, offset),
                                     outcome(reference_lines, raw, offset))

    def test_random_bytes_match_the_reference_split_including_undecodable_input(self):
        rng = random.Random(1700)
        alphabet = [b"a", b"\r", b"\n", b"\r\n", b"\xce\xb2", b"\xff", b"\xe2\x80\xa8", b"\x0b", b" ", b"\xc3"]
        for _ in range(3000):
            raw = b"".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 14)))
            offset = rng.choice([0, 0, 1, 3]) if len(raw) >= 3 else 0
            self.assertEqual(outcome(renditions._canonical_lines, raw, offset),
                             outcome(reference_lines, raw, offset), (raw, offset))


class TextMapping(unittest.TestCase):
    RAW = b"first\r\n\xce\xb2\nlast\rend"

    def payload(self, raw=None, bom=0):
        raw = self.RAW if raw is None else raw
        lines = reference_lines(raw, bom)
        return {"bom_bytes": bom, "lines": lines,
                "ends_with_terminator": bool(lines and lines[-1]["terminator"] != "none")}

    def test_mutations_get_the_verdicts_they_got_before(self):
        base = self.payload()
        mutations = {
            "unchanged": lambda p: None,
            "text": lambda p: p["lines"][0].update(text="other"),
            "line as bool": lambda p: p["lines"][0].update(line=True),
            "line as float": lambda p: p["lines"][1].update(line=2.0),
            "byte_start as float": lambda p: p["lines"][1].update(byte_start=float(p["lines"][1]["byte_start"])),
            "byte_end as bool": lambda p: p["lines"][0].update(byte_end=True),
            "byte_end moved": lambda p: p["lines"][0].update(byte_end=99),
            "terminator": lambda p: p["lines"][0].update(terminator="lf"),
            "terminator type": lambda p: p["lines"][0].update(terminator=None),
            "text type": lambda p: p["lines"][0].update(text=b"first\r\n"),
            "extra field": lambda p: p["lines"][0].update(extra=1),
            "missing field": lambda p: p["lines"][0].pop("terminator"),
            "record not a dict": lambda p: p["lines"].__setitem__(0, "x"),
            "dropped line": lambda p: p["lines"].pop(),
            "extra line": lambda p: p["lines"].append(dict(p["lines"][-1])),
            "reordered": lambda p: p["lines"].reverse(),
            "bom": lambda p: p.update(bom_bytes=3),
            "bom type": lambda p: p.update(bom_bytes=True),
            "bom 1": lambda p: p.update(bom_bytes=1),
            "final state": lambda p: p.update(ends_with_terminator=True),
            "final state type": lambda p: p.update(ends_with_terminator=0),
        }
        for name, change in mutations.items():
            for label, raw, bom in (("plain", self.RAW, 0), ("bom", b"\xef\xbb\xbf" + self.RAW, 3)):
                with self.subTest(name, source=label):
                    payload = self.payload(raw, bom)
                    change(payload)
                    self.assertEqual(verdict(renditions.validate_text_mapping, payload, raw),
                                     verdict(reference_mapping, payload, raw))
        self.assertIsNone(verdict(renditions.validate_text_mapping, base, self.RAW))

    def test_typed_look_alikes_are_refused_not_accepted_by_equality(self):
        payload = self.payload()
        payload["lines"][0]["line"] = True
        self.assertEqual(verdict(renditions.validate_text_mapping, payload, self.RAW), "Invalid text line field types")


if __name__ == "__main__":
    unittest.main()


class ParallelIdentity(WorkspaceCase):
    """The forked identity check yields the sequential identity, or defers to the sequential check's error."""
    selected_text = test_workspace.WorkspaceTests.selected_text

    def setUp(self):
        super().setUp()
        self.prepared = self.selected_text()
        self.config["renditions"] = [self.config["renditions"][0]]
        self.config, self.checksum = index.load_manifest(self.manifest, self.root)

    def identity(self, minimum):
        with patch.object(renditions, "PARALLEL_MIN", minimum), patch.object(renditions, "PARALLEL_WORKERS", 2):
            reads = set()
            digest = renditions.inputs(self.root, self.config, index.rendition_api(self.root),
                                       review_snapshot=None, reads=reads, keep=False)[1]
        return digest, reads

    def test_parallel_identity_and_reads_equal_the_sequential_ones(self):
        sequential = self.identity(10 ** 9)
        answers = []
        real = renditions._parallel_identity

        def recording(*args):
            answers.append(real(*args))
            return answers[-1]
        with patch.object(renditions, "_parallel_identity", recording):
            forked = self.identity(1)
        self.assertEqual(len(answers), 1)
        self.assertIsNotNone(answers[0], "the workers did the check")
        self.assertEqual(forked, sequential)
        self.assertTrue(sequential[1])

    def test_a_failing_check_reports_the_sequential_error(self):
        extraction = self.root / self.prepared["rendition"]
        extraction.write_bytes(extraction.read_bytes() + b" ")
        with self.assertRaises(index.IndexError_) as sequential:
            self.identity(10 ** 9)
        with self.assertRaises(index.IndexError_) as forked:
            self.identity(1)
        self.assertEqual(str(forked.exception), str(sequential.exception))

    def test_a_broken_pool_falls_back_to_the_sequential_check(self):
        expected = self.identity(10 ** 9)
        with patch("concurrent.futures.ProcessPoolExecutor", side_effect=OSError("no processes")):
            self.assertEqual(self.identity(1), expected)

    def test_a_worker_that_reports_failure_discards_the_parallel_result(self):
        expected = self.identity(10 ** 9)
        with patch.object(renditions, "_identity_chunk", return_value=None):
            self.assertEqual(self.identity(1), expected)

    def test_a_threaded_process_never_forks(self):
        expected = self.identity(10 ** 9)
        release = threading.Event()
        other = threading.Thread(target=release.wait)
        other.start()
        self.addCleanup(other.join)
        self.addCleanup(release.set)
        with patch("concurrent.futures.ProcessPoolExecutor", side_effect=AssertionError("forked")):
            self.assertEqual(self.identity(1), expected)

    def test_keeping_payloads_never_forks(self):
        with patch.object(renditions, "PARALLEL_MIN", 1), \
                patch.object(renditions, "_parallel_identity", side_effect=AssertionError("forked")):
            selected, digest = renditions.inputs(self.root, self.config, index.rendition_api(self.root))
        self.assertEqual(len(selected), 1)
