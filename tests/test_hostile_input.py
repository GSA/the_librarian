"""Hostile or malformed documents get a durable per-file disposition and never stop a cohort run (Kata e275).

Every fixture is built here from synthetic bytes."""
import io
import struct
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
from zipfile import ZIP_STORED, ZipFile

from tools.extraction import extract, mhtml
from tools.intake import adapters, cohort, cohort_ingest, parse_pool, runner, store

from test_intake_cohort import CohortCase, odt as good_odt

OFFICE, TEXT = extract.NS["office"], extract.NS["text"]


def odt_with(body, content_method=None):
    """A minimal ODT whose ``text:p`` paragraph holds ``body`` (markup); optionally a rewritten
    compression-method field for ``content.xml`` (an unsupported method such as 99)."""
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_STORED) as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", '<office:document-content xmlns:office="%s" xmlns:text="%s">'
                   "<office:body><office:text><text:p>%s</text:p></office:text></office:body>"
                   "</office:document-content>" % (OFFICE, TEXT, body))
        z.writestr("meta.xml", '<office:document-meta xmlns:office="%s"><office:meta/></office:document-meta>' % OFFICE)
        z.writestr("META-INF/manifest.xml", "<manifest/>")
    raw = buffer.getvalue()
    if content_method is not None:
        raw = set_method(raw, b"content.xml", content_method)
    return raw


def set_method(raw, name, method):
    """Rewrite the compression method of member ``name`` in its local and central directory headers."""
    data = bytearray(raw)
    for signature, offset, name_at in ((b"PK\x03\x04", 8, 30), (b"PK\x01\x02", 10, 46)):
        start = 0
        while (start := data.find(signature, start)) >= 0:
            length = struct.unpack_from("<H", data, start + 28)[0]
            if bytes(data[start + name_at:start + name_at + length]) == name:
                struct.pack_into("<H", data, start + offset, method)
            start += 4
    return bytes(data)


def nested_mime(depth):
    """``depth`` levels of multipart/mixed, each opening the next inside its own boundary."""
    out = b"MIME-Version: 1.0\r\n"
    for level in range(depth):
        out += b'Content-Type: multipart/mixed; boundary="b%d"\r\n\r\n--b%d\r\n' % (level, level)
    return out


BAD_ODTS = {
    "bad-count.odt": odt_with('<text:s text:c="x"/>'),
    "deep-spans.odt": odt_with("<text:span>" * 5000 + "x" + "</text:span>" * 5000),
    "unsupported-compression.odt": odt_with("hello", content_method=99),
    "not-a-zip.odt": b"PK\x03\x04 this is not a zip package",
}


class ExtractorContainmentTests(unittest.TestCase):
    def refused(self, raw):
        with self.assertRaises(extract.ExtractionError) as caught:
            extract.parse_payload("odt", raw, None)
        return str(caught.exception)

    def test_each_hostile_odt_is_a_refusal_not_a_crash(self):
        for name, raw in BAD_ODTS.items():
            with self.subTest(name):
                self.refused(raw)

    def test_a_non_numeric_or_unbounded_space_count_is_refused(self):
        for count in ("x", "-1", "0", "99999999999999999999", "1e3", " 3"):
            with self.subTest(count):
                self.assertIn("repeated-space", self.refused(odt_with('<text:s text:c="%s"/>' % count)))

    def test_nesting_beyond_the_cap_is_refused_before_any_walk(self):
        self.assertIn("nesting", self.refused(BAD_ODTS["deep-spans.odt"]))
        within = odt_with("<text:span>" * 100 + "ok" + "</text:span>" * 100)
        self.assertEqual(extract.parse_payload("odt", within, None)["format"], "odt")

    def test_the_walkers_do_not_recurse_on_depth(self):
        root = ET.Element("{%s}p" % TEXT)
        cursor = root
        for _ in range(3000):
            cursor = ET.SubElement(cursor, "{%s}span" % TEXT)
        cursor.text = "deep"
        self.assertEqual(sum(1 for _ in extract.walk(root)), 3001)
        self.assertEqual(extract.paragraph(root), "deep")

    def test_any_unanticipated_parser_failure_becomes_a_refusal(self):
        for error in (RecursionError(), NotImplementedError("compress type 99"), RuntimeError("bad"),
                      ZeroDivisionError()):
            with self.subTest(type(error).__name__), patch.object(extract, "_odt", side_effect=error):
                self.assertIn("Malformed ODT", self.refused(good_odt()))


class MimeContainmentTests(unittest.TestCase):
    def test_deeply_nested_multipart_is_refused_with_a_stable_code(self):
        raw = nested_mime(3000)
        self.assertTrue(mhtml.recognised(raw))
        with self.assertRaises(mhtml.MhtmlError) as caught:
            mhtml.payload(raw)
        self.assertEqual(caught.exception.code, "mime-malformed")

    def test_nesting_past_the_depth_cap_is_refused(self):
        with self.assertRaises(mhtml.MhtmlError) as caught:
            mhtml.payload(nested_mime(mhtml.MAX_DEPTH + 5) + b"\r\n--b0--\r\n")
        self.assertEqual(caught.exception.code, "mime-malformed")


