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
from tools.intake.contracts import IntakeError

CONFLUENCE = (b"MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=\"----=_Part_1\"\r\n\r\n"
              b"------=_Part_1\r\nContent-Type: text/html; charset=utf-8\r\n"
              b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
              b"<html><body><p class=3D\"x\">Fund codes are four=\r\n characters</p></body></html>\r\n"
              b"------=_Part_1--\r\n")
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
            "a/FALCON+\u2014+ORR Checklist.doc": CONFLUENCE,
            "a/broken.doc": b"MIME-Version: 1.0\r\nbody\r\n",
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
            "a/FALCON+\u2014+ORR Checklist.doc": ("admit", "mhtml"),
            "a/broken.doc": ("needs-reader", None),
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

    def test_missing_or_unlistable_inbox_fails_instead_of_counting_nothing(self):
        from tools.intake.contracts import IntakeError
        with self.assertRaises(IntakeError) as caught:
            cohort.classify(self.root, Path(self.temp.name) / "absent")
        self.assertEqual(caught.exception.code, "inbox_unavailable")
        (self.inbox / "a").chmod(0o000)
        self.addCleanup((self.inbox / "a").chmod, 0o555)
        if os.access(self.inbox / "a", os.R_OK):
            self.skipTest("running with permission override")
        with self.assertRaises(IntakeError) as caught:
            cohort.classify(self.root, self.inbox)
        self.assertEqual(caught.exception.code, "inbox_unavailable")

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
        self.assertEqual(report["counts"]["files"], 15)
        self.assertEqual(report["counts"]["disposition"], {
            "admit": 6, "skipped-artifact": 1, "needs-runtime": 1, "needs-reader": 2,
            "unreadable": 1, "empty": 1, "skipped-asset": 1, "archive-expanded": 1,
            "unsafe-path": 1})
        self.assertEqual(report["counts"]["outcome"], {
            "received": 6, "extracted": 6, "failed": 0, "changed-since-intake": 0,
            "not-processed": 0})
        self.assertEqual(report["counts"]["format"], {"text": 3, "mhtml": 1, "gemini-txt": 1, "odt": 1})
        raw = json.dumps(report)
        for body in ("Fund codes", "=3D", "<p"):
            self.assertNotIn(body, raw)
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

    def test_mime_doc_is_extracted_as_converted_text_and_malformed_mime_is_not_ingested(self):
        report = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
        broken = next(e for e in report["entries"] if e["relative"] == "a/broken.doc")
        self.assertEqual((broken["disposition"], broken["code"]), ("needs-reader", "mime-malformed"))
        self.assertNotIn("outcome", broken)
        entry = next(e for e in report["entries"] if e["relative"].endswith("ORR Checklist.doc"))
        payload = json.loads((self.root / entry["rendition"]).read_text())["payload"]
        self.assertEqual(payload["format"], "mhtml")
        self.assertEqual(payload["method"]["name"], "mhtml-html-text")
        self.assertEqual([r["text"] for r in payload["lines"]], ["Fund codes are four characters\n"])

    def test_text_entry_from_before_mime_recognition_is_corrected_not_kept(self):
        from tools.intake import upload
        old = cohort.submission("a/FALCON+\u2014+ORR Checklist.doc", "inbox")
        upload.submit(self.root, self.inbox / "a/FALCON+\u2014+ORR Checklist.doc", upload.descriptor(
            submission_id=old["submission_id"], format_name="text", name=old["name"],
            actor="cohort-runner", reason="earlier toolkit", origin_kind="local-path",
            origin_system="inbox", origin_locator=old["locator"]))
        report = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
        entry = next(e for e in report["entries"] if e["relative"].endswith("ORR Checklist.doc"))
        self.assertEqual(entry["outcome"], "extracted")
        self.assertNotEqual(entry["submission_id"], old["submission_id"])
        sidecar = json.loads((self.root / Path(entry["original"]).parent / "submission.json").read_text())
        self.assertEqual(sidecar["correction_of"], old["submission_id"])
        again = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
        entry = next(e for e in again["entries"] if e["relative"].endswith("ORR Checklist.doc"))
        self.assertEqual(entry["outcome"], "extracted")

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


