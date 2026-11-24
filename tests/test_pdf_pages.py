"""PDFs read page by page: routing, assembly, per-page retry and budgets, and the pinned runtime (Kata ev2r).

Unit children are replaced by small scripts for the hardening tests. Tests that convert through the
pinned Docling runtime skip unless it is provisioned (``tools/extraction/.local/`` or
LIBRARIAN_DOCLING_PYTHON/ARTIFACTS).
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.pdf_fixtures import encrypted_pdf, text_pdf
from tools.context_index import confidence
from tools.extraction import pdf_image, pdf_pages

RUNTIME = pdf_image.runtime(pdf_image.TOOLKIT)
PROVISIONED = pdf_image.status(pdf_image.TOOLKIT)["provisioned"]
SENTENCE = "The quick brown fox jumps over the lazy dog number %d."
TEXT = "\n".join(SENTENCE % i for i in range(6))


def text_page(number):
    return {"page": number, "text": TEXT, "image_objects": 0, "width": 612.0, "height": 792.0}


def scan_page(number):
    return {"page": number, "text": "", "image_objects": 1, "width": 612.0, "height": 792.0}


def inventory(pages, count=None):
    return {"reader": "pypdfium2", "version": "test", "page_count": count or len(pages), "pages": pages}


def read(number, attempts=1, texts=None):
    return {"texts": texts if texts is not None else ["Scanned words of page %d" % number],
            "textless": texts == [], "confidence": {"mean_grade": "good"}, "notes": [],
            "render": {"scale": 3.0}, "method": {"kind": "docling-ocr"}, "attempts": attempts}


class RoutingTests(unittest.TestCase):
    def test_each_page_keeps_its_text_layer_or_goes_to_ocr(self):
        pages = [text_page(1), scan_page(2), {"page": 3, "text": "Short caption", "image_objects": 0},
                 {"page": 4, "text": "Caption", "image_objects": 2}, {"page": 5, "error": "output_limit"}]
        self.assertEqual(pdf_pages.plan(pages), {1: "text-layer", 2: "ocr", 3: "text-layer", 4: "ocr", 5: "failed"})

    def test_a_document_without_real_text_reads_its_imageless_pages_by_ocr(self):
        # Its text may be drawn as paths; only OCR can tell.
        self.assertEqual(pdf_pages.plan([{"page": 1, "text": "x", "image_objects": 0}]), {1: "ocr"})


class AssemblyTests(unittest.TestCase):
    def test_pages_are_reassembled_in_order_with_placeholders_and_outcomes(self):
        payload = pdf_pages.assemble(inventory([text_page(1), scan_page(2), scan_page(3), scan_page(4)]), {
            2: read(2), 3: {"code": "timeout", "attempts": 2}, 4: read(4, texts=[])})
        self.assertEqual(payload["format"], "pdf-pages")
        self.assertEqual([line["line"] for line in payload["lines"]], list(range(1, len(payload["lines"]) + 1)))
        self.assertEqual([(line["page"], line["text"]) for line in payload["lines"][-2:]],
                         [(2, "Scanned words of page 2"), (3, "[Page 3 not extracted: extract-timeout after 2 attempts]")])
        self.assertEqual([(entry["page"], entry["outcome"]) for entry in payload["pages"]],
                         [(1, "text-layer"), (2, "ocr"), (3, "failed"), (4, "ocr")])
        self.assertEqual((payload["pages"][2]["code"], payload["pages"][3]["textless"]), ("extract-timeout", True))
        self.assertIn("1 page(s) were not extracted (extract-timeout)", " ".join(payload["warnings"]))

    def test_pages_beyond_the_page_budget_are_recorded_not_silently_dropped(self):
        payload = pdf_pages.assemble(inventory([text_page(1), text_page(2)], count=9), {})
        self.assertEqual(payload["unread_pages"], {"first": 3, "last": 9, "code": "extract-page-budget"})
        self.assertEqual(payload["lines"][-1], {"line": len(payload["lines"]), "page": 3,
                                                "text": "[Pages 3-9 not extracted: extract-page-budget]"})
        self.assertIn("7 page(s) were not extracted", " ".join(payload["warnings"]))

    def test_pages_beyond_the_record_cap_fail_behind_one_placeholder_within_the_cap(self):
        payload = pdf_pages.assemble(inventory([text_page(1), scan_page(2), text_page(3)], count=5),
                                     {2: {"code": "timeout", "attempts": 2}}, {"lines": 9})
        self.assertEqual([(entry["outcome"], entry.get("code")) for entry in payload["pages"]],
                         [("text-layer", None), ("failed", "extract-timeout"), ("failed", "extract-record-limit")])
        self.assertEqual([line["text"] for line in payload["lines"][-3:]],
                         ["[Page 2 not extracted: extract-timeout after 2 attempts]",
                          "[Page 3 not extracted: extract-record-limit]",
                          "[Pages 4-5 not extracted: extract-page-budget]"])
        self.assertEqual(len(payload["lines"]), 9)

    def test_pages_the_inventory_could_not_return_are_recorded_with_its_reason(self):
        payload = pdf_pages.assemble(dict(inventory([text_page(1)], count=3), unread="output_limit"), {})
        self.assertEqual(payload["unread_pages"], {"first": 2, "last": 3, "code": "extract-output-limit"})

    def test_a_document_is_refused_only_when_every_page_fails_or_nothing_has_text(self):
        cases = [({2: {"code": "timeout", "attempts": 2}, 3: {"code": "timeout", "attempts": 2}}, "extract-timeout"),
                 ({2: {"code": "timeout", "attempts": 2}, 3: {"code": "worker_crashed", "attempts": 2}},
                  "extract-all-pages-failed"),
                 ({2: read(2, texts=[]), 3: read(3, texts=[])}, "extract-empty-conversion")]
        for outcomes, code in cases:
            with self.subTest(code=code), self.assertRaises(pdf_pages.ConversionFailed) as caught:
                pdf_pages.assemble(inventory([scan_page(2), scan_page(3)]), outcomes)
            self.assertEqual(caught.exception.code, code)

    def test_confidence_is_the_union_of_page_kinds_and_flags_missing_pages(self):
        def codes(outcomes, pages, count=None):
            payload = pdf_pages.assemble(inventory(pages, count), outcomes)
            return {item["code"]: item["details"]
                    for item in confidence.for_rendition({"payload": payload}, None, None)["reasons"]}
        self.assertEqual(codes({}, [text_page(1)]), {"converted-format": ["pdf-text"]})
        self.assertEqual(codes({2: read(2)}, [text_page(1), scan_page(2)]),
                         {"converted-format": ["pdf-text"], "machine-ocr": ["ocr"]})
        self.assertEqual(codes({2: {"code": "timeout", "attempts": 2}}, [text_page(1), scan_page(2)], count=4),
                         {"converted-format": ["pdf-text"],
                          "incomplete-rendition": ["page 2: extract-timeout", "pages 3-4: extract-page-budget"]})


# A stand-in unit child: argv is (home, behaviour); it acts on job.json as the behaviour says.
CHILD = r"""
import json, os, signal, subprocess, sys, time
home, behaviour = sys.argv[1], json.loads(sys.argv[2])
job = json.load(open(home + "/job.json"))
key = "inventory" if job["kind"] == "inventory" else str(job["page"])
action = behaviour["actions"].get(key, "read")
if action.startswith("flaky:"):
    marker = action.split(":", 1)[1] + "/" + key
    action = "read" if os.path.exists(marker) else "crash"
    open(marker, "w").close()