class CohortContinuesTests(CohortCase):
    """A hostile file is reported and counted, and the cohort run (and every rerun) goes on."""

    def hostile(self):
        self.writable()
        for name, raw in BAD_ODTS.items():
            (self.inbox / "bad" / name).parent.mkdir(exist_ok=True)
            (self.inbox / "bad" / name).write_bytes(raw)
        (self.inbox / "bad" / "deep-mime.txt").write_bytes(nested_mime(3000))
        (self.inbox / "bad" / "deep-mime-nosuffix").write_bytes(nested_mime(3000))

    def test_classify_gives_every_hostile_file_a_counted_disposition(self):
        self.hostile()
        entries = {e["relative"]: e for e in cohort.classify(self.root, self.inbox)}
        self.assertEqual(entries["a/notes.md"]["disposition"], "admit")
        for name in ("deep-mime.txt", "deep-mime-nosuffix"):
            self.assertEqual((entries["bad/" + name]["disposition"], entries["bad/" + name]["code"]),
                             ("needs-reader", "mime-malformed"))
        self.assertEqual(entries["bad/not-a-zip.odt"]["disposition"], "unreadable")
        self.assertEqual(entries["bad/not-a-zip.odt"]["code"], "type_mismatch")

    def test_ingest_records_each_bad_odt_and_a_rerun_neither_crashes_nor_repeats(self):
        self.hostile()
        first = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
        by_name = {e["relative"]: e for e in first["entries"]}
        for name in ("bad-count", "deep-spans", "unsupported-compression"):
            entry = by_name["bad/%s.odt" % name]
            self.assertEqual((entry["outcome"], entry["stage"], entry["code"]), ("failed", "prepare", "native-input"),
                             name)
        self.assertEqual(first["counts"]["outcome"]["failed"], 3)
        self.assertEqual(first["counts"]["failed_by_code"], {"prepare/native-input": 3})
        self.assertEqual(by_name["a/notes.md"]["outcome"], "extracted")
        second = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
        self.assertEqual(first["counts"], second["counts"])
        attempts = {row["correlation_id"]: len(row["stages"][0]["attempts"]) for row in store.list_requests(self.root)
                    if row["stages"][0]["status"] == "blocked"}
        self.assertEqual(len(attempts), 3)
        self.assertEqual(set(attempts.values()), {1})  # blocked is a recorded disposition: not run again

    def test_an_unexpected_parser_exception_in_odt_fails_that_file_only(self):
        for batch_size in (1, 4):
            with self.subTest(batch_size=batch_size), \
                    patch.object(extract, "_odt", side_effect=ZeroDivisionError("decoder bug")):
                report = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner",
                                       batch_size=batch_size)
            entry = next(e for e in report["entries"] if e["relative"] == "plan.odt")
            self.assertEqual((entry["outcome"], entry["code"]), ("failed", "native-input"))
            self.assertEqual(report["counts"]["outcome"]["extracted"], 5)

    def test_a_stage_that_raises_something_unexpected_is_dead_lettered_not_propagated(self):
        # The adapters' catch-all classifies an unexpected error from a native call.
        with patch.object(extract, "bundle", side_effect=ZeroDivisionError("bug")):
            first = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
        failed = [e for e in first["entries"] if e.get("outcome") == "failed"]
        self.assertTrue(failed)
        self.assertEqual({(e["status"], e["code"]) for e in failed}, {("dead-letter", "native-unexpected")})
        self.assertEqual(first["counts"]["outcome"]["failed"], len(failed))
        again = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
        self.assertEqual(again["counts"]["failed_by_code"], first["counts"]["failed_by_code"])

    def test_a_judge_failure_in_a_worker_or_inline_refuses_only_that_file(self):
        real = cohort_ingest._judge

        def judge(name, raw, settings, nbytes):
            if name == "notes.md":
                raise RecursionError("hostile")
            return real(name, raw, settings, nbytes)
        with patch.object(cohort_ingest, "_judge", judge):
            entries = {e["relative"]: e for e in cohort.classify(self.root, self.inbox)}
            with parse_pool.Workers(1) as pool:
                pooled = {e["relative"]: e for e in cohort.classify(self.root, self.inbox, parse=pool)}
        for found in (entries, pooled):
            self.assertEqual((found["a/notes.md"]["disposition"], found["a/notes.md"]["code"]),
                             ("unreadable", "parse-failed"))
            self.assertEqual(found["m/plain.txt"]["disposition"], "admit")

    def test_a_job_error_delivered_by_the_pool_refuses_the_file(self):
        def fail(*args):
            raise RuntimeError("worker job failed")
        with patch.object(cohort_ingest, "judge", fail):
            with parse_pool.Workers(1) as pool:
                entries = {e["relative"]: e for e in cohort.classify(self.root, self.inbox, parse=pool)}
        self.assertEqual(entries["a/notes.md"]["code"], "parse-failed")
        self.assertEqual(entries["m/plain.txt"]["disposition"], "unreadable")


class AdapterClassificationTests(unittest.TestCase):
    def test_unexpected_errors_are_permanent_and_known_causes_keep_their_class(self):
        failure = adapters.unexpected_failure(ZeroDivisionError("x"))
        self.assertEqual((failure.category, failure.code), ("permanent", "native-unexpected"))
        self.assertEqual(adapters.unexpected_failure(TimeoutError()).code, "native-unexpected")
        import subprocess
        timeout = adapters.unexpected_failure(subprocess.TimeoutExpired("cmd", 1))
        self.assertEqual((timeout.category, timeout.code), ("retryable", "native-timeout"))
        self.assertIs(runner.classify(failure), failure)


if __name__ == "__main__":
    unittest.main()
