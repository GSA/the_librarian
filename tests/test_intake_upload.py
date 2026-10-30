"""Manual file upload and local-drop intake over the durable ledger."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from tools.catalog import catalog_store as catalog
from tools.intake import cli, runner, store, upload
from tools.intake.contracts import IntakeError

TEXT = b"Operator supplied notes.\n"


def value(sid="sub-1", name="notes.txt", fmt="text", **extra):
    return upload.descriptor(submission_id=sid, format_name=fmt, name=name,
                             actor="test-runner", reason="Structural upload test", **extra)


class UploadCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        self.outside = Path(self.temp.name) / "outside"
        self.outside.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)

    def file(self, name="notes.txt", raw=TEXT):
        path = self.outside / name
        path.write_bytes(raw)
        return path

    def records(self):
        return {r["id"]: r for r in catalog.read_records(self.root, limit=100)["records"]}

    def assertCode(self, code, call, *args, **kwargs):
        with self.assertRaises(IntakeError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)


class SubmitTests(UploadCase):
    def test_submission_stages_registers_and_reports_receipt_separately(self):
        ack = upload.submit(self.root, self.file(), value())
        self.assertEqual(ack["receipt"]["state"], "committed")
        self.assertEqual(ack["processing"], {"extraction": "not-started", "review": "not-started",
                                             "selection": "not-selected", "qualified": False})
        self.assertFalse(ack["existing"])
        self.assertEqual(ack["original"]["sha256"], hashlib.sha256(TEXT).hexdigest())
        retained = self.root / ack["original"]["path"]
        self.assertEqual(retained.read_bytes(), TEXT)
        self.assertEqual(stat.S_IMODE(retained.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(retained.parent.stat().st_mode), 0o700)
        self.assertTrue(ack["original"]["path"].startswith("sources/intake/sub-1/"))
        records = self.records()
        artifact = records[ack["records"]["artifact_id"]]["data"]
        self.assertEqual((artifact["role"], artifact["coverage"], artifact["extraction"]),
                         ("original", "pending-extraction", "pending"))
        receipt = records[ack["records"]["receipt_id"]]
        self.assertEqual(receipt["data"]["provenance"][0]["path"], "sources/intake/sub-1/submission.json")
        # Registered through the durable stage pipeline.
        summary = store.inspect(self.root, "upload-sub-1")
        self.assertEqual(summary["stages"][0]["status"], "succeeded")
        self.assertEqual(summary["stages"][0]["evidence"]["boundary"], "receipt-committed")

    def test_submission_provenance_is_preserved_and_filename_never_fabricates_origin(self):
        ack = upload.submit(self.root, self.file("v2-final-github.com-report.txt"),
                            value(name="v2-final-github.com-report.txt"))
        self.assertEqual(ack["origin"], {"kind": "operator-supplied", "system": None,
                                         "locator": None, "version": None})
        self.assertEqual(ack["identity_basis"], "operator-supplied-bytes")
        sidecar = json.loads((self.root / "sources/intake/sub-1/submission.json").read_bytes())
        self.assertEqual(sidecar["submission_id"], "sub-1")
        self.assertEqual(sidecar["actor"], "test-runner")
        self.assertNotIn("github", json.dumps(sidecar["origin"]))
        declared = upload.submit(self.root, self.file(), value(
            "sub-2", origin_system="drive", origin_locator="files/123", origin_version="7"))
        self.assertEqual(declared["origin"]["kind"], "declared-remote")
        self.assertEqual(declared["origin"]["version"], "7")
        self.assertEqual(declared["identity_basis"], "declared-origin")
        self.assertCode("invalid_origin", upload.validate_descriptor, {
            **value(), "origin": {"kind": "operator-supplied", "system": "drive",
                                  "locator": None, "version": None}})

    def test_equal_bytes_from_distinct_origins_remain_distinct(self):
        first = upload.submit(self.root, self.file(), value("sub-a", origin_system="drive", origin_locator="a"))
        second = upload.submit(self.root, self.file(), value("sub-b", origin_system="mail", origin_locator="b"))
        self.assertEqual(first["original"]["sha256"], second["original"]["sha256"])
        self.assertNotEqual(first["records"]["source_id"], second["records"]["source_id"])
        self.assertNotEqual(first["records"]["receipt_id"], second["records"]["receipt_id"])
        self.assertNotEqual(first["original"]["path"], second["original"]["path"])

    def test_exact_retry_returns_existing_acknowledgment(self):
        first = upload.submit(self.root, self.file(), value())
        revision = catalog.status(self.root)["revision"]
        again = upload.submit(self.root, self.file(), value())
        self.assertTrue(again["existing"])
        self.assertEqual({**again, "existing": False}, first)
        self.assertEqual(catalog.status(self.root)["revision"], revision)

    def test_changed_bytes_or_descriptor_conflict_and_correction_links(self):
        first = upload.submit(self.root, self.file(), value())
        self.assertCode("submission_conflict", upload.submit, self.root, self.file(raw=b"Changed.\n"), value())
        self.assertCode("submission_conflict", upload.submit, self.root, self.file(),
                        value(origin_system="drive", origin_locator="x"))
        self.assertEqual((self.root / first["original"]["path"]).read_bytes(), TEXT)
        fixed = upload.submit(self.root, self.file(raw=b"Changed.\n"), value("sub-1-fix", correction_of="sub-1"))
        self.assertEqual(fixed["correction_of"], "sub-1")
        self.assertEqual(fixed["records"]["source_id"], first["records"]["source_id"])
        relation = self.records()[fixed["records"]["relationship_id"]]["data"]
        self.assertEqual((relation["from_id"], relation["to_id"], relation["relation"]),
                         (fixed["records"]["artifact_id"], first["records"]["artifact_id"], "version-of"))
        self.assertEqual(store.inspect(self.root, "upload-sub-1-fix")["correction_of"], "upload-sub-1")
        self.assertCode("unknown_correction_parent", upload.submit, self.root, self.file(),
                        value("sub-3", correction_of="missing"))

    def test_expected_hash_mismatch_is_refused(self):
        self.assertCode("changed_input", upload.submit, self.root, self.file(), value(expected_sha256="0" * 64))


class RefusalTests(UploadCase):
    def assertNothingStaged(self):
        self.assertEqual(store.list_requests(self.root), [])
        self.assertFalse((self.root / "sources").exists())

    def test_traversal_symlink_and_nonregular_paths_are_refused(self):
        target = self.file()
        link = self.outside / "link.txt"
        link.symlink_to(target)
        self.assertCode("unsafe_path", upload.submit, self.root, link, value())
        linked_dir = Path(self.temp.name) / "linked"
        linked_dir.symlink_to(self.outside)
        self.assertCode("unsafe_path", upload.submit, self.root, linked_dir / "notes.txt", value())
        self.assertCode("unsafe_path", upload.submit, self.root, self.outside / ".." / "outside" / "notes.txt", value())
        self.assertCode("not_regular_file", upload.submit, self.root, self.outside, value())
        fifo = self.outside / "pipe.txt"
        os.mkfifo(fifo)
        self.assertCode("not_regular_file", upload.submit, self.root, fifo, value())
        self.assertCode("missing_input", upload.submit, self.root, self.outside / "absent.txt", value())
        for name in ("../escape.txt", "sub/notes.txt", ".hidden.txt", "submission.json"):
            self.assertCode("invalid_name", value, name=name)
        self.assertNothingStaged()

    def test_unsupported_and_mismatched_types_are_refused(self):
        self.assertCode("unsupported_type", value, name="memo.docx", fmt="docx")
        self.assertCode("unsupported_type", value, name="memo.pdf", fmt="text")
        self.assertCode("type_mismatch", upload.submit, self.root, self.file("a.pdf", b"not a pdf"),
                        value(name="a.pdf", fmt="pdf"))
        self.assertCode("type_mismatch", upload.submit, self.root, self.file("a.png", b"\xff\xd8\xff"),
                        value(name="a.png", fmt="image"))
        self.assertCode("type_mismatch", upload.submit, self.root, self.file("a.txt", b"\xff\xfe"), value(name="a.txt"))
        self.assertCode("type_mismatch", upload.submit, self.root, self.file("a.odt", b"PK"),
                        value(name="a.odt", fmt="odt"))
        self.assertCode("unsupported_type", upload.submit, self.root, self.file("e.txt", b""), value(name="e.txt"))
        self.assertNothingStaged()
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as z:
            z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        ack = upload.submit(self.root, self.file("ok.odt", buffer.getvalue()), value(name="ok.odt", fmt="odt"))
        self.assertEqual(ack["original"]["format"], "odt")
        ack = upload.submit(self.root, self.file("ok.pdf", b"%PDF-1.4\n"), value("sub-2", name="ok.pdf", fmt="pdf"))
        self.assertEqual(ack["receipt"]["state"], "committed")

    def test_file_mutated_between_inspection_and_read_is_refused(self):
        path = self.file()
        real_read = upload.read_stream

        def mutate(stream, size):
            data = real_read(stream, size)
            with open(path, "ab") as other:
                other.write(b"appended")
            return data
        with patch.object(upload, "read_stream", mutate):
            self.assertCode("changed_input", upload.submit, self.root, path, value())
        self.assertNothingStaged()

    def test_missing_or_unmarked_drop_is_never_success(self):
        self.assertCode("drop_unavailable", upload.drop_scan, self.outside / "unmounted")
        self.assertCode("drop_unavailable", upload.drop_scan, self.outside)
        self.assertCode("drop_unavailable", upload.drop_submit, self.root, self.outside)
        self.assertNothingStaged()


class RecoveryTests(UploadCase):
    def test_interrupted_copy_leaves_no_retained_file_and_retry_completes(self):
        def broken(stream, raw):
            stream.write(raw[:3])
            raise OSError("disk interrupted")
        with patch.object(upload, "write_all", broken), self.assertRaises(OSError):
            upload.submit(self.root, self.file(), value())
        directory = self.root / "sources/intake/sub-1"
        self.assertEqual(list(directory.iterdir()), [])
        self.assertEqual(store.inspect(self.root, "upload-sub-1")["stages"][0]["status"], "staged")
        self.assertEqual(self.records(), {})
        ack = upload.submit(self.root, self.file(), value())
        self.assertTrue(ack["existing"])
        self.assertEqual(ack["receipt"]["state"], "committed")
        self.assertEqual((self.root / ack["original"]["path"]).read_bytes(), TEXT)

    def test_restart_around_commit_recovers_without_duplicate(self):
        real = catalog.submit

        def crash_after(*args, **kwargs):
            real(*args, **kwargs)
            raise SystemExit("crash after commit")
        with patch.object(catalog, "submit", crash_after), self.assertRaises(SystemExit):
            upload.submit(self.root, self.file(), value())
        self.assertEqual(store.inspect(self.root, "upload-sub-1")["stages"][0]["status"], "running")
        revision = catalog.status(self.root)["revision"]
        ack = upload.submit(self.root, self.file(), value())
        self.assertEqual(ack["receipt"]["state"], "committed")
        self.assertEqual(catalog.status(self.root)["revision"], revision)
        self.assertEqual(len(store.inspect(self.root, "upload-sub-1")["stages"][0]["attempts"]), 1)

    def test_restart_before_commit_completes_on_retry(self):
        with patch.object(runner, "run", side_effect=SystemExit("crash before run")), self.assertRaises(SystemExit):
            upload.submit(self.root, self.file(), value())
        self.assertEqual(self.records(), {})
        ack = upload.submit(self.root, self.file(), value())
        self.assertEqual(ack["receipt"]["state"], "committed")

    def test_duplicate_concurrent_submissions_produce_one_receipt(self):
        path = self.file()
        results, errors = [], []

        def worker():
            try:
                results.append(upload.submit(self.root, path, value()))
            except Exception as error:  # pragma: no cover - surfaced below
                errors.append(error)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(r["existing"] for r in results), [False, True, True, True])
        self.assertEqual(len({r["records"]["receipt_id"] for r in results}), 1)
        receipts = [r for r in self.records().values() if r["kind"] == "receipt"]
        self.assertEqual(len(receipts), 1)

    def test_blocked_receipt_is_reported_without_claiming_success(self):
        catalog.submit(self.root, [{"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}}],
                       actor="test-runner", request_id="other", reason="Advance state",
                       expected_state=catalog.status(self.root)["state_token"])
        with patch.object(upload, "catalog_view", return_value=({"state_token": "0" * 64}, {"S1"})):
            ack = upload.submit(self.root, self.file(), value())
        self.assertEqual(ack["receipt"]["state"], "blocked")
        self.assertIn("code", ack["receipt"])
        self.assertFalse(ack["processing"]["qualified"])


class DropTests(UploadCase):
    def drop(self):
        drop = self.outside / "drop"
        drop.mkdir()
        (drop / upload.DROP_MARKER).write_text("")
        return drop

    def describe(self, drop, name, raw, sid):
        (drop / name).write_bytes(raw)
        described = value(sid, name=name)
        (drop / (name + upload.DESCRIPTOR_SUFFIX)).write_text(json.dumps(described))

    def test_scan_is_discovery_and_submit_is_explicit(self):
        drop = self.drop()
        self.describe(drop, "a.txt", TEXT, "drop-a")
        (drop / "b.txt").write_bytes(TEXT)
        (drop / "c.txt").symlink_to(drop / "a.txt")
        before = sorted(p.name for p in drop.iterdir())
        scan = upload.drop_scan(drop)
        self.assertEqual([(e["name"], e["state"]) for e in scan["entries"]],
                         [("a.txt", "ready"), ("b.txt", "refused"), ("c.txt", "refused")])
        self.assertEqual(scan["entries"][1]["code"], "missing_descriptor")
        self.assertEqual(store.list_requests(self.root), [])
        result = upload.drop_submit(self.root, drop)
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(result["entries"][0]["acknowledgment"]["receipt"]["state"], "committed")
        self.assertEqual(sorted(p.name for p in drop.iterdir()), before)
        again = upload.drop_submit(self.root, drop)
        self.assertTrue(again["entries"][0]["acknowledgment"]["existing"])

    def test_cli_submit_and_drop_commands(self):
        path = self.file()
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = cli.main(["--root", str(self.root), "submit-file", str(path), "--submission-id", "cli-1",
                             "--format", "text", "--actor", "test-runner", "--reason", "CLI test"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["receipt"]["state"], "committed")
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = cli.main(["--root", str(self.root), "drop-scan", "--drop", str(self.outside / "missing")])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue()), {"error": "drop_unavailable"})


if __name__ == "__main__":
    unittest.main()
