"""Read-only local cohort intake: classify, submit and extract with body-free counts."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
import unittest.mock
from zipfile import ZipFile

from tools.catalog import catalog_store as catalog
from tools.context_index import renditions
from tools.intake import cohort, standing, store, upload
from tools.intake.contracts import IntakeError, LedgerBusy

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


class QualifyCase(CohortCase):
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

    def _qualify(self, transport, **extra):
        args = dict(system="inbox", actor="cohort-runner",
                    policy_path="config/standing-policy.local.json",
                    manifest="config/selection.local.json", model="gpt-5.6-terra",
                    transport=transport, context=["documents/context.md"])
        inbox = extra.pop("inbox", self.inbox)
        args.update(extra)
        return cohort.qualify(self.root, inbox, **args)


class QualifyTests(QualifyCase):
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
        # A policy-version bump never supersedes the operator's correction of a standing review.
        self._bump_policy_version()
        bumped = self._qualify(transport)
        entry = next(e for e in bumped["entries"] if e["relative"] == "a/notes.md")
        self.assertEqual((entry["qualification"], entry["review_code"]),
                         ("pending-review", "standing-pending-review-conflicting-review"))
        self.assertNotIn("rev-operator-correction", event_reader.read_snapshot(self.root).successors)

    def _bump_policy_version(self):
        policy_file = self.root / "config/standing-policy.local.json"
        raw = json.loads(policy_file.read_text())
        raw["version"] += 1
        policy_file.write_text(json.dumps(raw))

    def test_changed_standing_policy_rereviews_and_supersedes_its_earlier_version(self):
        from datetime import datetime, timezone
        from unittest.mock import patch
        from tools.intake import standing
        from tools.review_events import event_reader
        fixed = datetime.now(timezone.utc).replace(microsecond=0)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed
        transport, calls = self._transport()
        # Both versions are reviewed within the same second.
        clock = patch.object(standing, "datetime", Clock)
        clock.start()
        self.addCleanup(clock.stop)
        self._qualify(transport)
        manifest = self.root / "config/selection.local.json"
        v1 = {e for r in json.loads(manifest.read_text())["renditions"] for e in r["review_event_ids"]}
        self._bump_policy_version()
        report = self._qualify(transport)
        self.assertEqual(len(calls), 12)
        self.assertEqual(report["counts"]["qualification"], {"qualified": 6})
        self.assertEqual(report["counts"]["published"], 1)
        selected = json.loads(manifest.read_text())["renditions"]
        v2 = {e for r in selected for e in r["review_event_ids"]}
        self.assertEqual((len(selected), len(v2), v1 & v2), (6, 6, set()))
        snapshot = event_reader.read_snapshot(self.root)
        self.assertEqual({p: snapshot.successors[p] in v2 for p in v1}, dict.fromkeys(v1, True))
        # Unchanged v2 inputs reuse the v2 reviews instead of conflicting with themselves.
        self.assertEqual(self._qualify(transport)["counts"]["qualification"], {"qualified": 6})
        self.assertEqual(len(calls), 12)

    def test_replayed_review_with_a_stale_pinned_intent_is_restaged_as_a_correction(self):
        from unittest.mock import patch
        from tools.intake import runner, standing
        transport, calls = self._transport()
        self._qualify(transport)
        self._bump_policy_version()
        # Reviews pinned before earlier-version reviews became supersedable.
        with patch.object(standing, "_earlier_standing_review", return_value=False):
            pinned = self._qualify(transport)
        self.assertEqual(pinned["counts"]["qualification"], {"pending-review": 6})
        blocked = [r["correlation_id"] for r in store.list_requests(self.root)
                   if r["correlation_id"].startswith("standing-") and not r["complete"]]
        self.assertEqual(len(blocked), 6)
        # An operator's own correction, under any valid name, is followed and corrected too.
        fix = blocked[0] + "-fix"
        stages = store.request(self.root, blocked[0])["stages"]
        cohort._stage_request(self.root, fix, [
            {**s, "arguments": {**s["arguments"], "request_id": f"req-{fix}-{s['id']}"}}
            for s in stages], correction_of=blocked[0])
        with patch.object(standing, "_earlier_standing_review", return_value=False):
            runner.run(self.root, fix, "review")
        for corr in blocked[1:] + [fix]:
            runner.replay(self.root, corr, "review")
        report = self._qualify(transport)
        self.assertEqual(len(calls), 12)
        self.assertEqual(report["counts"]["qualification"], {"qualified": 6})
        heads = [store.head(self.root, corr) for corr in blocked]
        self.assertEqual({(h["correlation_id"], h["correction_of"], h["complete"]) for h in heads},
                         {(c + "-c1", fix if c == blocked[0] else c, True) for c in blocked})
        # Settled corrections are reused, not corrected again.
        self.assertEqual(self._qualify(transport)["counts"]["qualification"], {"qualified": 6})
        self.assertEqual([store.head(self.root, c)["correlation_id"] for c in blocked],
                         [c + "-c1" for c in blocked])
        self.assertEqual(len(calls), 12)

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
        with patch.object(cohort.store, "correlation_ids", return_value=set()):
            again = self._qualify(base, concurrency=6)
        self.assertEqual(again["counts"], serial[0]["counts"])
        self.assertEqual(again["usage"], {"prompt_tokens": 60, "completion_tokens": 30})
        self.assertEqual(event_store.status(self.root)["state_token"], state)

    def test_review_staged_by_another_writer_during_the_model_call_is_reused(self):
        import threading
        base = self._transport()[0]
        nested, lock = [], threading.Lock()

        def transport(model, messages):
            with lock:
                first = not nested
                nested.append(1)
            if first:
                # Another writer qualifies the whole cohort while this call is in flight.
                self._qualify(base, concurrency=1)
            reply = base(model, messages)
            # A different reply would conflict with the other writer's saved request.
            value = json.loads(reply["content"])
            value["explanation"] = "concurrent writer reply"
            return {**reply, "content": json.dumps(value)}

        report = self._qualify(transport, concurrency=6)
        reference = self._qualify(base, concurrency=1)
        self.assertEqual(report["counts"], reference["counts"])
        self.assertEqual([e for e in report["entries"]
                          if e.get("review_code") == "review-superseded"], [])

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


def _cohort_files(inbox, prefix, count):
    inbox.mkdir(parents=True, exist_ok=True)
    for number in range(count):
        (inbox / f"{prefix}{number}.md").write_text(
            f"# {prefix.upper()} {number}\nSynthetic {prefix} cohort line {number} marker{prefix}{number}\n")
    return inbox


def _qualify_in_process(root, inbox, system):
    """Qualify one cohort in its own process, with a stub transport that always accepts."""
    def transport(model, messages):
        payload = json.loads(messages[-1]["content"])
        return {"content": json.dumps({
            "decision": "accept", "inspected_records": [r["i"] for r in payload["records"]],
            "inspected_warnings": [w["ref"] for w in payload["warnings"]],
            "contradictions": [], "missing_evidence": [], "explanation": "Inspected all records."}),
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    return cohort.qualify(
        Path(root), Path(inbox), system=system, actor="cohort-runner",
        policy_path="config/standing-policy.local.json", manifest="config/selection.local.json",
        model="gpt-5.6-terra", transport=transport, context=["documents/context.md"],
        concurrency=2)["counts"]["qualification"]


class SharedManifestTests(QualifyCase):
    """Several inboxes, one catalog, one selection manifest and one published index."""

    def setUp(self):
        super().setUp()
        self.a = _cohort_files(Path(self.temp.name) / "inbox-a", "a", 3)
        self.b = _cohort_files(Path(self.temp.name) / "inbox-b", "b", 2)
        self.manifest = self.root / "config/selection.local.json"
        self.transport, self.calls = self._transport()

    def qualify(self, name, **extra):
        inbox, system = {"a": (self.a, "system-a"), "b": (self.b, "system-b")}[name]
        return self._qualify(self.transport, inbox=inbox, system=system, **extra)

    def selections(self):
        return json.loads(self.manifest.read_text())["renditions"]

    def owners(self, *reports):
        """Manifest paths by cohort label, from the reports' extracted renditions."""
        owner = {e["rendition"]: label for label, report in reports
                 for e in report["entries"] if "rendition" in e}
        counts = {}
        for item in self.selections():
            counts[owner.get(item["path"], "?")] = counts.get(owner.get(item["path"], "?"), 0) + 1
        return counts

    def search(self, text):
        from tools.context_index import context_index as index
        with index.readonly(self.root / cohort.DEFAULT_INDEX) as db:
            return index.search(db, text, 10, None, root=self.root)["hits"]

    def test_a_b_a_b_keeps_every_cohorts_selections_and_publishes_them_all(self):
        seen = []
        first = self.qualify("a")
        second = self.qualify("b")
        reports = [("a", first), ("b", second)]
        self.assertEqual(self.owners(*reports), {"a": 3, "b": 2})
        self.assertEqual(len(self.calls), 5)
        self.calls.clear()
        # Re-qualifying a cohort finds its saved reviews: no model calls, no tokens.
        for name in ("a", "b"):
            report = self.qualify(name)
            reports.append((name, report))
            self.assertEqual(self.calls, [])
            self.assertEqual(report["usage"], {"prompt_tokens": 0, "completion_tokens": 0})
            self.assertEqual(self.owners(*reports), {"a": 3, "b": 2})
            self.assertEqual(report["counts"]["published"], 1)
            self.assertEqual(report["counts"]["other_cohorts"],
                             {"selections": 2 if name == "a" else 3, "unattributed": 0, "orphaned": 0})
            seen.append(report["counts"]["qualification"])
        self.assertEqual(seen, [{"qualified": 3}, {"qualified": 2}])
        for marker in ("markera0", "markera2", "markerb0", "markerb1"):
            self.assertEqual(len(self.search(marker)), 1, marker)

    def test_a_cohort_pruned_by_an_earlier_toolkit_is_restored_from_saved_reviews(self):
        first, second = self.qualify("a"), self.qualify("b")
        # What the toolkit before decision 0021 left behind after qualifying B: only B's selections.
        raw = json.loads(self.manifest.read_text())
        raw["renditions"] = [r for r in raw["renditions"]
                             if r["receipt_id"] in {e["receipt_id"] for e in second["entries"]
                                                    if "receipt_id" in e}]
        self.manifest.write_text(json.dumps(raw))
        self.assertEqual(len(self.selections()), 2)
        self.calls.clear()
        restored = self.qualify("a")
        self.assertEqual(self.calls, [])
        self.assertEqual(restored["usage"], {"prompt_tokens": 0, "completion_tokens": 0})
        self.assertEqual(self.owners(("a", first), ("b", second)), {"a": 3, "b": 2})
        self.assertEqual(restored["counts"]["published"], 1)
        for marker in ("markera0", "markera2", "markerb0"):
            self.assertEqual(len(self.search(marker)), 1, marker)

    def test_qualifying_one_cohort_leaves_the_other_cohorts_entries_byte_identical(self):
        self.qualify("a")
        before = self.manifest.read_bytes()
        a_entries = self.selections()
        self.qualify("b")
        raw = json.loads(self.manifest.read_text())
        # Cohort A's entries are the same objects, in the same places, ahead of B's additions.
        self.assertEqual(raw["renditions"][:3], a_entries)
        self.assertEqual({k: v for k, v in raw.items() if k != "renditions"},
                         {k: v for k, v in json.loads(before).items() if k != "renditions"})
        mid = self.manifest.read_bytes()
        # Requalifying A changes nothing at all: same bytes, so the published index digest is stable too.
        self.qualify("a")
        self.assertEqual(self.manifest.read_bytes(), mid)
        self.qualify("b")
        self.assertEqual(self.manifest.read_bytes(), mid)

    def test_a_cohort_that_dropped_a_file_loses_only_its_own_selection(self):
        self.qualify("a")
        self.qualify("b")
        (self.b / "b1.md").unlink()
        report = self.qualify("b")
        self.assertEqual(report["counts"]["qualification"], {"qualified": 1})
        self.assertEqual(len(self.selections()), 4)
        self.assertEqual(self.search("markerb1"), [])
        self.assertEqual(len(self.search("markera1")), 1)
        self.assertEqual(len(self.search("markerb0")), 1)

    def test_identical_bytes_under_two_systems_are_two_selections(self):
        (self.b / "same.md").write_bytes((self.a / "a0.md").read_bytes())
        first, second = self.qualify("a"), self.qualify("b")
        a0 = next(e for e in first["entries"] if e["relative"] == "a0.md")
        same = next(e for e in second["entries"] if e["relative"] == "same.md")
        self.assertEqual(a0["sha256"], same["sha256"])
        self.assertNotEqual(a0["rendition"], same["rendition"])
        self.assertEqual(len(self.selections()), 6)
        # Each system's receipt holds its own rendition: B dropping its copy never prunes A's.
        (self.b / "same.md").unlink()
        self.qualify("b")
        paths = {r["path"] for r in self.selections()}
        self.assertIn(a0["rendition"], paths)
        self.assertNotIn(same["rendition"], paths)

    def test_an_entry_that_cannot_be_attributed_to_a_cohort_is_never_removed(self):
        first = self.qualify("a")
        self.qualify("b")
        a0 = next(e for e in first["entries"] if e["relative"] == "a0.md")
        sidecar = self.root / Path(a0["original"]).parent / "submission.json"
        sidecar.write_bytes(sidecar.read_bytes() + b" ")
        report = self.qualify("b")
        # Cohort A's entry, whose submission record no longer matches the catalog pin, is kept and reported.
        self.assertEqual(len(self.selections()), 5)
        self.assertIn(a0["rendition"], {r["path"] for r in self.selections()})
        self.assertEqual(report["counts"]["other_cohorts"],
                         {"selections": 3, "unattributed": 1, "orphaned": 0})

    def test_only_a_declared_local_path_submission_names_a_cohort(self):
        first = self.qualify("a")
        a0 = next(e for e in first["entries"] if e["relative"] == "a0.md")
        records = catalog.read_records_by_receipt(self.root, [a0["receipt_id"]], limit=100)
        scoped = records[a0["receipt_id"]]
        self.assertEqual(cohort._submission_system(self.root, scoped), "system-a")
        manual = Path(self.temp.name) / "manual.txt"
        manual.write_text("Operator supplied\n")
        ack = upload.submit(self.root, manual, upload.descriptor(
            submission_id="manual-one", format_name="text", name="manual.txt",
            actor="operator", reason="Manual upload"))
        scoped = catalog.read_records_by_receipt(
            self.root, [ack["records"]["receipt_id"]], limit=100)[ack["records"]["receipt_id"]]
        self.assertIsNone(cohort._submission_system(self.root, scoped))

    def test_a_selection_whose_receipt_or_rendition_is_gone_is_dropped_and_counted(self):
        self.qualify("a")
        self.qualify("b")
        raw = json.loads(self.manifest.read_text())
        ghost = dict(raw["renditions"][0])
        ghost.update(receipt_id="D90", path=ghost["path"].replace(ghost["receipt_id"], "D90"))
        raw["renditions"].append(ghost)
        self.manifest.write_text(json.dumps(raw))
        report = self.qualify("b")
        self.assertEqual(report["counts"]["other_cohorts"],
                         {"selections": 3, "unattributed": 0, "orphaned": 1})
        self.assertEqual(len(self.selections()), 5)
        self.assertEqual(report["counts"]["published"], 1)

    def test_an_unreadable_catalog_stops_before_the_manifest_is_touched(self):
        self.qualify("a")
        before = self.manifest.read_bytes()
        with unittest.mock.patch.object(
                cohort.catalog, "read_records_by_receipt",
                side_effect=catalog.CatalogError("unavailable")), self.assertRaises(IntakeError) as caught:
            self.qualify("b")
        self.assertEqual(caught.exception.code, "selection_attribution_unavailable")
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_an_interrupted_manifest_write_leaves_the_manifest_as_it_was(self):
        self.qualify("a")
        before = self.manifest.read_bytes()
        real = os.replace

        def fail(source, target, *args, **kwargs):
            if Path(target) == self.manifest:
                raise OSError(5, "simulated crash")
            return real(source, target, *args, **kwargs)
        with unittest.mock.patch.object(os, "replace", fail):
            crashed = self.qualify("b")
        # Never a partial or mixed manifest, and no temporary file left behind: A's entries stay.
        self.assertEqual(crashed["counts"]["qualification"], {"pending-review": 2})
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual([p.name for p in self.manifest.parent.iterdir()
                          if p.name.startswith(".")], [])
        self.calls.clear()
        self.assertEqual(self.qualify("b")["counts"]["qualification"], {"qualified": 2})
        self.assertEqual((len(self.selections()), self.calls), (5, []))

    def test_the_manifest_is_rewritten_while_holding_the_ledger_writer_lock(self):
        self.qualify("a")
        seen = []
        real = cohort._set_manifest_renditions

        def probe(root, manifest, renditions_):
            try:
                with store.locked(root, wait=0):
                    seen.append("free")
            except LedgerBusy:
                seen.append("held")
            return real(root, manifest, renditions_)
        with unittest.mock.patch.object(cohort, "_set_manifest_renditions", probe):
            self.qualify("b")
        self.assertEqual(seen, ["held", "held"])

    def test_capacity_shared_with_other_cohorts_leaves_the_surplus_pending_and_resumable(self):
        self.qualify("a")
        # A cap of four, as enforced by standing.write_manifest itself and by the reconciliation,
        # shared with cohort A's three entries. Policy validation compares the policy's receipt
        # scope with the cap (``len(scope) <= cap``), which the real cap of 5,000 always satisfies.
        class Cap(int):
            def __ge__(self, other):
                return True
        small = types.SimpleNamespace(**{**vars(renditions), "MAX_SELECTIONS": Cap(4)})
        with unittest.mock.patch.object(standing, "renditions", small), \
                unittest.mock.patch.object(cohort, "renditions", small):
            report = self.qualify("b")
            self.assertEqual(len(self.selections()), 4)
            self.assertEqual(report["counts"]["qualification"], {"qualified": 1, "pending-review": 1})
            self.assertEqual([e["review_code"] for e in report["entries"] if e.get("review_code")],
                             ["selection-capacity"])
            # Not a conflict: the select stage was left unrun, not blocked for explicit replay.
            full = next(e for e in report["entries"] if e.get("review_code") == "selection-capacity")
            corr = "standing-%s-%s-%s" % (full["submission_id"], full["rendition_sha256"][:12], hashlib.sha256(
                (self.root / "config/standing-policy.local.json").read_bytes()).hexdigest()[:8])
            self.assertEqual([s["status"] for s in store.inspect(self.root, corr)["stages"]],
                             ["succeeded", "staged"])
            # Cohort A is untouched by B's overflow, and still full.
            self.assertEqual(self.owners(("a", self.qualify("a"))).get("a"), 3)
            self.assertEqual(self.qualify("b")["counts"]["qualification"],
                             {"qualified": 1, "pending-review": 1})
            # A frees a slot (a file leaves its inbox); B's pending rendition is then selected from
            # its saved review: no model call, no manual replay.
            (self.a / "a2.md").unlink()
            self.qualify("a")
            self.assertEqual(len(self.selections()), 3)
            self.calls.clear()
            resumed = self.qualify("b")
        self.assertEqual(resumed["counts"]["qualification"], {"qualified": 2})
        self.assertEqual((self.calls, resumed["usage"]),
                         ([], {"prompt_tokens": 0, "completion_tokens": 0}))
        self.assertEqual(len(self.selections()), 4)

    def test_a_select_blocked_by_a_cohort_that_filled_the_manifest_meanwhile_is_resumed(self):
        self.qualify("a")

        class Cap(int):
            def __ge__(self, other):
                return True
        small = types.SimpleNamespace(**{**vars(renditions), "MAX_SELECTIONS": Cap(3)})
        # Room looked available when the run checked, but another writer took it before the select ran.
        with unittest.mock.patch.object(standing, "renditions", small), \
                unittest.mock.patch.object(cohort, "_selection_room", return_value=True):
            report = self.qualify("b")
        self.assertEqual(report["counts"]["qualification"], {"pending-review": 2})
        self.assertEqual({e["review_code"] for e in report["entries"] if e.get("review_code")},
                         {"selection-capacity"})
        self.assertEqual(len(self.selections()), 3)
        # Blocked, but resumable: the cap is not a conflict, so the next run selects it.
        self.calls.clear()
        resumed = self.qualify("b")
        self.assertEqual(resumed["counts"]["qualification"], {"qualified": 2})
        self.assertEqual((self.calls, len(self.selections())), ([], 5))

    def test_two_processes_qualifying_different_cohorts_do_not_lose_each_others_writes(self):
        import multiprocessing
        context = multiprocessing.get_context("spawn")
        jobs = [(str(self.root), str(self.a), "system-a"), (str(self.root), str(self.b), "system-b")]
        with context.Pool(2) as pool:
            counts = pool.starmap(_qualify_in_process, jobs)
        self.assertEqual(counts, [{"qualified": 3}, {"qualified": 2}])
        self.assertEqual(len(self.selections()), 5)
        # A final pass in this process changes nothing: the interleaved writes were all kept.
        before = self.manifest.read_bytes()
        self.qualify("a"), self.qualify("b")
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_after_a_policy_bump_the_other_cohorts_old_review_blocks_publication_until_requalified(self):
        self.qualify("a")
        self.qualify("b")
        self._bump_policy_version()
        report = self.qualify("a")
        # Fail closed: B's selections still cite the older policy's reviews, so nothing is published.
        self.assertEqual(report["counts"]["qualification"], {"qualified": 3})
        self.assertEqual(report["counts"]["published"], 0)
        self.assertEqual(len(self.selections()), 5)
        self.assertEqual(self.qualify("b")["counts"]["published"], 1)
        self.assertEqual(len(self.selections()), 5)
        for marker in ("markera0", "markerb1"):
            self.assertEqual(len(self.search(marker)), 1, marker)

    def _bump_policy_version(self):
        policy_file = self.root / "config/standing-policy.local.json"
        raw = json.loads(policy_file.read_text())
        raw["version"] += 1
        policy_file.write_text(json.dumps(raw))


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
