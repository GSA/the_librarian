"""Lightweight worker contract tests; real acceptance is explicitly opt-in."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("docling_worker", HERE / "docling_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.home = HERE / ".local" / ("test-worker-" + uuid.uuid4().hex)
        self.home.mkdir(parents=True, mode=0o700)
        self.assets = self.home / "models"
        self.assets.mkdir()
        (self.assets / "weights.bin").write_bytes(b"public synthetic model bytes")

    def tearDown(self):
        shutil.rmtree(self.home)

    def test_fixed_profile_forbids_remote_and_automatic_backends(self):
        p = worker.profile()
        self.assertEqual(p["version"], 2)
        self.assertEqual((p["device"], p["inference_threads"]), ("cpu", 1))
        self.assertEqual(p["pipeline"], "standard")
        self.assertEqual(p["ocr"]["engine"], "rapidocr")
        self.assertEqual(p["ocr"]["backend"], "onnxruntime")
        self.assertTrue(p["ocr"]["enabled"])
        self.assertEqual(p["ocr"]["models"], {
            "det": "RapidOcr/en_PP-OCRv3_det_infer.onnx",
            "cls": "RapidOcr/ch_ppocr_mobile_v2.0_cls_infer.onnx",
            "rec": "RapidOcr/en_PP-OCRv3_rec_infer.onnx",
        })
        self.assertNotIn("model_size", p["ocr"])
        self.assertEqual(p["tables"], {"enabled": True, "mode": "accurate"})
        for key in ("remote_services", "external_plugins", "picture_description",
                    "link_fetching", "enrichments", "retain_images"):
            self.assertIs(p[key], False)
        self.assertEqual(p["limits"], {
            "source_bytes": 64 * 1024**2, "pdf_pages": 100,
            "page_pixels": 32_000_000, "result_bytes": 64 * 1024**2,
            "address_space_bytes": 16 * 1024**3, "wall_seconds": 600,
            "diagnostic_bytes": 64 * 1024,
        })
        p["ocr"]["enabled"] = False
        self.assertTrue(worker.profile()["ocr"]["enabled"])

    def test_required_assets_name_the_pinned_ocr_models(self):
        self.assertEqual(worker.REQUIRED_ASSETS[5:8], (
            "RapidOcr/en_PP-OCRv3_det_infer.onnx",
            "RapidOcr/ch_ppocr_mobile_v2.0_cls_infer.onnx",
            "RapidOcr/en_PP-OCRv3_rec_infer.onnx",
        ))

    def test_profile_version_distinguishes_ocr_model_changes(self):
        """A rendition's profile must state which models produced it.

        Superseding the OCR pins has to change the profile's canonical bytes,
        so a rendition prepared under the old models can never compare equal
        to one prepared under the new ones.
        """
        current = worker.canonical_bytes(worker.profile())
        superseded = worker.REQUIRED_ASSETS[:5] + (
            "RapidOcr/PP-OCRv6_det_small.onnx",
            "RapidOcr/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
            "RapidOcr/PP-OCRv6_rec_small.onnx",
        )
        with patch.object(worker, "REQUIRED_ASSETS", superseded):
            other = worker.canonical_bytes(worker.profile())
        self.assertNotEqual(current, other)

    def test_compatible_substituted_assets_refused_before_loading(self):
        files = [{"path": path, "size": size, "sha256": sha}
                 for path, (size, sha) in zip(worker.REQUIRED_ASSETS, worker.ASSET_PINS)]
        for index in range(len(files)):
            with self.subTest(path=files[index]["path"]):
                substituted = [dict(record) for record in files]
                substituted[index]["sha256"] = "0" * 64
                with patch.dict(sys.modules, {"onnxruntime": None}):
                    with self.assertRaisesRegex(worker.WorkerError, "pinned model asset mismatch"):
                        worker._validate_assets(self.assets, {"files": substituted})
        self.assertEqual(len(worker.profile()["asset_lock"]["files"]), 8)

    def test_conversion_report_binds_same_operation_identity(self):
        payload = self.payload()
        runtime = {"docling": "pinned-test-version"}
        pages = [{"page_no": 1, "width": 100, "height": 100}]
        with (patch.object(worker, "_inspect_source", return_value=pages),
              patch.object(worker, "_versions", return_value=runtime),
              patch.object(worker, "_validate_assets"),
              patch.object(worker, "_run_conversion", return_value=payload)):
            report = worker.convert(b"source", "source.pdf", "pdf", self.assets)
        self.assertEqual(set(report), {"conversion", "runtime", "profile", "assets"})
        self.assertEqual(report["conversion"], payload)
        self.assertEqual(report["runtime"], runtime)
        self.assertEqual(report["profile"], worker.profile())
        self.assertEqual(report["assets"], worker.asset_manifest(self.assets))
        self.assertEqual(worker.canonical_bytes(report), worker.canonical_bytes(report))

    def test_model_binding_detects_changed_bytes_and_outside_links(self):
        before = worker.asset_manifest(self.assets)
        self.assertEqual(before["files"][0]["path"], "weights.bin")
        (self.assets / "weights.bin").write_bytes(b"changed")
        self.assertNotEqual(before["sha256"], worker.asset_manifest(self.assets)["sha256"])
        worker.assert_assets_unchanged(self.assets, worker.asset_manifest(self.assets))
        with self.assertRaisesRegex(worker.WorkerError, "changed"):
            worker.assert_assets_unchanged(self.assets, before)
        (self.home / "outside").write_bytes(b"outside")
        (self.assets / "weights.bin").unlink()
        (self.assets / "weights.bin").symlink_to(self.home / "outside")
        with self.assertRaisesRegex(worker.WorkerError, "symlink"):
            worker.asset_manifest(self.assets)

    def test_source_inspection_rejects_encryption_and_large_geometry(self):
        with self.assertRaisesRegex(worker.WorkerError, "encrypted"):
            worker.validate_pages([{"page_no": 1, "width": 10, "height": 10}],
                                  encrypted=True)
        with self.assertRaisesRegex(worker.WorkerError, "pixels"):
            worker.validate_pages([{"page_no": 1, "width": 2000, "height": 2000}])
        self.assertEqual(worker.validate_pages(
            [{"page_no": 1, "width": 1000, "height": 1000}])[0]["page_no"], 1)
        for value in (float("nan"), float("inf"), -1, 0):
            with self.assertRaises(worker.WorkerError):
                worker.validate_pages([{"page_no": 1, "width": value, "height": 10}])
        with self.assertRaises(worker.WorkerError):
            worker.validate_pages([{"page_no": 1, "width": 1e308, "height": 10}])
        with self.assertRaisesRegex(worker.WorkerError, "pages"):
            worker.validate_pages([{"page_no": i + 1, "width": 1, "height": 1}
                                   for i in range(101)])

    def document(self):
        return {
            "schema_name": "DoclingDocument", "version": "1.0.0", "name": "sample",
            "body": {"self_ref": "#/body", "children": [{"$ref": "#/tables/0"}]},
            "tables": [{"self_ref": "#/tables/0", "label": "table",
                        "prov": [{"page_no": 1, "bbox": {
                            "l": 1, "t": 2, "r": 80, "b": 40, "coord_origin": "TOPLEFT"}}],
                        "data": {"table_cells": [
                            {"text": "0123456789", "start_row_offset_idx": 0,
                             "start_col_offset_idx": 0, "end_row_offset_idx": 1,
                             "end_col_offset_idx": 1}],
                            "num_rows": 1, "num_cols": 1}}],
            "texts": [{"self_ref": "#/texts/0", "label": "text", "text": "Not approved",
                       "source_timestamp": "source meaning preserved",
                       "prov": [{"page_no": 1, "bbox": {
                           "l": 1, "t": 2, "r": 80, "b": 40, "coord_origin": "TOPLEFT"}}]}],
            "pages": {"1": {"page_no": 1, "size": {"width": 100, "height": 100}}}
        }

    def payload(self, **overrides):
        args = {"document": self.document(), "format_name": "pdf",
                "pages": [{"page_no": 1, "width": 100, "height": 100}],
                "processed_pages": [1], "status": "success",
                "confidence": {"1": {"ocr_score": float("nan"), "layout_score": 0.2}},
                "errors": []}
        args.update(overrides)
        return worker.build_payload(**args)

    def test_blank_pages_are_not_failed_pages(self):
        pages = [{"page_no": 1, "width": 100, "height": 100},
                 {"page_no": 2, "width": 100, "height": 100}]
        doc = self.document()
        doc["pages"]["2"] = {"page_no": 2, "size": {"width": 100, "height": 100}}
        result = self.payload(document=doc, pages=pages, processed_pages=[1, 2])
        self.assertEqual(result["pages"][1]["textless"], True)
        self.assertEqual(result["pages"][1]["page_no"], 2)
        with self.assertRaisesRegex(worker.WorkerError, "page"):
            self.payload(document=doc, pages=pages, processed_pages=[1])

    def test_partial_or_empty_conversion_refuses(self):
        for status in ("partial_success", "failure", "pending"):
            with self.assertRaisesRegex(worker.WorkerError, "status"):
                self.payload(status=status)
        with self.assertRaisesRegex(worker.WorkerError, "empty"):
            self.payload(document={"texts": [], "tables": [], "pages": {
                "1": {"page_no": 1, "size": {"width": 100, "height": 100}}}})
        with self.assertRaisesRegex(worker.WorkerError, "error"):
            self.payload(errors=["failed page"])

    def test_payload_preserves_tables_provenance_and_unknown_confidence(self):
        result = self.payload()
        self.assertEqual(result["document"], self.document())
        self.assertEqual(result["confidence"]["1"]["ocr_score"], {"state": "unknown"})
        self.assertIn("low_confidence", {w["code"] for w in result["warnings"]})
        self.assertIn("unknown_confidence", {w["code"] for w in result["warnings"]})
        raw = worker.canonical_bytes(result)
        self.assertEqual(json.loads(raw)["document"]["tables"], self.document()["tables"])
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.assertRaises(worker.WorkerError):
                worker.canonical_bytes({"bad": value})
        doc = self.document()
        doc["texts"][0]["prov"][0]["bbox"]["t"] = float("nan")
        with self.assertRaises(worker.WorkerError):
            self.payload(document=doc)

    def test_confidence_grades_are_preserved_and_unspecified_is_unknown(self):
        grades = {"mean_grade": "fair", "low_grade": "poor", "mean_score": 0.65,
                  "low_score": 0.275, "pages": {"1": {"mean_grade": "unspecified"}}}
        payload = self.payload(confidence=grades)
        self.assertEqual(payload["confidence"]["mean_grade"], "fair")
        self.assertEqual(payload["confidence"]["low_grade"], "poor")
        self.assertEqual(payload["confidence"]["pages"]["1"]["mean_grade"],
                         {"state": "unknown"})
        with self.assertRaises(worker.WorkerError):
            self.payload(confidence={"mean_grade": "perfect"})

    def test_empty_tables_do_not_make_an_empty_conversion_usable(self):
        doc = self.document()
        doc["texts"] = []
        doc["tables"][0]["data"]["table_cells"][0]["text"] = ""
        with self.assertRaisesRegex(worker.WorkerError, "empty"):
            self.payload(document=doc)

    def test_invalid_coordinates_and_content_are_not_silently_null(self):
        for bad in (None, float("inf"), float("nan"), -1, 200):
            doc = self.document()
            doc["texts"][0]["prov"][0]["bbox"]["l"] = bad
            with self.assertRaises(worker.WorkerError):
                self.payload(document=doc)
        doc = self.document()
        doc["texts"][0]["text"] = float("nan")
        with self.assertRaises(worker.WorkerError):
            self.payload(document=doc)
        for bad in (1.1, -0.01, "favorable", True):
            with self.assertRaises(worker.WorkerError):
                self.payload(confidence={"1": {"ocr_score": bad}})

    def test_byte_and_serialization_limits(self):
        with patch.dict(worker.LIMITS, source_bytes=3):
            with self.assertRaisesRegex(worker.WorkerError, "byte limit"):
                worker.convert(b"1234", "test.pdf", "pdf", self.assets)
        with self.assertRaisesRegex(worker.WorkerError, "byte limit"):
            worker.canonical_bytes({"large": "x" * 50}, max_bytes=32)
        with self.assertRaises(worker.WorkerError):
            worker.canonical_bytes({"bad": "\ud800"})

    def test_missing_dependency_or_assets_is_not_success(self):
        with patch.object(worker.importlib.metadata, "version",
                          side_effect=worker.importlib.metadata.PackageNotFoundError):
            with self.assertRaisesRegex(worker.WorkerError, "missing optional dependency"):
                worker.inspect_runtime(self.assets)
        with patch.object(worker, "_versions", return_value={}):
            with self.assertRaisesRegex(worker.WorkerError, "missing required model"):
                worker.inspect_runtime(self.assets)
        with self.assertRaisesRegex(worker.WorkerError, "missing"):
            worker.asset_manifest(self.home / "missing")
        with (patch.object(worker, "_versions", return_value={"observed": "version"}),
              patch.object(worker, "_validate_assets")):
            inspected = worker.inspect_runtime(self.assets)
        self.assertEqual(inspected["profile"], worker.profile())
        self.assertEqual(inspected["runtime"], {"observed": "version"})
        self.assertEqual(inspected["assets"], worker.asset_manifest(self.assets))
        self.assertNotIn(str(self.home), worker.canonical_bytes(inspected).decode())

    def test_mutation_before_or_after_inference_cannot_succeed(self):
        source_pages = [{"page_no": 1, "width": 100, "height": 100}]
        for mode in ("bytes", "rename", "symlink", "root_link"):
            with self.subTest(mode=mode):
                path = self.assets / "weights.bin"
                if path.is_symlink():
                    path.unlink()
                path.write_bytes(b"synthetic")

                def inference(*args):
                    if mode == "bytes":
                        path.write_bytes(b"changed")
                    elif mode == "rename":
                        path.rename(self.assets / "other.bin")
                    elif mode == "symlink":
                        path.unlink()
                        path.symlink_to(self.home / "external.bin")
                    else:
                        self.assets.rename(self.home / "moved")
                        self.assets.symlink_to(self.home / "moved", target_is_directory=True)
                    return self.payload()

                with (patch.object(worker, "_inspect_source", return_value=source_pages),
                      patch.object(worker, "_versions", return_value={}),
                      patch.object(worker, "_validate_assets"),
                      patch.object(worker, "_run_conversion", side_effect=inference)):
                    with self.assertRaises(worker.WorkerError):
                        worker.convert(b"source", "source.pdf", "pdf", self.assets)
                if self.assets.is_symlink():
                    self.assets.unlink()
                    (self.home / "moved").rename(self.assets)
                for entry in self.assets.iterdir():
                    entry.unlink()

        def mutate_before(*args):
            (self.assets / "late.bin").write_bytes(b"new asset")
        with (patch.object(worker, "_inspect_source", return_value=source_pages),
              patch.object(worker, "_versions", return_value={}),
              patch.object(worker, "_validate_assets", side_effect=mutate_before),
              patch.object(worker, "_run_conversion") as inference):
            (self.assets / "weights.bin").write_bytes(b"original")
            with self.assertRaisesRegex(worker.WorkerError, "before inference"):
                worker.convert(b"source", "source.pdf", "pdf", self.assets)
            inference.assert_not_called()

    def test_inspection_and_conversion_have_distinct_exact_job_schemas(self):
        job = {"schema": "docling-worker-job-v1", "operation": "inspect",
               "artifacts_path": str(self.assets)}
        with patch.object(worker, "inspect_runtime", return_value={"runtime": "observed"}) as inspect:
            self.assertEqual(worker.execute_job(job), {"runtime": "observed"})
            inspect.assert_called_once_with(self.assets)
        source = self.home / "input.pdf"
        source.write_bytes(b"snapshot")
        source.chmod(0o600)
        conversion_job = dict(job, operation="convert", source_path=str(source),
                              name="logical.pdf", format="pdf")
        with patch.object(worker, "convert", return_value={"complete": True}) as convert:
            self.assertEqual(worker.execute_job(conversion_job), {"complete": True})
            convert.assert_called_once_with(b"snapshot", "logical.pdf", "pdf", self.assets)
        for changes in ({"source_path": "unrequested"}, {"operation": "download"},
                        {"schema": "v2"}, {"artifacts_path": None}):
            with self.assertRaises(worker.WorkerError):
                worker.execute_job(dict(job, **changes))
        with self.assertRaises(worker.WorkerError):
            worker._parse_json(b'{"x":1,"x":2}')
        for raw in (b'{"x":NaN}', b'{"x":Infinity}', b'{bad}'):
            with self.assertRaises(worker.WorkerError):
                worker._parse_json(raw)

    def test_worker_cli_private_bounded_failure_and_no_overwrite(self):
        job = self.home / "job.json"
        result = self.home / "result.json"
        job.write_bytes(b'{"schema":"unknown"}')
        job.chmod(0o600)
        command = [sys.executable, str(HERE / "docling_worker.py"), str(job), str(result)]
        p = subprocess.run(command, capture_output=True, timeout=20)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertLess(len(result.read_bytes()), 65536)
        self.assertEqual(result.stat().st_mode & 0o777, 0o600)
        error = json.loads(result.read_bytes())
        self.assertFalse(error["ok"])
        self.assertEqual(error["error"]["code"], "invalid_job")
        self.assertNotIn(str(self.home), result.read_text())
        original = result.read_bytes()
        self.assertEqual(subprocess.run(command, capture_output=True, timeout=20).returncode, 2)
        self.assertEqual(original, result.read_bytes())
        result.unlink()
        job.chmod(0o644)
        self.assertEqual(subprocess.run(command, capture_output=True, timeout=20).returncode, 1)
        self.assertEqual(json.loads(result.read_bytes())["error"]["code"], "unsafe_path")
        result.unlink()
        result.symlink_to(job)
        process = subprocess.run(command, capture_output=True, timeout=20)
        self.assertNotEqual(process.returncode, 0)
        self.assertNotIn(b"Traceback", process.stderr)
        self.assertLess(len(process.stderr), 65536)

    def test_worker_result_stays_in_retained_parent_after_symlink_swap(self):
        job = self.home / "job.json"
        job.write_bytes(b"{}")
        job.chmod(0o600)
        parent = self.home / "private-output"
        parent.mkdir(mode=0o700)
        retained = self.home / "renamed-output"
        redirected = self.home / "redirected-output"
        redirected.mkdir(mode=0o700)
        script = (f"import sys;sys.path.insert(0,{str(HERE)!r});"
                  "import docling_worker as w;from pathlib import Path;"
                  f"parent=Path({str(parent)!r});retained=Path({str(retained)!r});"
                  f"redirected=Path({str(redirected)!r});"
                  "def_placeholder")
        script = script.replace("def_placeholder", "\n"
            "def swap(_):\n"
            " parent.rename(retained)\n"
            " parent.symlink_to(redirected, target_is_directory=True)\n"
            " return {'complete': True}\n"
            "w.execute_job=swap\n"
            "raise SystemExit(w.main(sys.argv[1:]))\n")
        process = subprocess.run([sys.executable, "-c", script, str(job),
                                  str(parent / "result.json")], capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(list(redirected.iterdir()), [])
        result = retained / "result.json"
        self.assertTrue(json.loads(result.read_bytes())["ok"])
        self.assertEqual(result.stat().st_mode & 0o777, 0o600)

    def test_worker_alarm_cannot_interrupt_result_publication(self):
        job = self.home / "job.json"
        job.write_bytes(b"{}")
        job.chmod(0o600)
        for ok in (True, False):
            with self.subTest(ok=ok):
                result = self.home / ("alarm-success.json" if ok else "alarm-error.json")
                script = (f"import sys,os,signal;sys.path.insert(0,{str(HERE)!r});"
                          "import docling_worker as w\n"
                          "real_open=w.os.open\n"
                          "real_fsync=w.os.fsync\n"
                          "def opening(path,flags,*args,**kwargs):\n"
                          " if flags & os.O_CREAT:\n"
                          "  assert signal.alarm(0)==0\n"
                          "  os.kill(os.getpid(),signal.SIGALRM)\n"
                          " return real_open(path,flags,*args,**kwargs)\n"
                          "def syncing(fd):\n"
                          " os.kill(os.getpid(),signal.SIGALRM)\n"
                          " return real_fsync(fd)\n"
                          "def execute(_):\n"
                          f" if not {ok!r}: raise w.WorkerError('synthetic timeout','timeout')\n"
                          " return {'complete':True}\n"
                          "w.os.open=opening\nw.os.fsync=syncing\nw.execute_job=execute\n"
                          "raise SystemExit(w.main(sys.argv[1:]))\n")
                process = subprocess.run([sys.executable, "-c", script, str(job), str(result)],
                                         capture_output=True, timeout=10)
                self.assertEqual(process.returncode, 0 if ok else 1, process.stderr)
                self.assertNotIn(b"Traceback", process.stderr)
                observed = json.loads(result.read_bytes())
                self.assertEqual(observed["ok"], ok)
                if not ok:
                    self.assertEqual(observed["error"]["code"], "timeout")
                self.assertEqual(result.stat().st_mode & 0o777, 0o600)

    def test_worker_cli_enforces_process_and_output_budgets(self):
        job = self.home / "job.json"
        job.write_bytes(b"{}")
        job.chmod(0o600)
        prefix = (f"import sys,time,resource;sys.path.insert(0,{str(HERE)!r});"
                  "import docling_worker as w;")
        for mode, expression in (
                ("limits", "w.execute_job=lambda _: {'address_space':"
                 "resource.getrlimit(resource.RLIMIT_AS)[0],"
                 "'file_bytes':resource.getrlimit(resource.RLIMIT_FSIZE)[0]};"),
                ("output", "w.LIMITS['result_bytes']=256;"
                 "w.execute_job=lambda _: {'oversize':'x'*1024};"),
                ("timeout", "w.LIMITS['wall_seconds']=1;"
                 "w.execute_job=lambda _: time.sleep(3);")):
            with self.subTest(mode=mode):
                result = self.home / (mode + ".json")
                process = subprocess.run(
                    [sys.executable, "-c", prefix + expression +
                     "raise SystemExit(w.main(sys.argv[1:]))", str(job), str(result)],
                    capture_output=True, timeout=10)
                observed = json.loads(result.read_bytes())
                if mode == "limits":
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertEqual(observed["payload"]["address_space"], 16 * 1024**3)
                    self.assertEqual(observed["payload"]["file_bytes"], 64 * 1024**2)
                else:
                    self.assertEqual(process.returncode, 1, process.stderr)
                    self.assertFalse(observed["ok"])
                    self.assertEqual(observed["error"]["code"],
                                     "output_limit" if mode == "output" else "timeout")
                    self.assertLessEqual(result.stat().st_size, 256)


def native_pdf(*, password=None):
    """Small public-text PDF, no third-party fixture or generation dependency."""
    lines = [
        "BT /F1 20 Tf 50 750 Td (Synthetic ingestion check) Tj ET",
        "BT /F1 14 Tf 50 715 Td (We do not approve release.) Tj ET",
        "BT /F1 14 Tf 50 690 Td (0123456789) Tj ET",
    ]
    columns = {
        "LEFT": ("FIRST alpha beta gamma", "SECOND delta epsilon zeta",
                 "THIRD eta theta iota kappa", "FOURTH lambda mu nu",
                 "FIFTH xi omicron pi rho"),
        "RIGHT": ("FIRST beta gamma delta", "SECOND epsilon zeta eta",
                  "THIRD theta iota kappa", "FOURTH mu nu xi omicron",
                  "FIFTH omicron pi rho"),
    }
    # A real two-column layout needs a visible gutter and enough lines for the
    # layout model to distinguish columns from a two-row grid. The former
    # three-short-line fixture had a 30pt gutter and was correctly clustered
    # across rows, so it did not test the promised column-major reading order.
    for x, prefix in ((50, "LEFT"), (350, "RIGHT")):
        for i, tail in enumerate(columns[prefix]):
            lines.append(f"BT /F1 11 Tf {x} {655-i*18} Td ({prefix} {tail}) Tj ET")
    for y in (517, 477, 437, 397):
        lines.append(f"50 {y} m 550 {y} l S")
    for x in (50, 300, 550):
        lines.append(f"{x} 397 m {x} 517 l S")
    for row, cells in enumerate((("Item", "Count"), ("Alpha", "7"), ("Beta", "9"))):
        for x, text in zip((65, 315), cells):
            lines.append(f"BT /F1 16 Tf {x} {502-row*40} Td ({text}) Tj ET")
    content = "\n".join(lines).encode("ascii")
    trailer_extra = ""
    encryption_dict = None
    if password is not None:
        import hashlib
        import struct
        padding = bytes.fromhex(
            "28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a")

        def padded(text):
            return (text.encode("ascii") + padding)[:32]

        def rc4(key, data):
            state, j = list(range(256)), 0
            for i in range(256):
                j = (j + state[i] + key[i % len(key)]) % 256
                state[i], state[j] = state[j], state[i]
            result, i, j = bytearray(), 0, 0
            for value in data:
                i = (i + 1) % 256
                j = (j + state[i]) % 256
                state[i], state[j] = state[j], state[i]
                result.append(value ^ state[(state[i] + state[j]) % 256])
            return bytes(result)

        owner = rc4(hashlib.md5(padded("public-test-owner")).digest()[:5], padded(password))
        file_id = hashlib.md5(b"public-synthetic-encrypted-fixture").digest()
        key = hashlib.md5(padded(password) + owner + struct.pack("<i", -4) + file_id).digest()[:5]
        user = rc4(key, padding)
        content = rc4(hashlib.md5(key + b"\x05\x00\x00\x00\x00").digest()[:10], content)
        encryption_dict = (f"<< /Filter /Standard /V 1 /R 2 /Length 40 /P -4 "
                           f"/O <{owner.hex()}> /U <{user.hex()}> >>").encode()
        trailer_extra = f" /Encrypt 6 0 R /ID [<{file_id.hex()}><{file_id.hex()}>]"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
    ]
    if encryption_dict is not None:
        objects.append(encryption_dict)
    result = b"%PDF-1.7\n"
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(result)
    result += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    result += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets[1:])
    result += (f"trailer\n<< /Size {len(offsets)} /Root 1 0 R{trailer_extra} >>\n"
               f"startxref\n{xref}\n%%EOF\n").encode()
    return result


@unittest.skipUnless(os.environ.get("DOCLING_REAL_TESTS") == "1",
                     "opt in with DOCLING_REAL_TESTS=1 and DOCLING_ARTIFACTS_PATH")
class RealWorkerTests(unittest.TestCase):
    """Every inference is a fresh unshare -Urn subprocess, never an online retry."""
    def setUp(self):
        self.home = HERE / ".local" / ("real-worker-" + uuid.uuid4().hex)
        self.home.mkdir(parents=True, mode=0o700)
        self.assets = Path(os.environ["DOCLING_ARTIFACTS_PATH"]).absolute()

    def tearDown(self):
        shutil.rmtree(self.home)

    def fixtures(self):
        from io import BytesIO
        import pypdfium2 as pdfium
        native = native_pdf()
        with pdfium.PdfDocument(native) as document:
            page = document[0]
            bitmap = page.render(scale=2)
            image = bitmap.to_pil().copy()
            bitmap.close()
            page.close()
        png, jpeg, scan = BytesIO(), BytesIO(), BytesIO()
        image.save(png, format="PNG", dpi=(144, 144))
        image.save(jpeg, format="JPEG", quality=98, dpi=(144, 144))
        image.convert("RGB").save(scan, format="PDF", resolution=144)
        mixed = BytesIO()
        with (pdfium.PdfDocument(native) as one,
              pdfium.PdfDocument(scan.getvalue()) as two,
              pdfium.PdfDocument.new() as output):
            output.import_pages(one)
            blank = output.new_page(600, 800)
            blank.close()
            output.import_pages(two)
            output.save(mixed)
        return {"native.pdf": native, "scanned.pdf": scan.getvalue(),
                "mixed.pdf": mixed.getvalue(), "image.png": png.getvalue(),
                "image.jpg": jpeg.getvalue()}

    def check_fixture(self, name):
        import hashlib
        source = self.home / name
        source.write_bytes(self.fixtures()[name])
        source.chmod(0o600)
        format_name = "pdf" if name.endswith(".pdf") else "image"
        outputs, runs = [], []
        for n in range(2):
            job, result = self.home / f"job-{n}.json", self.home / f"result-{n}.json"
            job.write_bytes(worker.canonical_bytes({
                "schema": "docling-worker-job-v1", "operation": "convert",
                "source_path": str(source), "name": name, "format": format_name,
                "artifacts_path": str(self.assets),
            }))
            job.chmod(0o600)
            env = dict(os.environ, TMPDIR=str(self.home))
            process = subprocess.run(
                ["unshare", "-Urn", sys.executable, str(HERE / "docling_worker.py"),
                 str(job), str(result)], capture_output=True, timeout=620, env=env)
            raw = result.read_bytes() if result.exists() else b""
            runs.append((process.returncode, raw[:4096], process.stderr[-4096:]))
            outputs.append(raw)
        self.assertEqual([r[0] for r in runs], [0, 0], repr(runs))
        # Independent runs, complete result envelopes, no normalized/dropped fields.
        self.assertEqual(outputs[0], outputs[1], "complete canonical rendition differs")
        envelope = json.loads(outputs[0])
        self.assertTrue(envelope["ok"])
        report = envelope["payload"]
        self.assertEqual(set(report), {"conversion", "runtime", "profile", "assets"})
        self.assertEqual(report["profile"], worker.profile())
        self.assertEqual(report["assets"], worker.asset_manifest(self.assets))
        payload = report["conversion"]
        self.assertEqual(worker.canonical_bytes(envelope), outputs[0])
        texts = [t["text"] for t in payload["document"]["texts"]]
        text = " ".join(texts)
        self.assertIn("We do not approve release.", text)
        self.assertIn("0123456789", text)
        column_labels = ("FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH")
        positions = ([text.index("LEFT " + label) for label in column_labels]
                     + [text.index("RIGHT " + label) for label in column_labels])
        self.assertEqual(positions, sorted(positions), text)
        matches = []
        for table in payload["document"]["tables"]:
            cells = table["data"]["table_cells"]
            by_cell = {(c["start_row_offset_idx"], c["start_col_offset_idx"]):
                       c["text"].strip() for c in cells}
            if (by_cell.get((0, 0)), by_cell.get((0, 1)),
                by_cell.get((1, 0)), by_cell.get((1, 1)),
                by_cell.get((2, 0)), by_cell.get((2, 1))) == (
                    "Item", "Count", "Alpha", "7", "Beta", "9"):
                matches.append(table)
        self.assertTrue(matches, "table row/column associations not preserved")
        expected_pages = 3 if name == "mixed.pdf" else 1
        self.assertEqual([p["page_no"] for p in payload["pages"]],
                         list(range(1, expected_pages + 1)))
        if name == "mixed.pdf":
            self.assertTrue(payload["pages"][1]["textless"])
            self.assertFalse(payload["pages"][0]["textless"])
            self.assertFalse(payload["pages"][2]["textless"])
        text_pages, located = worker._provenance(payload["document"], payload["pages"])
        self.assertGreater(located, 0)
        self.assertEqual(text_pages, {1, 3} if name == "mixed.pdf" else {1})
        print(f"REAL PASS {name}: two isolated complete canonical runs; "
              f"{len(outputs[0])} bytes sha256={hashlib.sha256(outputs[0]).hexdigest()}")

    def test_real_native_pdf(self):
        self.check_fixture("native.pdf")

    def test_real_scanned_pdf(self):
        self.check_fixture("scanned.pdf")

    def test_real_mixed_pdf_with_blank(self):
        self.check_fixture("mixed.pdf")

    def test_real_png(self):
        self.check_fixture("image.png")

    def test_real_jpeg(self):
        self.check_fixture("image.jpg")

    def test_actual_source_checks_without_inference(self):
        from io import BytesIO
        import pypdfium2 as pdfium
        from PIL import Image
        with pdfium.PdfDocument(native_pdf()) as native:
            native.del_page(0)
            page = native.new_page(2000, 2000)
            page.close()
            giant = BytesIO()
            native.save(giant)
        with patch.object(worker, "_run_conversion") as inference:
            with self.assertRaisesRegex(worker.WorkerError, "pixels"):
                worker.convert(giant.getvalue(), "large.pdf", "pdf", self.assets)
            inference.assert_not_called()
        for raw, name, selected in (
                (b"%PDF-1.7\nbad", "bad.pdf", "pdf"),
                (b"not a PDF", "bad.pdf", "pdf"),
                (native_pdf(), "bad.png", "image"),
                (native_pdf(), "bad.docx", "docx"),
                (b"\x89PNG\r\n\x1a\nbad", "bad.png", "image")):
            with self.assertRaises(worker.WorkerError):
                worker.convert(raw, name, selected, self.assets)
        for dpi in ((72, 72), (1, 1)):
            # <32M input pixels but >32M at the actual 3x OCR render scale.
            raw = BytesIO()
            Image.new("RGB", (2000, 2000), "white").save(raw, format="PNG", dpi=dpi)
            with self.assertRaisesRegex(worker.WorkerError, "pixels"):
                worker._inspect_source(raw.getvalue(), "large.png", "image")
        animated = BytesIO()
        Image.new("RGB", (10, 10)).save(
            animated, format="PNG", save_all=True,
            append_images=[Image.new("RGB", (10, 10), "white")])
        with self.assertRaisesRegex(worker.WorkerError, "multi-frame"):
            worker._inspect_source(animated.getvalue(), "frames.png", "image")
        for password in ("", "public-test-password"):
            encrypted = native_pdf(password=password)
            with pdfium.PdfDocument(encrypted, password=password) as pdf:
                self.assertEqual(len(pdf), 1)
            with self.assertRaisesRegex(worker.WorkerError, "encrypted"):
                worker._inspect_source(encrypted, "encrypted.pdf", "pdf")
        for name, source in self.fixtures().items():
            pages = worker._inspect_source(source, name, "pdf" if name.endswith(".pdf") else "image")
            self.assertEqual(len(pages), 3 if name == "mixed.pdf" else 1)

    def test_installed_pipeline_configuration_and_render_guards(self):
        from types import SimpleNamespace
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.document import ConversionStatus
        from docling.datamodel.base_models import ConfidenceReport, PageConfidenceScores
        from docling.backend.docling_parse_backend import ThreadedDoclingParseDocumentBackend
        from docling.backend.image_backend import ImageDocumentBackend
        model_doc = WorkerTests.document(self)
        result = SimpleNamespace(
            document=SimpleNamespace(export_to_dict=lambda: model_doc),
            status=ConversionStatus.SUCCESS, errors=[], pages=[SimpleNamespace(page_no=1)],
            confidence=ConfidenceReport(
                pages={1: PageConfidenceScores(parse_score=0.9, layout_score=0.8,
                                               table_score=0.7, ocr_score=0.2)}))
        for selected, fmt in (("pdf", InputFormat.PDF), ("image", InputFormat.IMAGE)):
            with patch("docling.document_converter.DocumentConverter") as converter:
                converter.return_value.convert.return_value = result
                payload = worker._run_conversion(
                    b"synthetic", "test.pdf" if selected == "pdf" else "test.png",
                    selected, self.assets, [{"page_no": 1, "width": 100, "height": 100}])
                self.assertEqual(payload["document"], model_doc)
                captured = converter.call_args.kwargs["format_options"][fmt]
                options = captured.pipeline_options
                self.assertEqual(options.accelerator_options.model_dump(mode="json")["device"], "cpu")
                self.assertEqual(options.accelerator_options.num_threads, 1)
                self.assertFalse(options.enable_remote_services)
                self.assertFalse(options.allow_external_plugins)
                self.assertFalse(options.generate_page_images)
                self.assertFalse(options.generate_picture_images)
                self.assertFalse(options.generate_table_images)
                self.assertEqual(options.ocr_options.backend, "onnxruntime")
                self.assertEqual(options.ocr_options.scale, 3)
                self.assertEqual(options.table_structure_options.mode.value, "accurate")
                self.assertEqual(options.layout_options.engine_options.model_dump(
                    mode="json")["engine_type"], "transformers")
                self.assertFalse(options.layout_options.engine_options.compile_model)
                self.assertEqual(options.ocr_options.lang, ["en"])
                self.assertEqual(options.ocr_options.rapidocr_params[
                    "EngineConfig.onnxruntime.inter_op_num_threads"], 1)
                self.assertEqual(options.ocr_options.rapidocr_params[
                    "EngineConfig.onnxruntime.intra_op_num_threads"], 1)
                import threading
                pipeline = captured.pipeline_cls.__new__(captured.pipeline_cls)
                pipeline._idle_model_threads = []
                pipeline._model_threads_lock = threading.Lock()
                executors = pipeline._borrow_model_threads()
                try:
                    thread_ids = {ex.submit(threading.get_ident).result()
                                  for ex in executors.values()}
                    self.assertEqual(len(thread_ids), 1,
                                     "standard stage defaults run five inference threads")
                finally:
                    for ex in set(executors.values()):
                        ex.shutdown()
                backend = captured.backend.__new__(captured.backend)
                renders = []
                fake_page = SimpleNamespace(
                    get_size=lambda: SimpleNamespace(width=1000, height=1000),
                    get_page_image=lambda **kw: renders.append(kw) or "pixels")
                if selected == "pdf":
                    self.assertEqual(captured.backend_options.parser_threads, 1)
                    self.assertEqual(captured.backend_options.render_scale, 1.0)
                    with patch.object(ThreadedDoclingParseDocumentBackend, "iter_pages",
                                      return_value=iter([fake_page])):
                        guarded = next(backend.iter_pages())
                else:
                    with patch.object(ImageDocumentBackend, "load_page", return_value=fake_page):
                        guarded = backend.load_page(0)
                self.assertEqual(guarded.get_page_image(scale=3), "pixels")
                with self.assertRaisesRegex(worker.WorkerError, "scale"):
                    guarded.get_page_image(scale=4)
                self.assertEqual(len(renders), 1)


if __name__ == "__main__":
    unittest.main()
