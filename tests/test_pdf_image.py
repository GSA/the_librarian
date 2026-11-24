"""PDF and image reading: routing, limits, hardening and cohort integration (Kata ye2s).

Pure routing and limit logic always runs. Tests that convert through the pinned Docling runtime skip
unless it is provisioned (``tools/extraction/.local/`` or LIBRARIAN_DOCLING_PYTHON/ARTIFACTS).
"""
import contextlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.pdf_fixtures import encrypted_pdf, image_pdf, jpeg_header, png, png_header, text_pdf
from tools.catalog import catalog_store as catalog
from tools.context_index import confidence, context_index as index, renditions
from tools.extraction import docling_worker, pdf_image, pdf_pages
from tools.intake import adapters, cohort, cohort_runtime, standing, store

RUNTIME = pdf_image.runtime(pdf_image.TOOLKIT)
PROVISIONED = pdf_image.status(pdf_image.TOOLKIT)["provisioned"]
SENTENCE = "The quick brown fox jumps over the lazy dog number %d."


def page(number, text, images=0):
    return {"page": number, "text": text, "image_objects": images}


def report(pages):
    return {"reader": "pypdfium2", "version": "test", "pages": pages}


class RoutingTests(unittest.TestCase):
    def test_a_real_text_layer_is_used_and_documented(self):
        use, reason = pdf_image.text_layer_decision([page(1, " ".join([SENTENCE % i for i in range(6)]), 1)])
        self.assertEqual((use, reason), (True, None))

    def test_short_text_pages_without_images_still_use_the_text_layer(self):
        pages = [page(i, SENTENCE % i) for i in range(1, 6)]
        self.assertEqual(pdf_image.text_layer_decision(pages), (True, None))

    def test_scanned_or_low_text_files_fall_back_to_ocr(self):
        cases = {
            "fewer than": [page(1, "", 1)],
            "fewer than ": [page(1, "Title only", 0)],
            "image-bearing page(s) 2": [page(1, "x " * 300 + SENTENCE * 5, 0), page(2, "Scan 1", 1)],
            "image-bearing page(s) 1": [page(1, "#$%^&*" * 100 + "abc" * 70, 2)],
        }
        for expected, pages in cases.items():
            with self.subTest(expected=expected):
                use, reason = pdf_image.text_layer_decision(pages)
                self.assertFalse(use)
                self.assertIn(expected.strip(), reason)

    def test_text_payload_numbers_lines_by_page_and_marks_its_kind(self):
        payload = pdf_image.text_payload(report([page(1, "first\n\n  second  \n"), page(2, "third")]))
        self.assertEqual(payload["lines"], [{"line": 1, "page": 1, "text": "first"},
                                            {"line": 2, "page": 1, "text": "  second"},
                                            {"line": 3, "page": 2, "text": "third"}])
        self.assertEqual((payload["format"], payload["source_format"], payload["method"]["kind"]),
                         ("pdf-text", "pdf", "pdf-text-layer"))

    def test_pages_count_against_the_standing_page_budget(self):
        payload = pdf_image.text_payload(report([page(1, "first"), page(2, "second")]))
        _, pages, _, _ = standing._expected_inspection({"payload": payload}, [])
        self.assertEqual(pages, 2)  # a max_pages 1 policy refuses this two-page PDF

    def test_ocr_is_low_confidence_machine_ocr_and_a_text_layer_converted_format(self):
        text = pdf_image.text_payload(report([page(1, "first")]))
        ocr = {**text, "format": "ocr"}
        for payload, code in ((ocr, "machine-ocr"), (text, "converted-format")):
            label = confidence.for_rendition({"payload": payload}, None, None)
            self.assertEqual(label["reasons"], [{"code": code, "details": [payload["format"]]}])

    def test_line_records_are_capped(self):
        with mock.patch.dict(pdf_image.LIMITS, lines=2), self.assertRaises(pdf_image.ConversionFailed) as caught:
            pdf_image.text_payload(report([page(1, "a\nb\nc")]))
        self.assertEqual(caught.exception.code, "extract-record-limit")

    def test_docling_document_is_flattened_in_reading_order_with_tables_and_unreached_items(self):
        document = {
            "body": {"children": [{"$ref": "#/texts/1"}, {"$ref": "#/groups/0"}, {"$ref": "#/tables/0"}]},
            "furniture": {"children": []},
            "groups": [{"children": [{"$ref": "#/texts/0"}]}],
            "texts": [{"text": "second", "prov": [{"page_no": 1}]},
                      {"text": "first", "prov": [{"page_no": 1}]},
                      {"text": "orphan", "prov": [{"page_no": 2}]}],
            "tables": [{"prov": [{"page_no": 2}], "data": {"num_rows": 1, "num_cols": 2, "table_cells": [
                {"text": "a", "start_row_offset_idx": 0, "end_row_offset_idx": 1,
                 "start_col_offset_idx": 0, "end_col_offset_idx": 1},
                {"text": "b", "start_row_offset_idx": 0, "end_row_offset_idx": 1,
                 "start_col_offset_idx": 1, "end_col_offset_idx": 2}]}}],
        }
        self.assertEqual(pdf_image._flatten(document),
                         [(1, "first"), (1, "second"), (2, "a | b"), (2, "orphan")])


    def test_text_spanning_pages_is_split_at_its_charspans_or_counted(self):
        def document(prov):
            return {"body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]},
                    "texts": [{"text": "end of one start of two", "prov": prov},
                              {"text": "after", "prov": [{"page_no": 2}]}]}
        split = document([{"page_no": 1, "charspan": [0, 10]}, {"page_no": 2, "charspan": [11, 23]}])
        self.assertEqual(pdf_image._flatten(split),
                         [(1, "end of one "), (2, "start of two"), (2, "after")])
        spanning = []
        unplaced = document([{"page_no": 1}, {"page_no": 2}])
        self.assertEqual(pdf_image._flatten(unplaced, spanning)[0], (1, "end of one start of two"))
        self.assertEqual(spanning, ["#/texts/0"])