if action == "hang":
    time.sleep(60)
if action == "orphan":
    child = subprocess.Popen(["sleep", "60"])
    open(behaviour["pids"] + "/" + key, "w").write(str(child.pid))
    time.sleep(60)
if action == "escape":
    subprocess.Popen(["sleep", behaviour["token"]], start_new_session=True)
    time.sleep(60)
if action == "crash":
    os.kill(os.getpid(), signal.SIGKILL)
if action.startswith("sleep:"):
    time.sleep(float(action.split(":", 1)[1]))
    action = "read"
if action.startswith("refuse:"):
    result = {"ok": False, "code": action.split(":", 1)[1], "message": "refused"}
elif job["kind"] == "inventory":
    result = {"ok": True, "payload": behaviour["inventory"]}
else:
    result = {"ok": True, "payload": {"texts": ["Scanned words of page " + key], "textless": False,
              "confidence": {}, "notes": [], "render": {"scale": 3.0}, "method": {"kind": "docling-ocr"}}}
result["stats"] = {"seconds": 0.0, "peak_rss_kib": 1}
open(home + "/result.json", "w").write(json.dumps(result))
"""


class HardeningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.work = self.base / "work"
        self.work.mkdir()
        self.state = self.base / "state"
        self.state.mkdir()

    def convert(self, actions, pages, units=None, namespace=(), token=None, **limits):
        behaviour = {"inventory": inventory(pages), "pids": str(self.state), "actions": actions, "token": token}
        argv = lambda home: [*namespace, sys.executable, "-c", CHILD, str(home), json.dumps(behaviour)]  # noqa: E731
        log = []
        with mock.patch.object(pdf_pages, "_child_argv", argv):
            payload, stats = pdf_pages.convert(b"%PDF-1.4\n", "a.pdf", RUNTIME, self.work,
                                               {"page_wall_seconds": 2, **limits}, units, log)
        return payload, stats, log

    def test_each_page_is_retried_once_then_fails_alone_with_its_code(self):
        flaky = "flaky:" + str(self.state)
        payload, stats, log = self.convert({"2": "hang", "3": "crash", "4": flaky},
                                           [text_page(1), scan_page(2), scan_page(3), scan_page(4), scan_page(5)])
        outcomes = [(e["page"], e["outcome"], e.get("code"), e.get("attempts")) for e in payload["pages"]]
        self.assertEqual(outcomes, [(1, "text-layer", None, None), (2, "failed", "extract-timeout", 2),
                                    (3, "failed", "extract-worker-crashed", 2), (4, "ocr", None, 2),
                                    (5, "ocr", None, 1)])
        self.assertEqual(stats["pages"], {"text-layer": 1, "failed": 2, "ocr": 2})
        self.assertEqual(sum(1 for entry in log if entry["kind"] == "ocr"), 7)
        texts = [line["text"] for line in payload["lines"]]
        self.assertIn("[Page 2 not extracted: extract-timeout after 2 attempts]", texts)
        self.assertIn("Scanned words of page 4", texts)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_a_timed_out_unit_leaves_no_process_behind(self):
        self.convert({"2": "orphan"}, [text_page(1), scan_page(2)])
        pid = int((self.state / "2").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("the timed-out unit's process group survived")

    @unittest.skipUnless(subprocess.run([*pdf_pages.NAMESPACE, "true"], capture_output=True).returncode == 0,
                         "unprivileged PID namespaces unavailable")
    def test_a_killed_unit_takes_processes_in_other_sessions_with_it(self):
        token = "59.%06d" % (os.getpid() % 1000000)  # a unique `sleep` argument to find it by

        def running():
            for entry in Path("/proc").iterdir():
                try:
                    if (entry / "cmdline").read_bytes().split(b"\0")[:2] == [b"sleep", token.encode()]:
                        return True
                except OSError:
                    continue
            return False
        payload, _, _ = self.convert({"2": "escape"}, [text_page(1), scan_page(2)], namespace=pdf_pages.NAMESPACE,
                                     token=token)
        self.assertEqual(payload["pages"][1]["code"], "extract-timeout")
        deadline = time.monotonic() + 5
        while running() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(running(), "a process started by the killed unit survived it")

    def test_deterministic_page_refusals_are_not_retried(self):
        payload, _, log = self.convert({"2": "refuse:pixel_limit"}, [text_page(1), scan_page(2)])
        self.assertEqual((payload["pages"][1]["code"], payload["pages"][1]["attempts"]), ("extract-pixel-limit", 1))
        self.assertEqual(len(log), 2)

    def test_the_document_time_budget_marks_later_pages_without_running_them(self):
        pages = [text_page(1)] + [scan_page(n) for n in range(2, 6)]
        # One page fits the budget with 0.4s to spare for child startup on a slow runner; two never do.
        payload, _, log = self.convert({str(n): "sleep:0.8" for n in range(2, 6)}, pages, document_seconds=1.2)
        self.assertEqual([(e["outcome"], e.get("code"), e.get("attempts")) for e in payload["pages"][3:]],
                         [("failed", "extract-time-budget", 0)] * 2)
        self.assertIn("[Page 5 not extracted: extract-time-budget]", [line["text"] for line in payload["lines"]])
        self.assertEqual(sum(1 for entry in log if entry["kind"] == "ocr"), 2)

    def test_an_unreadable_document_or_one_whose_pages_all_fail_is_refused(self):
        for behaviour, code, runs in (({"inventory": "refuse:encrypted"}, "extract-encrypted", 1),
                                      ({"inventory": "crash"}, "extract-worker-crashed", 2),
                                      ({"1": "crash", "2": "crash"}, "extract-worker-crashed", 5)):
            with self.subTest(code=code):
                log = []
                argv = lambda home, b=behaviour: [  # noqa: E731
                    sys.executable, "-c", CHILD, str(home),
                    json.dumps({"inventory": inventory([scan_page(1), scan_page(2)]), "actions": b})]
                with mock.patch.object(pdf_pages, "_child_argv", argv), \
                        self.assertRaises(pdf_pages.ConversionFailed) as caught:
                    pdf_pages.convert(b"%PDF-1.4\n", "a.pdf", RUNTIME, self.work, None, None, log)
                self.assertEqual((caught.exception.code, len(log)), (code, runs))

    def test_units_of_every_document_share_one_concurrency_bound(self):
        lock, active = threading.Lock(), {"now": 0, "peak": 0}

        def attempt(directory, job, wall_seconds):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            time.sleep(0.05)
            with lock:
                active["now"] -= 1
            if job["kind"] == "inventory":
                return inventory([scan_page(n) for n in range(1, 5)]), {}
            return {k: v for k, v in read(job["page"]).items() if k != "attempts"}, {}
        with mock.patch.object(pdf_pages, "_attempt", attempt), ThreadPoolExecutor(2) as units, \
                ThreadPoolExecutor(3) as documents:
            results = list(documents.map(lambda name: pdf_pages.convert(b"%PDF-1.4\n", name, RUNTIME, self.work,
                                                                        units=units), ["a", "b", "c"]))
        self.assertEqual([stats["pages"] for _, stats in results], [{"ocr": 4}] * 3)
        self.assertEqual(active["peak"], 2)


@unittest.skipUnless(PROVISIONED, "pinned Docling runtime not provisioned")
class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        base = Path(cls.temp.name)
        cls.work = base / "work"
        cls.work.mkdir(mode=0o700)
        text = base / "text.pdf"
        text.write_bytes(text_pdf([[SENTENCE % i for i in range(6)]]))
        cls.mixed, cls.big = base / "mixed.pdf", base / "big.pdf"
        # A text page, the same page scanned at 216 DPI, and that scan drawn across an oversized sheet,
        # assembled with the pinned runtime's own libraries (test fixtures only).
        script = (
            "import sys, pypdfium2 as p; t = p.PdfDocument(sys.argv[1]); "
            "i = t[0].render(scale=3).to_pil().convert('RGB'); i.save(sys.argv[3], resolution=216); "
            "out = p.PdfDocument.new(); out.import_pages(t); out.import_pages(p.PdfDocument(sys.argv[3])); "
            "sheet = out.new_page(7000, 9000); image = p.PdfImage.new(out); "
            "image.set_bitmap(p.PdfBitmap.from_pil(i)); image.set_matrix(p.PdfMatrix().scale(7000, 9000)); "
            "sheet.insert_obj(image); sheet.gen_content(); out.save(sys.argv[2])")
        subprocess.run([str(RUNTIME["python"]), "-I", "-c", script, str(text), str(cls.mixed),
                        str(base / "scan.pdf")], check=True, env={"PATH": os.defpath})
        cls.big.write_bytes(text_pdf([[SENTENCE % (n * 10 + i) for i in range(5)] for n in range(120)]))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_each_page_uses_its_text_layer_or_its_own_ocr(self):
        payload, stats = pdf_pages.convert(self.mixed.read_bytes(), "mixed.pdf", RUNTIME, self.work)
        self.assertEqual([entry["outcome"] for entry in payload["pages"]], ["text-layer", "ocr", "ocr"], payload["pages"])
        for number in (2, 3):
            self.assertIn("quick brown fox",
                          " ".join(line["text"] for line in payload["lines"] if line["page"] == number))
        # The oversized sheet is rendered smaller to fit the pixel cap rather than refused.
        self.assertEqual(payload["pages"][1]["render_scale"], 3)
        self.assertLess(payload["pages"][2]["render_scale"], 1)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_a_long_text_pdf_is_read_without_a_page_limit_refusal(self):
        payload, stats = pdf_pages.convert(self.big.read_bytes(), "big.pdf", RUNTIME, self.work)
        self.assertEqual((payload["page_count"], stats["pages"]), (120, {"text-layer": 120}))
        self.assertEqual(payload["lines"][-1]["page"], 120)

    def test_unreadable_pdfs_are_refused_with_codes(self):
        for raw, code in ((encrypted_pdf(), "extract-encrypted"),
                          (b"%PDF-1.4\nnot a pdf\n", "extract-malformed-source")):
            with self.subTest(code=code), self.assertRaises(pdf_pages.ConversionFailed) as caught:
                pdf_pages.convert(raw, "bad.pdf", RUNTIME, self.work)
            self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
