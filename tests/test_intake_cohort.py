"""Read-only local cohort intake: classify, submit and extract with body-free counts."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from tools.catalog import catalog_store as catalog
from tools.intake import cohort, store

GEMINI = b"Summary\nDecided things.\nTranscript\nAlex: hello there\n"


def odt():
    from tools.extraction import extract
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", '<office:document-content xmlns:office="' + extract.NS["office"]
                   + '" xmlns:text="' + extract.NS["text"] + '"><office:body><office:text>'
                   '<text:p>Plan</text:p></office:text></office:body></office:document-content>')
        z.writestr("meta.xml", '<office:document-meta xmlns:office="' + extract.NS["office"]
                   + '"><office:meta/></office:document-meta>')
        z.writestr("META-INF/manifest.xml", "<manifest/>")
    return buffer.getvalue()


def tree(path):
    return {p.relative_to(path).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(),
                                             p.stat().st_mode)
            for p in sorted(path.rglob("*")) if p.is_file()}


class CohortCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)
        self.inbox = Path(self.temp.name) / "inbox"
        files = {
            "a/notes.md": b"# Notes\nLine\n",
            "a/FALCON+\u2014+ORR Checklist.doc": b"MIME-Version: 1.0\r\nbody\r\n",
            "a/FALCON+\u2014+ORR Checklist.doc:Zone.Identifier": b"[ZoneTransfer]\n",
            "m/Sync - Notes by Gemini.txt": GEMINI,
            "m/plain.txt": b"Just text\n",
            "plan.odt": odt(),
            "scan.pdf": b"%PDF-1.7\n",
            "word.docx": b"PK\x03\x04",
            "bad.txt": b"\xff\xfe\x00broken",
            "empty.md": b"",
            "site/spud-out/site/assets/app.js": b"x()",
            "bundle.zip": b"PK\x03\x04",
            "_extracted/bundle/readme.md": b"inside\n",
        }
        for name, raw in files.items():
            path = self.inbox / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        os.symlink(self.inbox / "a/notes.md", self.inbox / "link.md")
        for path in [self.inbox, *self.inbox.rglob("*")]:
            if not path.is_symlink():
                path.chmod(0o555 if path.is_dir() else 0o444)
        self.addCleanup(self.writable)
        self.before = tree(self.inbox)

    def writable(self):
        for path in [self.inbox, *self.inbox.rglob("*")]:
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() else 0o644)


class ClassifyTests(CohortCase):
    def test_every_file_gets_one_explicit_disposition(self):
        entries = {e["relative"]: e for e in cohort.classify(self.root, self.inbox)}
        expected = {
            "a/notes.md": ("admit", "text"),
            "a/FALCON+\u2014+ORR Checklist.doc": ("admit", "text"),
            "a/FALCON+\u2014+ORR Checklist.doc:Zone.Identifier": ("skipped-artifact", None),
            "m/Sync - Notes by Gemini.txt": ("admit", "gemini-txt"),
            "m/plain.txt": ("admit", "text"),
            "plan.odt": ("admit", "odt"),
            "scan.pdf": ("needs-runtime", "pdf"),
            "word.docx": ("needs-reader", None),
            "bad.txt": ("unreadable", "text"),
            "empty.md": ("empty", None),
            "site/spud-out/site/assets/app.js": ("skipped-asset", None),
            "bundle.zip": ("archive-expanded", None),
            "_extracted/bundle/readme.md": ("admit", "text"),
            "link.md": ("unsafe-path", None),
        }
        self.assertEqual({k: (v["disposition"], v["format"]) for k, v in entries.items()}, expected)
        self.assertEqual(tree(self.inbox), self.before)

    def test_over_limit_is_counted_not_dropped(self):
        original = cohort.upload.FORMATS["text"]
        cohort.upload.FORMATS["text"] = (original[0], 4)
        self.addCleanup(cohort.upload.FORMATS.__setitem__, "text", original)
        entries = {e["relative"]: e for e in cohort.classify(self.root, self.inbox)}
        self.assertEqual(entries["a/notes.md"]["disposition"], "over-limit")

    def test_submission_names_and_locators_are_ascii_safe_and_stable(self):
        entry = cohort.submission("a/FALCON+\u2014+ORR Checklist.doc", "inbox")
        self.assertRegex(entry["name"], r"\A[A-Za-z0-9][A-Za-z0-9._-]*\.doc\Z")
        self.assertEqual(entry["locator"], "a/FALCON%2B%E2%80%94%2BORR%20Checklist.doc")
        self.assertRegex(entry["submission_id"], r"\Ainbox-[a-f0-9]{24}\Z")
        self.assertEqual(entry, cohort.submission("a/FALCON+\u2014+ORR Checklist.doc", "inbox"))
        for other in (cohort.submission("a/FALCON+-+ORR Checklist.doc", "inbox"),
                      cohort.submission("a/FALCON+\u2014+ORR Checklist.doc", "other")):
            self.assertNotEqual(entry["submission_id"], other["submission_id"])


class IngestTests(CohortCase):
    def test_ingest_submits_and_extracts_admitted_files_with_body_free_counts(self):
        report = cohort.ingest(self.root, self.inbox, system="falcon-docs-inbox",
                               actor="cohort-runner")
        self.assertEqual(tree(self.inbox), self.before)
        self.assertEqual(report["counts"]["files"], 14)
        self.assertEqual(report["counts"]["disposition"], {
            "admit": 6, "skipped-artifact": 1, "needs-runtime": 1, "needs-reader": 1,
            "unreadable": 1, "empty": 1, "skipped-asset": 1, "archive-expanded": 1,
            "unsafe-path": 1})
        self.assertEqual(report["counts"]["outcome"], {
            "received": 6, "extracted": 6, "failed": 0, "changed-since-intake": 0,
            "not-processed": 0})
        self.assertEqual(report["counts"]["format"], {"text": 4, "gemini-txt": 1, "odt": 1})
        raw = json.dumps(report)
        for body in ("Line", "Decided", "hello there", "Just text", "Plan", "inside"):
            self.assertNotIn(body, raw)
        records = catalog.read_records(self.root, limit=200)["records"]
        renditions = [r for r in records if r["id"].startswith("A-extract-")]
        self.assertEqual(len(renditions), 6)
        self.assertTrue(all(r["data"]["coverage"] == "deferred" for r in renditions))
        entry = next(e for e in report["entries"]
                     if e["relative"] == "a/FALCON+\u2014+ORR Checklist.doc")
        sidecar = json.loads((self.root / Path(entry["original"]).parent / "submission.json")
                             .read_text())
        self.assertEqual(sidecar["origin"], {"kind": "local-path", "system": "falcon-docs-inbox",
                                             "locator": entry["locator"], "version": None})
        self.assertEqual(sidecar["original"]["sha256"],
                         hashlib.sha256((self.inbox / entry["relative"]).read_bytes()).hexdigest())

    def test_rerun_is_idempotent(self):
        first = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
        state = catalog.status(self.root)["state_token"]
        second = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
        self.assertEqual(first["counts"], second["counts"])
        self.assertEqual(catalog.status(self.root)["state_token"], state)

    def test_limit_bounds_the_admitted_files_processed(self):
        report = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner",
                               limit=2)
        self.assertEqual(report["counts"]["outcome"]["received"], 2)
        self.assertEqual(report["counts"]["outcome"]["extracted"], 2)
        self.assertEqual(report["counts"]["outcome"]["not-processed"], 4)

    def test_extraction_failure_is_counted_and_others_continue(self):
        from unittest.mock import patch
        from tools.intake import adapters
        real = adapters.prepare
        calls = []

        def failing(root, stage):
            if stage["operation"] == "extract-prepare" and not calls:
                calls.append(stage)
                raise adapters.StageFailure("blocked", "native-input")
            return real(root, stage)
        with patch.object(adapters, "prepare", side_effect=failing):
            report = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
        self.assertEqual(report["counts"]["outcome"]["failed"], 1)
        self.assertEqual(report["counts"]["outcome"]["extracted"], 5)
        failed = [e for e in report["entries"] if e.get("outcome") == "failed"]
        self.assertEqual([e["code"] for e in failed], ["native-input"])

class CliTests(CohortCase):
    def test_cli_prints_body_free_counts_and_writes_private_report(self):
        import contextlib
        from tools.intake import cohort as module
        out = io.StringIO()
        report = Path(self.temp.name) / "report.json"
        with contextlib.redirect_stdout(out):
            code = module.main(["ingest", "--root", str(self.root), "--inbox", str(self.inbox),
                                "--system", "inbox", "--actor", "cohort-runner",
                                "--report", str(report)])
        self.assertEqual(code, 0)
        printed = json.loads(out.getvalue())
        self.assertEqual(set(printed), {"counts"})
        self.assertEqual(printed["counts"]["outcome"]["extracted"], 6)
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(json.loads(report.read_text())["entries"]), 14)


if __name__ == "__main__":
    unittest.main()