class ImageLimitTests(unittest.TestCase):
    def test_headers_give_dimensions_before_any_decoder(self):
        self.assertEqual(pdf_image.image_size(png_header(640, 480)), (640, 480))
        self.assertEqual(pdf_image.image_size(jpeg_header(800, 600)), (800, 600))
        self.assertIsNone(pdf_image.image_size(b"%PDF-1.4"))

    def test_decompression_bombs_are_refused_from_the_header(self):
        for raw in (png_header(20_000, 20_000), jpeg_header(65_000, 65_000)):
            self.assertEqual(pdf_image.pixel_refusal(raw, "image"), "pixel_limit")
        self.assertIsNone(pdf_image.pixel_refusal(png_header(2560, 1440), "image"))
        with self.assertRaises(pdf_image.ConversionFailed) as caught:
            pdf_image.convert(png_header(20_000, 20_000), "bomb.png", "image", RUNTIME, Path("."))
        self.assertEqual(caught.exception.code, "extract-pixel-limit")

    def test_large_screenshots_get_dpi_metadata_so_the_ocr_raster_fits(self):
        width, height = 2560, 1440
        self.assertFalse(pdf_image._fits(width, height, 72))
        raw = png(width, height)
        adjusted, dpi = pdf_image.ocr_source(raw, "image")
        self.assertGreater(dpi, 72)
        self.assertEqual(pdf_image.image_size(adjusted), (width, height))
        index = adjusted.index(b"pHYs")
        ppm = struct.unpack(">I", adjusted[index + 4:index + 8])[0]
        self.assertTrue(pdf_image._fits(width, height, ppm * 0.0254))
        self.assertEqual(adjusted.count(b"pHYs"), 1)
        self.assertIn(raw[raw.index(b"IDAT") - 4:], adjusted)  # pixel data untouched
        again, _ = pdf_image.ocr_source(adjusted, "image")
        self.assertEqual(again, adjusted)

    def test_jpeg_density_is_set_in_jfif_or_inserted(self):
        with_jfif = jpeg_header(4000, 3000)
        without = with_jfif[:2] + with_jfif[20:]
        for raw in (with_jfif, without):
            adjusted, dpi = pdf_image.ocr_source(raw, "image")
            self.assertEqual(pdf_image.image_size(adjusted), (4000, 3000))
            start = adjusted.index(b"JFIF\x00")
            unit, x, y = struct.unpack(">BHH", adjusted[start + 7:start + 12])
            self.assertEqual((unit, x, y), (1, dpi, dpi))
            self.assertTrue(pdf_image._fits(4000, 3000, dpi))

    def test_pdfs_pass_through_unchanged(self):
        raw = text_pdf(["x"])
        self.assertEqual(pdf_image.ocr_source(raw, "pdf"), (raw, None))


class HardeningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)

    def child(self, code, **limits):
        argv = [sys.executable, "-c", code]
        with mock.patch.object(pdf_image, "_child_argv", lambda home: argv):
            started = time.monotonic()
            with self.assertRaises(pdf_image.ConversionFailed) as caught:
                pdf_image.convert(b"%PDF-1.4\n", "a.pdf", "pdf", RUNTIME, self.work, limits)
        return caught.exception, time.monotonic() - started

    def test_a_hanging_child_is_killed_retried_once_and_refused(self):
        error, seconds = self.child("import time; time.sleep(60)", wall_seconds=1)
        self.assertEqual(error.code, "extract-timeout")
        self.assertLess(seconds, 20)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_a_crashing_child_is_retried_once_and_refused(self):
        calls = []
        real = pdf_image._attempt

        def counted(*args):
            calls.append(1)
            return real(*args)
        with mock.patch.object(pdf_image, "_attempt", counted):
            error, _ = self.child("import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
        self.assertEqual((error.code, len(calls)), ("extract-worker-crashed", 2))

    def test_an_oversized_result_is_refused(self):
        argv = [sys.executable, "-c", "import sys; open(sys.argv[1] + '/result.json', 'w').write('x' * 100)"]
        with mock.patch.object(pdf_image, "MAX_RESULT", 10), \
                mock.patch.object(pdf_image, "_child_argv", lambda home: [*argv, str(home)]), \
                self.assertRaises(pdf_image.ConversionFailed) as caught:
            pdf_image.convert(b"%PDF-1.4\n", "a.pdf", "pdf", RUNTIME, self.work)
        self.assertEqual(caught.exception.code, "extract-output-limit")

    def test_conversion_children_can_write_the_largest_pinned_model_asset(self):
        # Docling's adapter copies every pinned asset inside these children (some exceed 64 MiB);
        # a sparse write at the last byte meets the file-size limit without writing the bytes.
        write = ("def write(home, *_):\n"
                 "    with open(Path(home) / 'asset', 'wb') as stream:\n"
                 "        stream.seek(pdf_image.LARGEST_ASSET - 1)\n"
                 "        stream.write(b'\\0')\n"
                 "    return {'method': {}}\n")
        entries = {"child": "pdf_image.extract_file = write\nsys.exit(pdf_image.child(sys.argv[1]))",
                   "unit": "pdf_pages.read_unit = write\nsys.exit(pdf_pages.unit(sys.argv[1]))"}
        self.assertGreater(pdf_image.LARGEST_ASSET, pdf_image.MAX_RESULT * 2)
        for entry, call in entries.items():
            with self.subTest(entry), tempfile.TemporaryDirectory(dir=self.work) as home:
                (Path(home) / "source").write_bytes(b"x")
                (Path(home) / "job.json").write_text(json.dumps({
                    "name": "a.png", "format": "image", "python": "", "artifacts": "",
                    "address_space_bytes": 4 * 1024**3, "cpu_seconds": 60, "wall_seconds": 60}))
                code = ("import sys\nfrom pathlib import Path\n"
                        "from tools.extraction import pdf_image, pdf_pages\n" + write + call)
                subprocess.run([sys.executable, "-c", code, home], cwd=pdf_image.TOOLKIT, check=False)
                result = json.loads((Path(home) / "result.json").read_text())
                self.assertTrue(result["ok"], result)

    def test_deterministic_refusals_are_not_retried(self):
        calls = []

        def refuse(*args):
            calls.append(1)
            raise pdf_image.ConversionFailed("encrypted")
        with mock.patch.object(pdf_image, "_attempt", refuse), self.assertRaises(pdf_image.ConversionFailed):
            pdf_image.convert(b"%PDF-1.4\n", "a.pdf", "pdf", RUNTIME, self.work)
        self.assertEqual(len(calls), 1)

    def test_a_refusal_is_a_blocked_stage_with_its_own_code(self):
        failure = adapters.native_failure(pdf_image.ConversionFailed("encrypted"))
        self.assertEqual((failure.category, failure.code), ("blocked", "extract-encrypted"))


class StatusTests(unittest.TestCase):
    def test_an_unprovisioned_instance_reports_a_code_and_the_action(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, clear=False) as env:
            env.pop(pdf_image.PYTHON_ENV, None)
            env.pop(pdf_image.ARTIFACTS_ENV, None)
            result = pdf_image.status(root)
        self.assertFalse(result["provisioned"])
        self.assertEqual(result["code"], "docling-not-provisioned")
        self.assertIn("python", result["missing"])
        self.assertIn("tools/extraction/README.md", result["action"])

    def test_runtime_paths_are_absolute_for_the_child_without_resolving_the_venv_link(self):
        with mock.patch.dict(os.environ, {pdf_image.PYTHON_ENV: "venv/bin/python",
                                          pdf_image.ARTIFACTS_ENV: "models"}):
            paths = pdf_image.runtime("instance")
        self.assertEqual(paths["python"], Path.cwd() / "venv/bin/python")
        self.assertEqual(paths["artifacts"], Path.cwd() / "models")
        with mock.patch.dict(os.environ, clear=False) as env:
            env.pop(pdf_image.PYTHON_ENV, None)
            self.assertEqual(pdf_image.runtime("instance")["python"],
                             Path.cwd() / "instance" / pdf_image.RUNTIME / "venv-docling/bin/python")

    def test_the_preflight_check_is_informational(self):
        from types import SimpleNamespace
        from the_librarian_mcp import preflight
        with tempfile.TemporaryDirectory() as root:
            config = SimpleNamespace(root=Path(root), toolkit_root=pdf_image.TOOLKIT, timeout_seconds=30)
            check = preflight.pdf_image_runtime(config)
        self.assertTrue(check["ok"])
        self.assertIn("provisioned", check)
        self.assertTrue(check["action"])


def provision(root):
    """A fake provisioned runtime below ``root`` (sparse files at the pinned sizes)."""
    local = Path(root) / pdf_image.RUNTIME
    python = local / "venv-docling/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 1\n")
    python.chmod(0o755)
    for relative, (size, _) in zip(docling_worker.REQUIRED_ASSETS, docling_worker.ASSET_PINS):
        path = local / "models" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as stream:
            stream.truncate(size)


class CohortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(pdf_image.PYTHON_ENV, None)
        os.environ.pop(pdf_image.ARTIFACTS_ENV, None)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)
        self.inbox = Path(self.temp.name) / "inbox"
        self.inbox.mkdir()
        files = {
            "a.pdf": text_pdf([" ".join([SENTENCE % i for i in range(5)])]),
            "locked.pdf": encrypted_pdf(),
            "shot.png": png(16, 16),
            "photo.jpg": jpeg_header(8, 8),
            "fake.png": b"not a png",
            "bomb.png": png_header(30_000, 30_000),
            "note.md": b"# Note\n",
        }
        for name, raw in files.items():
            (self.inbox / name).write_bytes(raw)

    def dispositions(self):
        return {e["relative"]: (e["disposition"], e["format"], e.get("code"))
                for e in cohort.classify(self.root, self.inbox)}

    def test_without_the_runtime_pdfs_and_images_stay_needs_runtime_with_a_code(self):
        found = self.dispositions()
        for name in ("a.pdf", "locked.pdf", "shot.png", "photo.jpg", "fake.png", "bomb.png"):
            self.assertEqual(found[name][0::2], ("needs-runtime", "docling-not-provisioned"), name)
        self.assertEqual(found["note.md"][0], "admit")

    def test_with_the_runtime_they_are_read_typed_and_bomb_checked(self):
        provision(self.root)
        found = self.dispositions()
        self.assertEqual(found["a.pdf"], ("admit", "pdf", None))
        self.assertEqual(found["shot.png"], ("admit", "image", None))
        self.assertEqual(found["photo.jpg"], ("admit", "image", None))
        self.assertEqual(found["fake.png"], ("unreadable", "image", "type_mismatch"))
        self.assertEqual(found["bomb.png"], ("over-limit", "image", "pixel-limit"))

    def fake_convert(self, calls, refusals, active=None):
        """Fakes of ``(pdf_image.convert, pdf_pages.convert)``: images and PDFs."""
        lock = threading.Lock()

        def convert(raw, name, format_name, paths, work, limits=None):
            with lock:
                calls.append(name)
                if active is not None:
                    active["now"] += 1
                    active["peak"] = max(active["peak"], active["now"])
            try:
                time.sleep(0.05)
                if name in refusals:
                    raise pdf_image.ConversionFailed(refusals[name])
                text = (SENTENCE % len(raw)) if format_name == "pdf" else "OCR words"
                if format_name == "pdf":
                    text = "\n".join([text] + [SENTENCE % i for i in range(5)])
                    return pdf_pages.assemble({**report([page(1, text)]), "page_count": 1}, {}), {}
                document = {"body": {"children": [{"$ref": "#/texts/0"}]}, "texts": [
                    {"text": text, "prov": [{"page_no": 1}]}]}
                return pdf_image.ocr_payload({
                    "conversion": {"document": document, "pages": [{"page_no": 1, "textless": False}],
                                   "warnings": [], "confidence": {}},
                    "runtime": {}, "profile": {"version": 1}, "assets": {}}, format_name), {}
            finally:
                if active is not None:
                    with lock:
                        active["now"] -= 1

        def convert_pdf(raw, name, paths, work, limits=None, units=None, log=None):
            return convert(raw, name, "pdf", paths, work, limits)
        return convert, convert_pdf

    def faked(self, *args, **kwargs):
        images, pdfs = self.fake_convert(*args, **kwargs)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(pdf_image, "convert", images))
        stack.enter_context(mock.patch.object(pdf_pages, "convert", pdfs))
        return stack

    def test_ingest_extracts_them_concurrently_and_a_refusal_never_stops_the_cohort(self):
        provision(self.root)
        calls, active = [], {"now": 0, "peak": 0}
        for module in (pdf_image, pdf_pages):
            module.forget_failures()
            self.addCleanup(module.forget_failures)
        with self.faked(calls, {"locked.pdf": "encrypted"}, active):
            report_ = cohort.ingest(self.root, self.inbox, system="inbox", actor="runner",
                                    extract_concurrency=3)
            outcomes = {e["relative"]: (e.get("outcome"), e.get("code")) for e in report_["entries"]}
            self.assertEqual(outcomes["locked.pdf"], ("failed", "extract-encrypted"))
            for name in ("a.pdf", "shot.png", "photo.jpg", "note.md"):
                self.assertEqual(outcomes[name][0], "extracted", name)
            # Converted once each, ahead of the ledger and in parallel; the stages reused the results.
            self.assertEqual(sorted(calls), ["a.pdf", "locked.pdf", "photo.jpg", "shot.png"])
            self.assertGreater(active["peak"], 1)
            self.assertLessEqual(active["peak"], 3)
            self.assertEqual(report_["counts"]["failed_by_code"], {"prepare/extract-encrypted": 1})
            self.assertEqual((pdf_image._FAILED, pdf_pages._FAILED), ({}, {}))  # refusals are not retained
            again = cohort.ingest(self.root, self.inbox, system="inbox", actor="runner")
        self.assertEqual(len(calls), 4)  # a decided file is not converted again
        self.assertEqual(again["counts"]["outcome"], report_["counts"]["outcome"])
        records = {r["id"]: r for r in catalog.read_records(self.root, limit=200)["records"]}
        formats = set()
        for entry in report_["entries"]:
            if entry.get("outcome") != "extracted" or entry["format"] not in pdf_image.FORMATS:
                continue
            raw = (self.root / entry["rendition"]).read_bytes()
            value = json.loads(raw)
            formats.add((value["payload"]["format"], value["payload"]["method"]["kind"]))
            self.assertIn("A-extract-" + value["run_id"], records)
            located = list(renditions.records(raw, value, index.rendition_api()))
            self.assertEqual(located[0]["source_location"]["page"], 1)
            self.assertEqual(standing.EXTRACT_FORMATS[value["payload"]["format"]], "lines")
        self.assertEqual(formats, {("pdf-pages", "pdf-pages"), ("ocr", "docling-ocr")})

    def test_a_crash_between_conversion_and_ledger_is_completed_by_the_next_run(self):
        provision(self.root)
        calls = []
        with self.faked(calls, {}), \
                mock.patch.object(cohort_runtime, "convert_ahead", side_effect=RuntimeError("crash")), \
                self.assertRaises(RuntimeError):
            cohort.ingest(self.root, self.inbox, system="inbox", actor="runner")
        with self.faked(calls, {"locked.pdf": "encrypted"}):
            report_ = cohort.ingest(self.root, self.inbox, system="inbox", actor="runner")
        self.assertEqual(report_["counts"]["outcome"]["extracted"], 4)
        self.assertEqual(report_["counts"]["outcome"]["failed"], 1)