class QualifyTests(CohortCase):
    def setUp(self):
        super().setUp()
        from tools.review_events import event_store as events
        events.initialize(self.root)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config/policy.local.json").write_text(
            json.dumps({"audience": "authorized-local"}))
        (self.root / "documents").mkdir(parents=True, exist_ok=True)
        (self.root / "documents/context.md").write_text(
            "# Cohort Context\nQualified Falcon cohort context.\n")

    def _transport(self, incomplete_text=None):
        calls = []

        def fake(model, messages):
            calls.append((model, messages))
            payload = json.loads(messages[-1]["content"])
            records = [r["i"] for r in payload["records"]]
            if incomplete_text and incomplete_text in messages[-1]["content"]:
                records = []
            reply = {
                "decision": "accept",
                "inspected_records": records,
                "inspected_warnings": [w["ref"] for w in payload["warnings"]],
                "contradictions": [], "missing_evidence": [],
                "explanation": "Inspected all rendition records against standing policy.",
            }
            return {"content": json.dumps(reply),
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        return fake, calls

    def test_qualify_reviews_selects_and_publishes_cohort_and_leaves_incomplete_visibly_pending(self):
        from tools.context_index import context_index as index
        transport, calls = self._transport(incomplete_text="Just text")
        report = cohort.qualify(
            self.root, self.inbox, system="falcon-docs-inbox", actor="cohort-runner",
            policy_path="config/standing-policy.local.json",
            manifest="config/selection.local.json",
            model="gpt-5.6-terra", transport=transport,
            context=["documents/context.md"])
        self.assertEqual(len(calls), 6)
        self.assertEqual(report["counts"]["qualification"],
                         {"qualified": 5, "pending-review": 1})
        self.assertEqual(report["counts"]["published"], 1)
        self.assertEqual(report["usage"], {"prompt_tokens": 60, "completion_tokens": 30})
        plain = next(e for e in report["entries"] if e["relative"] == "m/plain.txt")
        self.assertEqual(plain["qualification"], "pending-review")
        stage = store.inspect(
            self.root, "standing-" + plain["submission_id"] + "-" + plain["rendition_sha256"][:12]
            + "-" + hashlib.sha256((self.root / "config/standing-policy.local.json").read_bytes()).hexdigest()[:8]
        )["stages"][0]
        self.assertEqual(stage["status"], "blocked")
        self.assertEqual(stage["attempts"][-1]["code"],
                         "standing-pending-review-insufficient-inspected-scope")

        manifest = self.root / "config/selection.local.json"
        config, _ = index.load_manifest(manifest, self.root)
        self.assertEqual(len(config["renditions"]), 5)
        with index.readonly(self.root / cohort.DEFAULT_INDEX) as db:
            hits = index.search(db, "Fund codes", 10, None, root=self.root)["hits"]
            self.assertEqual(len(hits), 1)
            got = index.packet(db, config, hits[0]["locator"], 60000, root=self.root)
            rendition = got["renditions"][hits[0]["path"]]
            self.assertEqual(rendition["source"]["sha256"],
                             hashlib.sha256(CONFLUENCE).hexdigest())
            self.assertTrue(rendition["rendition_sha256"])
            self.assertTrue(got["pieces"][0]["source_hash"])
            self.assertEqual(got["pieces"][0]["locator"], hits[0]["locator"])
            many = index.packet_many(db, config, [hits[0]["locator"]], 60000, root=self.root)
            self.assertEqual(len(many["requests"]), 1)
            self.assertEqual(index.search(db, "Just text", 10, None, root=self.root)["hits"], [])

    def test_qualify_rerun_is_idempotent_without_extra_model_calls_or_revisions(self):
        from tools.review_events import event_store as events
        transport, calls = self._transport()
        first = cohort.qualify(
            self.root, self.inbox, system="inbox", actor="cohort-runner",
            policy_path="config/standing-policy.local.json",
            manifest="config/selection.local.json",
            model="gpt-5.6-terra", transport=transport,
            context=["documents/context.md"])
        self.assertEqual(first["counts"]["qualification"], {"qualified": 6})
        self.assertEqual(len(calls), 6)
        cat_state = catalog.status(self.root)["state_token"]
        rev_state = events.status(self.root)["state_token"]
        second = cohort.qualify(
            self.root, self.inbox, system="inbox", actor="cohort-runner",
            policy_path="config/standing-policy.local.json",
            manifest="config/selection.local.json",
            model="gpt-5.6-terra", transport=transport,
            context=["documents/context.md"])
        self.assertEqual(len(calls), 6)
        self.assertEqual(second["counts"]["qualification"], {"qualified": 6})
        self.assertEqual(second["usage"], {"prompt_tokens": 0, "completion_tokens": 0})
        self.assertEqual(catalog.status(self.root)["state_token"], cat_state)
        self.assertEqual(events.status(self.root)["state_token"], rev_state)

    def test_modifying_or_adding_one_file_updates_only_affected_item_and_republishes(self):
        from tools.context_index import context_index as index
        transport, calls = self._transport()
        first = cohort.qualify(
            self.root, self.inbox, system="inbox", actor="cohort-runner",
            policy_path="config/standing-policy.local.json",
            manifest="config/selection.local.json",
            model="gpt-5.6-terra", transport=transport,
            context=["documents/context.md"])
        old_notes = next(e for e in first["entries"] if e["relative"] == "a/notes.md")
        calls.clear()

        self.writable()
        (self.inbox / "a/notes.md").write_bytes(b"# Notes\nUpdated cohort line\n")
        (self.inbox / "a/added.md").write_bytes(b"# Added\nFresh cohort item\n")
        for path in [self.inbox, *self.inbox.rglob("*")]:
            if not path.is_symlink():
                path.chmod(0o555 if path.is_dir() else 0o444)

        second = cohort.qualify(
            self.root, self.inbox, system="inbox", actor="cohort-runner",
            policy_path="config/standing-policy.local.json",
            manifest="config/selection.local.json",
            model="gpt-5.6-terra", transport=transport,
            context=["documents/context.md"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(second["counts"]["qualification"], {"qualified": 7})
        new_notes = next(e for e in second["entries"] if e["relative"] == "a/notes.md")
        self.assertNotEqual(new_notes["submission_id"], old_notes["submission_id"])
        self.assertNotEqual(new_notes["receipt_id"], old_notes["receipt_id"])
        sidecar = json.loads(
            (self.root / Path(new_notes["original"]).parent / "submission.json").read_text())
        self.assertEqual(sidecar["correction_of"], old_notes["submission_id"])
        selected = json.loads((self.root / "config/selection.local.json").read_text())["renditions"]
        selected_receipts = {r["receipt_id"] for r in selected}
        self.assertIn(new_notes["receipt_id"], selected_receipts)
        self.assertNotIn(old_notes["receipt_id"], selected_receipts)
        self.assertEqual(len(selected), 7)
        with index.readonly(self.root / cohort.DEFAULT_INDEX) as db:
            self.assertEqual(
                len(index.search(db, "Updated cohort line", 10, None, root=self.root)["hits"]), 1)
            self.assertEqual(
                len(index.search(db, "Fresh cohort item", 10, None, root=self.root)["hits"]), 1)
            notes_hits = index.search(db, "Notes", 10, None, root=self.root)["hits"]
            self.assertEqual([h["path"] for h in notes_hits], [new_notes["rendition"]])

    def _qualify(self, transport, **extra):
        args = dict(system="inbox", actor="cohort-runner",
                    policy_path="config/standing-policy.local.json",
                    manifest="config/selection.local.json", model="gpt-5.6-terra",
                    transport=transport, context=["documents/context.md"])
        args.update(extra)
        return cohort.qualify(self.root, self.inbox, **args)

    def test_qualify_requires_a_maintained_context_before_touching_anything(self):
        transport, calls = self._transport()
        with self.assertRaises(IntakeError) as caught:
            self._qualify(transport, context=None)
        self.assertEqual(caught.exception.code, "context_required")
        self.assertEqual(calls, [])
        self.assertFalse((self.root / "config/standing-policy.local.json").exists())
        self.assertEqual(store.list_requests(self.root), [])

    def test_qualify_refuses_bad_paths_and_unknown_context_before_touching_anything(self):
        transport, calls = self._transport()
        cases = [dict(policy_path="../escape.local.json"), dict(manifest="/tmp/x.json"),
                 dict(manifest="config/s\u00e9lection.local.json"),
                 dict(context=["documents/missing.md"]),
                 dict(context=["documents/context.md#no-such-section"]),
                 dict(context=["config/policy.local.json"])]
        for extra in cases:
            with self.subTest(extra), self.assertRaises(IntakeError):
                self._qualify(transport, **extra)
        self.assertEqual(calls, [])
        self.assertEqual(store.list_requests(self.root), [])
        self.assertFalse((self.root / "config/standing-policy.local.json").exists())
        self.assertFalse((self.root.parent / "escape.local.json").exists())

    def test_qualify_preflights_policy_permissions_and_index_before_ingesting(self):
        transport, calls = self._transport()
        policy_file = self.root / "config/standing-policy.local.json"
        self._qualify(transport)
        calls.clear()
        before = catalog.status(self.root)["state_token"]
        policy_file.chmod(0o644)
        with self.assertRaises(IntakeError):
            self._qualify(transport)
        policy_file.chmod(0o600)
        with self.assertRaises(IntakeError):
            self._qualify(transport, index_path="/tmp/escape.sqlite3")
        with self.assertRaises(IntakeError):
            self._qualify(transport, model="other-model")
        self.assertEqual(calls, [])
        self.assertEqual(catalog.status(self.root)["state_token"], before)

    def test_superseded_review_is_not_resurrected_by_a_later_qualify(self):
        from copy import deepcopy
        from tools.review_events import event_reader, event_store as events
        transport, calls = self._transport()
        report = self._qualify(transport)
        notes = next(e for e in report["entries"] if e["relative"] == "a/notes.md")
        snap = event_reader.read_snapshot(self.root)
        old = next(e for e in snap.events if e["event_id"] == notes["review_event_id"])
        newer = deepcopy(old)
        newer.update(event_id="rev-operator-correction", supersedes=[old["event_id"]],
                     correction_reason="Operator corrected this standing review.",
                     recorded_at="2099-01-01T00:00:00+00:00")
        state = events.status(self.root)
        events.append(self.root, events.bundle([newer]), old["reviewer"], "req-operator-fix",
                      state["revision"], state["state_token"])
        calls.clear()
        again = self._qualify(transport)
        self.assertEqual(calls, [])
        entry = next(e for e in again["entries"] if e["relative"] == "a/notes.md")
        self.assertEqual(entry["qualification"], "pending-review")
        self.assertEqual(entry["review_code"], "review-superseded")
        selected = json.loads((self.root / "config/selection.local.json").read_text())["renditions"]
        self.assertNotIn(notes["review_event_id"], [e for r in selected for e in r["review_event_ids"]])

    def test_changed_standing_policy_does_not_report_old_evidence_as_qualified(self):
        transport, _ = self._transport()
        self._qualify(transport)
        policy_file = self.root / "config/standing-policy.local.json"
        raw = json.loads(policy_file.read_text())
        raw["version"] = 2
        policy_file.write_text(json.dumps(raw))
        report = self._qualify(transport)
        self.assertNotIn("qualified", report["counts"]["qualification"])
        self.assertEqual(report["counts"]["published"], 1)

    def test_qualify_restores_a_selection_missing_from_the_manifest(self):
        transport, calls = self._transport()
        self._qualify(transport)
        manifest = self.root / "config/selection.local.json"
        raw = json.loads(manifest.read_text())
        dropped = raw["renditions"].pop()
        manifest.write_text(json.dumps(raw))
        report = self._qualify(transport)
        self.assertEqual(len(calls), 6)
        self.assertEqual(report["counts"]["qualification"], {"qualified": 6})
        restored = json.loads(manifest.read_text())["renditions"]
        self.assertIn(dropped, restored)
        self.assertEqual(len(restored), 6)
        self.assertEqual([p.name for p in manifest.parent.iterdir()
                          if p.name.startswith(".")], [])

    def test_a_new_rendition_of_the_same_submission_is_reviewed_and_selected_again(self):
        from unittest.mock import patch
        from tools.extraction import mhtml
        transport, calls = self._transport()
        first = self._qualify(transport)
        old = next(e for e in first["entries"] if e["relative"].endswith("ORR Checklist.doc"))
        calls.clear()
        from tools.extraction import extract
        real = extract.fingerprint
        with patch.object(mhtml, "METHOD_VERSION", mhtml.METHOD_VERSION + 1), \
                patch.object(extract, "fingerprint", lambda r, f: real(r, f)[::-1]):
            second = self._qualify(transport)
        new = next(e for e in second["entries"] if e["relative"].endswith("ORR Checklist.doc"))
        self.assertNotEqual(new["rendition"], old["rendition"])
        self.assertEqual(len(calls), 1)
        paths = [r["path"] for r in
                 json.loads((self.root / "config/selection.local.json").read_text())["renditions"]]
        self.assertIn(new["rendition"], paths)
        self.assertNotIn(old["rendition"], paths)
        self.assertEqual(len(paths), 6)

    def test_concurrent_reviews_match_serial_outcomes_regardless_of_completion_order(self):
        import random
        import shutil
        import threading
        import time
        from collections import Counter
        from tools.review_events import event_reader
        base, lock = self._transport(incomplete_text="Just text")[0], threading.Lock()

        def run(concurrency):
            started, finished = [], []

            def transport(model, messages):
                with lock:
                    started.append(messages[-1]["content"])
                    order = len(started)
                    transport.retries["http-429"] += 1
                # Earlier calls finish later, so completion order differs from input order.
                time.sleep(0.02 * (8 - order) + random.uniform(0, 0.01))
                with lock:
                    finished.append(messages[-1]["content"])
                return base(model, messages)
            transport.retries = Counter()
            root = self.root
            self.root = Path(self.temp.name) / ("instance-%d" % concurrency)
            shutil.copytree(root, self.root)
            report = self._qualify(transport, concurrency=concurrency)
            ledger = {r["correlation_id"]: [(st["id"], st["status"]) for st in
                                            store.inspect(self.root, r["correlation_id"])["stages"]]
                      for r in store.list_requests(self.root)}
            events = sorted(e["event_id"] for e in event_reader.read_snapshot(self.root).events)
            manifest = (self.root / "config/selection.local.json").read_text()
            self.root = root
            return report, ledger, events, manifest, started, finished

        serial, parallel = run(1), run(6)
        self.assertEqual(parallel[:4], serial[:4])
        self.assertEqual(serial[0]["usage"], {"prompt_tokens": 60, "completion_tokens": 30})
        self.assertEqual(serial[0]["retries"], {"http-429": 6})
        self.assertEqual(serial[0]["counts"]["qualification"],
                         {"qualified": 5, "pending-review": 1})
        self.assertEqual(serial[4], serial[5])
        self.assertNotEqual(parallel[4], parallel[5])
        with self.assertRaises(IntakeError):
            self._qualify(base, concurrency=0)

        # A review staged by another writer after the prefetch decision is not staged
        # again: the prefetched reply is discarded and only its tokens are counted.
        from unittest.mock import patch
        from tools.review_events import event_store
        self.root = Path(self.temp.name) / "instance-6"
        state = event_store.status(self.root)["state_token"]
        with patch.object(cohort.store, "list_requests", return_value=[]):
            again = self._qualify(base, concurrency=6)
        self.assertEqual(again["counts"], serial[0]["counts"])
        self.assertEqual(again["usage"], {"prompt_tokens": 60, "completion_tokens": 30})
        self.assertEqual(event_store.status(self.root)["state_token"], state)

    def test_memoised_store_validation_still_refuses_changed_content(self):
        from copy import deepcopy
        import sqlite3
        from tools.review_events import event_reader, event_store as events
        from tools.review_events.review_events import ReviewError
        transport, _ = self._transport()
        self._qualify(transport)
        # Every event is now known valid; a changed event or envelope is still refused.
        known = event_reader.read_snapshot(self.root).events
        events.validate_history(known, self.root)
        changed = deepcopy(known)
        changed[-1]["unexpected"] = True
        with self.assertRaises(ReviewError):
            events.validate_history(changed, self.root)
        with self.assertRaises(ReviewError):
            events.contract.schema_validate({**events.bundle(known, self.root), "authority": "x"},
                                            events.store_schema(self.root), root=self.root)
        # A tampered ledger artifact or request is refused although its peers were memoised.
        db = self.root / ".local/intake/intake.sqlite3"
        store.list_requests(self.root)
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE stages SET evidence=evidence||' ' WHERE rowid="
                               "(SELECT max(rowid) FROM stages WHERE evidence IS NOT NULL)")
        with self.assertRaises(IntakeError) as caught:
            store.list_requests(self.root)
        self.assertEqual(caught.exception.code, "corrupt_artifact")
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE stages SET evidence=substr(evidence,1,length(evidence)-1) "
                               "WHERE rowid=(SELECT max(rowid) FROM stages WHERE evidence IS NOT NULL)")
            connection.execute("UPDATE stages SET evidence=NULL, evidence_sha256=NULL "
                               "WHERE rowid=(SELECT max(rowid) FROM stages WHERE status='succeeded')")
        with self.assertRaises(IntakeError) as caught:
            store.list_requests(self.root)
        self.assertEqual(caught.exception.code, "corrupt_stage")

    def test_qualify_republishes_when_the_index_is_missing(self):
        from tools.context_index import context_index as index
        transport, _ = self._transport()
        self._qualify(transport)
        (self.root / cohort.DEFAULT_INDEX).unlink()
        report = self._qualify(transport)
        self.assertEqual(report["counts"]["published"], 1)
        with index.readonly(self.root / cohort.DEFAULT_INDEX) as db:
            self.assertEqual(
                len(index.search(db, "Fund codes", 10, None, root=self.root)["hits"]), 1)


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
        self.assertEqual(len(json.loads(report.read_text())["entries"]), 15)

    def test_cli_qualify_prints_body_free_counts_and_usage(self):
        import contextlib
        from unittest.mock import patch
        from tools.intake import cohort as module, model_review
        from tools.review_events import event_store as events
        events.initialize(self.root)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config/policy.local.json").write_text(
            json.dumps({"audience": "authorized-local"}))
        (self.root / "documents").mkdir(parents=True, exist_ok=True)
        (self.root / "documents/context.md").write_text("# Context\nBody.\n")

        def fake(model, messages):
            payload = json.loads(messages[-1]["content"])
            return {"content": json.dumps({
                "decision": "accept",
                "inspected_records": [r["i"] for r in payload["records"]],
                "inspected_warnings": [w["ref"] for w in payload["warnings"]],
                "contradictions": [], "missing_evidence": [], "explanation": "Clean.",
            }), "usage": {"prompt_tokens": 5, "completion_tokens": 2}}

        out = io.StringIO()
        report = Path(self.temp.name) / "qualify-report.json"
        models_cfg = Path(self.temp.name) / "models.json"
        models_cfg.write_text("{}")
        with patch.object(model_review, "usai_transport", return_value=fake), \
                contextlib.redirect_stdout(out):
            code = module.main([
                "qualify", "--root", str(self.root), "--inbox", str(self.inbox),
                "--system", "inbox", "--actor", "cohort-runner",
                "--policy", "config/standing-policy.local.json",
                "--manifest", "config/selection.local.json",
                "--models-config", str(models_cfg),
                "--model", "gpt-5.6-terra",
                "--context", "documents/context.md",
                "--report", str(report), "--concurrency", "3",
            ])
        self.assertEqual(code, 0)
        raw = out.getvalue()
        for body in ("Fund codes", "Line", "Decided", "hello there", "Just text", "Plan", "inside"):
            self.assertNotIn(body, raw)
        printed = json.loads(raw)
        self.assertEqual(set(printed), {"counts", "usage"})
        self.assertEqual(printed["counts"]["qualification"], {"qualified": 6})
        self.assertEqual(printed["usage"], {"prompt_tokens": 30, "completion_tokens": 12})
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