@unittest.skipUnless(PROVISIONED, "pinned Docling runtime not provisioned")
class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.temp.name) / "work"
        cls.work.mkdir(mode=0o700)
        # Rasters of real text are drawn with the pinned runtime's own renderer (test fixtures only).
        source = Path(cls.temp.name) / "text.pdf"
        source.write_bytes(text_pdf([[SENTENCE % i for i in range(3)]]))
        script = ("import sys, pypdfium2; d = pypdfium2.PdfDocument(sys.argv[1]); "
                  "i = d[0].render(scale=3).to_pil().convert('RGB'); "
                  "i.save(sys.argv[2]); i.save(sys.argv[3]); i.save(sys.argv[4], resolution=216)")
        cls.png, cls.jpg, cls.scan = (Path(cls.temp.name) / n for n in ("text.png", "text.jpg", "scan.pdf"))
        subprocess.run([str(RUNTIME["python"]), "-I", "-c", script, str(source), str(cls.png), str(cls.jpg),
                        str(cls.scan)], check=True, env={"PATH": os.defpath})

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def convert(self, raw, name, fmt):
        return pdf_image.convert(raw, name, fmt, RUNTIME, self.work)

    def refused(self, raw, name):
        with self.assertRaises(pdf_image.ConversionFailed) as caught:
            self.convert(raw, name, "pdf")
        return caught.exception.code

    def test_a_text_layer_pdf_is_read_without_ocr(self):
        payload, stats = self.convert(text_pdf([[SENTENCE % i for i in range(8)], "Second page"]), "a.pdf", "pdf")
        self.assertEqual((payload["format"], payload["method"]["kind"], stats["kind"]),
                         ("pdf-text", "pdf-text-layer", "pdf-text-layer"))
        self.assertEqual(payload["lines"][0], {"line": 1, "page": 1, "text": SENTENCE % 0})
        self.assertEqual(payload["lines"][-1], {"line": 9, "page": 2, "text": "Second page"})

    def test_a_scanned_pdf_falls_back_to_ocr(self):
        payload, _ = self.convert(self.scan.read_bytes(), "scan.pdf", "pdf")
        self.assertEqual((payload["format"], payload["source_format"]), ("ocr", "pdf"))
        self.assertIn("fewer than", payload["method"]["fallback"])
        self.assertIn("quick brown fox", " ".join(line["text"] for line in payload["lines"]))

    def test_images_are_read_by_ocr(self):
        for path in (self.png, self.jpg):
            with self.subTest(path=path.name):
                payload, _ = self.convert(path.read_bytes(), path.name, "image")
                self.assertEqual((payload["format"], payload["method"]["kind"]), ("ocr", "docling-ocr"))
                self.assertIn("quick brown fox", " ".join(line["text"] for line in payload["lines"]))

    def test_encrypted_malformed_and_textless_pdfs_are_refused_with_codes(self):
        self.assertEqual(self.refused(encrypted_pdf(), "locked.pdf"), "extract-encrypted")
        self.assertEqual(self.refused(b"%PDF-1.4\nnot a pdf\n", "bad.pdf"), "extract-malformed-source")
        self.assertEqual(self.refused(image_pdf(), "grey.pdf"), "extract-empty-conversion")

    def test_cohort_ingest_reads_pdfs_and_images_end_to_end(self):
        root, inbox = Path(self.temp.name) / "instance", Path(self.temp.name) / "inbox"
        root.mkdir()
        inbox.mkdir()
        store.initialize(root)
        catalog.initialize(root)
        (inbox / "a.pdf").write_bytes(text_pdf([[SENTENCE % i for i in range(8)]]))
        (inbox / "shot.png").write_bytes(self.png.read_bytes())
        with mock.patch.dict(os.environ, {pdf_image.PYTHON_ENV: str(RUNTIME["python"]),
                                          pdf_image.ARTIFACTS_ENV: str(RUNTIME["artifacts"])}):
            result = cohort.ingest(root, inbox, system="inbox", actor="runner", extract_concurrency=2)
        self.assertEqual(result["counts"]["outcome"]["extracted"], 2)
        kinds = {e["relative"]: json.loads((root / e["rendition"]).read_bytes())["payload"]["method"]["kind"]
                 for e in result["entries"]}
        self.assertEqual(kinds, {"a.pdf": "pdf-pages", "shot.png": "docling-ocr"})
        self.assertEqual(list(pdf_image.work_directory(root).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
