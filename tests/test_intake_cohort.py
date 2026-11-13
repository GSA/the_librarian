"""Read-only local cohort intake: classify, submit and extract with body-free counts."""
import contextlib
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
from tools.intake import cohort, model_review, runner, standing, store, upload
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


class BlockedCase(CohortCase):
    """Registrations blocked as a changed extraction identity, as in Kata ap0b."""

    def setUp(self):
        super().setUp()
        self.real = catalog.submit

    def run_ingest(self):
        return cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")

    def block_registrations(self):
        """A lost catalog race recorded as blocked native-input, as before concurrent-writer retries, then a
        toolkit change that moves the extractor identity: the replay blocks as changed identity."""
        def race(root, records, **kwargs):
            if kwargs["request_id"].startswith("extract-"):
                raise catalog.CatalogError("Stale expected state; re-read before writing")
            return self.real(root, records, **kwargs)
        with unittest.mock.patch.object(catalog, "submit", side_effect=race):
            first = self.run_ingest()
        self.assertEqual(first["counts"]["failed_by_code"], {"register/native-input": 6})
        self.upgrade = unittest.mock.patch.object(
            cohort.extract, "fingerprint", lambda root, fmt: "f" * 64)
        self.upgrade.start()
        self.addCleanup(self.upgrade.stop)
        for request in store.list_requests(self.root):
            if request["correlation_id"].startswith("register-"):
                runner.replay(self.root, request["correlation_id"], "register")
                runner.settle(self.root, request["correlation_id"], "register")

    def registers(self):
        return sorted(r["correlation_id"] for r in store.list_requests(self.root)
                      if r["correlation_id"].startswith("register-"))

    def correct(self, parent, name, extra=None):
        """Stage a correction the way an operator script did: same stages, a new name."""
        request = store.request(self.root, parent)
        (stage,) = request["stages"]
        if stage["operation"] == "extract-register":
            stage["arguments"]["expected_state"] = catalog.status(self.root)["state_token"]
        cohort._stage_request(self.root, name, [stage], correction_of=parent)
        return name

    def repair(self, sid_register, suffix="-fix"):
        sid = sid_register.removeprefix("register-")
        self.correct("extract-" + sid, "extract-" + sid + suffix)
        runner.settle(self.root, "extract-" + sid + suffix, "prepare")
        self.correct(sid_register, sid_register + suffix)
        return runner.settle(self.root, sid_register + suffix, "register")


class CorrectionChainTests(BlockedCase):
    """Ingest follows the store's validated correction chain to its head (Kata ap0b)."""

    def test_repaired_registration_is_extracted_with_the_heads_evidence(self):
        self.block_registrations()
        blocked = self.run_ingest()
        self.assertEqual(blocked["counts"]["failed_by_code"],
                         {"register/changed-extraction-identity": 6})
        repaired, left = self.registers()[:-1], self.registers()[-1]
        for register in repaired:
            self.assertEqual(self.repair(register)["stages"][0]["status"], "succeeded")
        report = self.run_ingest()
        self.assertEqual(report["counts"]["outcome"]["extracted"], 5)
        self.assertEqual(report["counts"]["failed_by_code"],
                         {"register/changed-extraction-identity": 1})
        head = store.head(self.root, repaired[0])["stages"][0]["evidence"]
        done = [e for e in report["entries"] if e.get("outcome") == "extracted"]
        self.assertIn((head["rendition"], head["rendition_sha256"]),
                      {(e["rendition"], e["rendition_sha256"]) for e in done})
        (failed,) = [e for e in report["entries"] if e.get("outcome") == "failed"]
        self.assertEqual((failed["status"], failed["category"], failed["code"], failed["attempts"]),
                         ("blocked", "blocked", "changed-extraction-identity", 2))
        self.assertEqual(store.inspect(self.root, left)["stages"][0]["status"], "blocked")

    def test_rerun_after_repair_adds_no_extraction_work(self):
        self.block_registrations()
        for register in self.registers():
            self.repair(register)
        self.run_ingest()
        requests, state = len(store.correlation_ids(self.root)), catalog.status(self.root)["state_token"]
        with unittest.mock.patch.object(runner, "run", wraps=runner.run) as ran:
            again = self.run_ingest()
        self.assertEqual(again["counts"]["outcome"]["extracted"], 6)
        self.assertEqual((len(store.correlation_ids(self.root)), ran.call_count), (requests, 0))
        self.assertEqual(catalog.status(self.root)["state_token"], state)

    def test_a_correction_is_followed_by_the_store_chain_not_by_name(self):
        self.block_registrations()
        register = self.registers()[0]
        self.repair(register, suffix="-whatever-the-operator-chose")
        report = self.run_ingest()
        self.assertEqual(report["counts"]["outcome"]["extracted"], 1)
        # A look-alike request that is not linked by correction_of changes nothing.
        other = self.registers()[1]
        stage = store.request(self.root, other)["stages"][0]
        cohort._stage_request(self.root, other + "-reid1", [stage])
        self.assertEqual(self.run_ingest()["counts"]["outcome"]["extracted"], 1)

    def test_a_blocked_head_is_reported_with_its_own_code_and_a_staged_head_is_settled(self):
        self.block_registrations()
        first, second = self.registers()[:2]
        self.repair(first)
        # A correction that is itself blocked is the head: reported failed, with its own attempts.
        with unittest.mock.patch.object(cohort.extract, "fingerprint", lambda r, f: "e" * 64):
            self.correct(first + "-fix", first + "-again")
            self.assertEqual(runner.settle(self.root, first + "-again", "register")
                             ["stages"][0]["status"], "blocked")
        report = self.run_ingest()
        self.assertEqual(report["counts"]["outcome"]["failed"], 6)
        self.assertEqual(sorted(e["attempts"] for e in report["entries"]
                                if e.get("outcome") == "failed"), [1] + [2] * 5)
        # A staged, never-run correction is settled by ingest and reported as what it became.
        sid = second.removeprefix("register-")
        self.correct("extract-" + sid, "extract-" + sid + "-fix")
        runner.settle(self.root, "extract-" + sid + "-fix", "prepare")
        self.correct(second, second + "-fix")
        self.assertEqual(store.inspect(self.root, second + "-fix")["stages"][0]["status"], "staged")
        report = self.run_ingest()
        self.assertEqual(store.inspect(self.root, second + "-fix")["stages"][0]["status"], "succeeded")
        self.assertEqual(report["counts"]["outcome"]["extracted"], 1)

    def test_a_correction_must_repeat_the_operation_and_receipt(self):
        self.block_registrations()
        register = self.registers()[0]
        other = self.registers()[1]
        stage = store.request(self.root, other)["stages"][0]
        cohort._stage_request(self.root, register + "-wrong", [stage], correction_of=register)
        report = self.run_ingest()
        mismatched = [e for e in report["entries"] if e.get("code") == "correction_mismatch"]
        self.assertEqual(len(mismatched), 1)
        self.assertEqual(report["counts"]["outcome"]["failed"], 6)
        self.assertEqual(report["counts"]["failed_by_code"],
                         {"register/changed-extraction-identity": 5, "register/correction_mismatch": 1})

    def test_an_ambiguous_chain_fails_that_entry_without_aborting_the_run(self):
        self.block_registrations()
        register = self.registers()[0]
        self.correct(register, register + "-one")
        self.correct(register, register + "-two")
        report = self.run_ingest()
        self.assertEqual(report["counts"]["outcome"]["failed"], 6)
        self.assertEqual(report["counts"]["failed_by_code"], {
            "register/changed-extraction-identity": 5, "register/ambiguous_correction": 1})

    def test_single_chain_reports_and_ledger_effects_are_unchanged(self):
        report = self.run_ingest()
        ledger = [(r["correlation_id"], r["request_sha256"], r["correction_of"])
                  for r in store.list_requests(self.root)]
        again = self.run_ingest()
        self.assertEqual(report["counts"], again["counts"])
        self.assertEqual(ledger, [(r["correlation_id"], r["request_sha256"], r["correction_of"])
                                  for r in store.list_requests(self.root)])
        self.assertTrue(all(r["correction_of"] is None for r in store.list_requests(self.root)
                            if r["correlation_id"].startswith(("extract-", "register-"))))


class RecoverIdentityTests(BlockedCase):
    """A supported recovery for registrations blocked as changed extraction identity."""

    def test_dry_run_reports_candidates_and_writes_nothing(self):
        self.block_registrations()
        before = store.correlation_ids(self.root)
        report = cohort.recover_identity(self.root)
        self.assertEqual((report["apply"], report["candidates"]), (False, 6))
        self.assertEqual(report["by_operation_code"], {"extract-register/changed-extraction-identity": 6})
        self.assertEqual(store.correlation_ids(self.root), before)
        self.assertNotIn("results", report)

    def test_apply_corrects_the_pair_and_ingest_then_reports_extracted(self):
        self.block_registrations()
        roots = self.registers()
        applied = cohort.recover_identity(self.root, apply=True)
        self.assertEqual((applied["candidates"], applied["results"]), (6, {"succeeded": 6}))
        report = self.run_ingest()
        self.assertEqual(report["counts"]["outcome"]["extracted"], 6)
        self.assertEqual(report["counts"]["failed_by_code"], {})
        for register in roots:
            head = store.head(self.root, register)
            self.assertEqual((head["correlation_id"], head["correction_of"], head["complete"]),
                             (register + "-c1", register, True))
            self.assertEqual(store.inspect(self.root, register)["stages"][0]["status"], "blocked")
            self.assertEqual(store.head(self.root, "extract-" + register.removeprefix("register-"))
                             ["stages"][0]["status"], "succeeded")
        # Idempotent: nothing is left to recover and a second apply stages nothing.
        count = len(store.correlation_ids(self.root))
        again = cohort.recover_identity(self.root, apply=True)
        self.assertEqual((again["candidates"], len(store.correlation_ids(self.root))), (0, count))

    def test_an_already_repaired_chain_is_not_a_candidate(self):
        self.block_registrations()
        self.repair(self.registers()[0])
        self.assertEqual(cohort.recover_identity(self.root)["candidates"], 5)

    def test_other_blocked_registrations_are_not_candidates(self):
        self.block_registrations()
        sid = self.registers()[0].removeprefix("register-")
        # A registration blocked for another reason (a native-input fault) is left alone.
        with unittest.mock.patch.object(runner, "classify", wraps=runner.classify):
            self.assertEqual(cohort.recover_identity(self.root)["by_operation_code"],
                             {"extract-register/changed-extraction-identity": 6})
        with store.locked(self.root) as c, c:
            c.execute("UPDATE attempts SET code='native-input' WHERE correlation_id=?",
                      ("register-" + sid,))
        self.assertEqual(cohort.recover_identity(self.root)["candidates"], 5)

    def test_a_changed_source_still_blocks_the_corrected_pair(self):
        self.block_registrations()
        register = self.registers()[0]
        receipt = store.request(self.root, register)["stages"][0]["arguments"]["receipt"]
        original = catalog.receipt_metadata(self.root, [receipt])["registrations"][receipt]["artifact"]
        path = self.root / original["data"]["path"]
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b"tampered\n")
        applied = cohort.recover_identity(self.root, apply=True)
        self.assertEqual((applied["results"], applied["skipped"]),
                         ({"succeeded": 5}, {"source-unavailable": 1}))
        report = self.run_ingest()
        self.assertEqual(report["counts"]["outcome"]["extracted"], 5)
        self.assertEqual(sum(report["counts"]["failed_by_code"].values()), 1)
        # The tampered source is refused (still blocked, never corrected, never re-staged).
        self.assertEqual(store.head(self.root, register)["correlation_id"], register)
        count = len(store.correlation_ids(self.root))
        cohort.recover_identity(self.root, apply=True)
        self.assertEqual(len(store.correlation_ids(self.root)), count)

    def test_a_removed_original_is_skipped_and_the_rest_are_still_recovered(self):
        self.block_registrations()
        receipt = store.request(self.root, self.registers()[0])["stages"][0]["arguments"]["receipt"]
        original = catalog.receipt_metadata(self.root, [receipt])["registrations"][receipt]["artifact"]
        (self.root / original["data"]["path"]).unlink()
        report = cohort.recover_identity(self.root)
        self.assertEqual((report["candidates"], report["skipped"]), (5, {"source-unavailable": 1}))
        self.assertEqual(cohort.recover_identity(self.root, apply=True)["results"], {"succeeded": 5})

    def test_a_correction_with_differently_named_stages_is_recovered(self):
        self.block_registrations()
        register = self.registers()[0]
        sid = register.removeprefix("register-")
        fault = catalog.CatalogError("Invalid record")
        renamed = {}
        with unittest.mock.patch.object(cohort.extract, "fingerprint", lambda r, f: "d" * 64):
            for name, parent in (("extract-" + sid + "-fix", "extract-" + sid),
                                 (register + "-fix", register)):
                (stage,) = store.request(self.root, parent)["stages"]
                renamed[name] = {**stage, "id": "step-" + stage["id"]}
                if stage["operation"] == "extract-register":
                    renamed[name]["arguments"] = {
                        **stage["arguments"], "expected_state": catalog.status(self.root)["state_token"]}
                cohort._stage_request(self.root, name, [renamed[name]], correction_of=parent)
                with unittest.mock.patch.object(cohort.adapters.extract.catalog, "submit", side_effect=fault):
                    runner.settle(self.root, name, renamed[name]["id"])
        # The renamed registration is blocked on a fault under one extractor; under today's it is
        # refused as a changed identity (replay keeps the pin), and its stage is not called register.
        runner.replay(self.root, register + "-fix", "step-register")
        self.assertEqual(runner.settle(self.root, register + "-fix", "step-register")
                         ["stages"][0]["attempts"][-1]["code"], "changed-extraction-identity")
        self.assertEqual(cohort.recover_identity(self.root)["candidates"], 6)
        result = cohort.recover_identity(self.root, apply=True)
        self.assertEqual(result["results"], {"succeeded": 6})
        self.assertEqual(self.run_ingest()["counts"]["outcome"]["extracted"], 6)

    def test_a_register_without_a_matching_prepare_is_skipped_not_guessed(self):
        self.block_registrations()
        register = self.registers()[0]
        sid = register.removeprefix("register-")
        stage = store.request(self.root, "extract-" + sid)["stages"][0]
        cohort._stage_request(self.root, "extract-" + sid + "-bad", [
            {**stage, "arguments": {**stage["arguments"], "format_name": "odt"}}],
            correction_of="extract-" + sid)
        report = cohort.recover_identity(self.root)
        self.assertEqual((report["candidates"], report["skipped"]), (5, {"prepare-mismatch": 1}))

    def test_cli_entry_point_reports_body_free_counts(self):
        self.block_registrations()
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cohort.main(["recover-identity", "--root", str(self.root)]), 0)
        self.assertEqual(json.loads(out.getvalue())["candidates"], 6)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cohort.main(["recover-identity", "--root", str(self.root), "--apply"]), 0)
        self.assertEqual(json.loads(out.getvalue())["results"], {"succeeded": 6})


class ReplayTests(CohortCase):
    """Replaying an unchanged inbox reads the ledger once per entry and changes nothing (Kata wrwb)."""

    def ingest(self):
        return cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")

    def test_unchanged_replay_reports_exactly_what_the_first_ingest_did(self):
        first = self.ingest()
        state = catalog.status(self.root)["state_token"]
        self.assertEqual(self.ingest(), first)
        self.assertEqual(catalog.status(self.root)["state_token"], state)

    def test_replay_opens_the_ledger_three_times_per_admitted_entry_and_never_inspects(self):
        # One read of the receipt request and its summary, plus the correction-chain head of the
        # extraction and of the registration (kept: a corrected request must still be followed).
        first = self.ingest()
        admitted = first["counts"]["outcome"]["received"]
        with unittest.mock.patch.object(store, "locked", wraps=store.locked) as locked, \
                unittest.mock.patch.object(store, "inspect", wraps=store.inspect) as inspected:
            self.ingest()
        self.assertEqual(locked.call_count, 3 * admitted)
        inspected.assert_not_called()

    def test_a_file_modified_between_runs_is_still_detected_and_corrected(self):
        first = self.ingest()
        old = next(e for e in first["entries"] if e["relative"] == "a/notes.md")
        self.writable()
        (self.inbox / "a/notes.md").write_bytes(b"# Notes\nA different line\n")
        second = self.ingest()
        new = next(e for e in second["entries"] if e["relative"] == "a/notes.md")
        self.assertEqual(new["outcome"], "extracted")
        self.assertNotEqual(new["submission_id"], old["submission_id"])
        sidecar = json.loads((self.root / Path(new["original"]).parent / "submission.json").read_text())
        self.assertEqual(sidecar["correction_of"], old["submission_id"])
        self.assertEqual(second["counts"]["outcome"], first["counts"]["outcome"])
        self.assertEqual(self.ingest(), second)

    def test_a_file_modified_after_classification_is_changed_since_intake_on_replay(self):
        self.ingest()
        self.writable()
        real = cohort.classify

        def classify_then_change(*args):
            entries = real(*args)
            (self.inbox / "a/notes.md").write_bytes(b"# Notes\nChanged after classification\n")
            return entries
        with unittest.mock.patch.object(cohort, "classify", classify_then_change):
            report = self.ingest()
        entry = next(e for e in report["entries"] if e["relative"] == "a/notes.md")
        self.assertEqual((entry["outcome"], entry["code"]), ("changed-since-intake", "changed_input"))
        self.assertEqual(report["counts"]["outcome"]["changed-since-intake"], 1)

    def test_a_retained_copy_that_no_longer_matches_still_refuses_on_replay(self):
        first = self.ingest()
        entry = next(e for e in first["entries"] if e["relative"] == "a/notes.md")
        retained = self.root / entry["original"]
        retained.chmod(0o600)
        retained.write_bytes(b"# Notes\nTampered\n")
        report = self.ingest()
        again = next(e for e in report["entries"] if e["relative"] == "a/notes.md")
        self.assertEqual((again["outcome"], again["code"]), ("failed", "destination_conflict"))

    def test_only_a_succeeded_stage_is_reused_from_the_entrys_ledger_read(self):
        summary = lambda status: {"stages": [{"status": status}]}
        known = {"done": ({}, summary("succeeded")), "again": ({}, summary("retryable")),
                 "stuck": ({}, summary("blocked")), "going": ({}, summary("running"))}
        self.assertEqual(upload.settled_summary(known, "done"), summary("succeeded"))
        for name in ("again", "stuck", "going", "absent"):
            self.assertIsNone(upload.settled(known, name))
        self.assertIsNone(upload.settled(None, "done"))


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
            # ``throughput`` reports timing and varies by design; everything else must not.
            report = {k: v for k, v in report.items() if k != "throughput"}
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
            self.assertEqual(report["counts"]["selection_capacity"],
                             {"entries": 1, "model_calls_skipped": 1})
            # Cohort A is untouched by B's overflow, and still full.
            self.assertEqual(self.owners(("a", self.qualify("a"))).get("a"), 3)
            self.assertEqual(self.qualify("b")["counts"]["qualification"],
                             {"qualified": 1, "pending-review": 1})
            # A frees a slot (a file leaves its inbox); B's held-back rendition was never reviewed,
            # so it now costs its one review.
            (self.a / "a2.md").unlink()
            self.qualify("a")
            self.assertEqual(len(self.selections()), 3)
            self.calls.clear()
            resumed = self.qualify("b")
        self.assertEqual(resumed["counts"]["qualification"], {"qualified": 2})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.selections()), 4)

    def capped(self, cap):
        """Patch the manifest cap seen by the selector and the cohort run (policy checks stay real)."""
        class Cap(int):
            def __ge__(self, other):
                return True
        small = types.SimpleNamespace(**{**vars(renditions), "MAX_SELECTIONS": Cap(cap)})
        stack = contextlib.ExitStack()
        stack.enter_context(unittest.mock.patch.object(standing, "renditions", small))
        stack.enter_context(unittest.mock.patch.object(cohort, "renditions", small))
        return stack

    def fresh_instance(self):
        """A second empty instance with the same inboxes, and a new counting transport."""
        from tools.review_events import event_store as events
        root = Path(self.temp.name) / "instance-2"
        (root / "config").mkdir(parents=True)
        (root / "documents").mkdir()
        store.initialize(root)
        catalog.initialize(root)
        events.initialize(root)
        (root / "config/policy.local.json").write_text(json.dumps({"audience": "authorized-local"}))
        (root / "documents/context.md").write_text("# Cohort Context\nQualified Falcon cohort context.\n")
        self.root, self.manifest = root, root / "config/selection.local.json"
        self.transport, self.calls = self._transport()

    def check_held_back_entries_cost_no_call_and_resume_with_one_each(self, concurrency):
        self.qualify("a")
        # Cohort B has two files and one free slot (cap 4, three of A's entries).
        self.calls.clear()
        with self.capped(4):
            report = self.qualify("b", concurrency=concurrency)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(report["usage"], {"prompt_tokens": 10, "completion_tokens": 5})
        self.assertEqual(report["counts"]["qualification"], {"qualified": 1, "pending-review": 1})
        self.assertEqual(report["counts"]["selection_capacity"], {"entries": 1, "model_calls_skipped": 1})
        held = next(e for e in report["entries"] if e.get("review_code") == "selection-capacity")
        # Nothing was staged for it, so no review is saved; it is not a conflict or a failure.
        corr = "standing-%s-%s-%s" % (held["submission_id"], held["rendition_sha256"][:12], hashlib.sha256(
            (self.root / "config/standing-policy.local.json").read_bytes()).hexdigest()[:8])
        with self.assertRaises(IntakeError):
            store.inspect(self.root, corr)
        self.assertEqual(len(self.selections()), 4)
        # A rerun at the same capacity stays at zero calls.
        self.calls.clear()
        with self.capped(4):
            again = self.qualify("b", concurrency=concurrency)
        self.assertEqual((self.calls, again["counts"]["selection_capacity"]),
                         ([], {"entries": 1, "model_calls_skipped": 1}))
        # Room appears: the held-back entry costs one review, the selected one none.
        self.calls.clear()
        resumed = self.qualify("b", concurrency=concurrency)
        self.assertEqual(resumed["counts"]["qualification"], {"qualified": 2})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(resumed["counts"]["selection_capacity"], {"entries": 0, "model_calls_skipped": 0})
        self.assertEqual(len(self.selections()), 5)

    def test_entries_that_cannot_be_selected_cost_no_model_call_and_resume_with_one_each(self):
        self.check_held_back_entries_cost_no_call_and_resume_with_one_each(1)

    def test_the_same_holds_with_concurrent_reviews(self):
        self.check_held_back_entries_cost_no_call_and_resume_with_one_each(3)

    def test_the_precheck_changes_only_the_calls_not_the_outcome_of_a_run_that_pays_for_all(self):
        outcome = lambda r: sorted((e["relative"], e["qualification"], e.get("review_code"))
                                   for e in r["entries"] if "qualification" in e)
        self.qualify("a")
        with self.capped(4):
            skipped = self.qualify("b")
        self.assertEqual(len(self.calls), 4)
        self.fresh_instance()
        self.qualify("a")
        with self.capped(4), unittest.mock.patch.object(cohort, "_held_back", return_value=False):
            paid = self.qualify("b")
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(outcome(skipped), outcome(paid))
        self.assertEqual(paid["counts"]["selection_capacity"], {"entries": 1, "model_calls_skipped": 0})

    def test_a_saved_review_is_never_repeated_and_is_selected_when_room_exists(self):
        self.qualify("a")
        # Room looked available, so both of B's reviews were paid and saved; the select was refused.
        with self.capped(3), unittest.mock.patch.object(cohort, "_selection_room", return_value=True):
            self.qualify("b")
        self.assertEqual(len(self.calls), 5)
        self.calls.clear()
        with self.capped(3):
            held = self.qualify("b")
        self.assertEqual((self.calls, held["counts"]["qualification"], held["counts"]["selection_capacity"]),
                         ([], {"pending-review": 2}, {"entries": 2, "model_calls_skipped": 0}))
        resumed = self.qualify("b")
        self.assertEqual((self.calls, resumed["counts"]["qualification"], len(self.selections())),
                         ([], {"qualified": 2}, 5))

    def test_a_full_manifest_does_not_change_the_outcome_of_out_of_scope_entries(self):
        self.qualify("a")
        policy_file = self.root / "config/standing-policy.local.json"
        raw = json.loads(policy_file.read_text())
        raw["version"] += 1
        raw["scope"]["receipts"] = ["D1", "D2", "D3"]  # B's receipts are D4 and D5
        policy_file.write_text(json.dumps(raw))
        policy_file.chmod(0o600)
        self.calls.clear()
        with self.capped(3):
            report = self.qualify("b")
        self.assertEqual(self.calls, [])
        self.assertEqual(report["counts"]["out_of_scope"], {"entries": 2, "model_calls_skipped": 2})
        self.assertEqual(report["counts"]["selection_capacity"], {"entries": 0, "model_calls_skipped": 0})

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


class ThroughputTests(QualifyCase):
    """Adaptive concurrency and range-encoded inspection change cost, never an outcome."""

    def stub(self, ranges=False, throttle_every=0, delay=0.0):
        """Counting transport: calls, tokens (reply characters / 4), simulated 429/503 retries."""
        import threading
        import time
        from collections import Counter
        lock, state = threading.Lock(), {"calls": 0, "completion": 0, "in_flight": 0, "peak": 0}

        def encode(records):
            if not ranges:
                return records
            out, start = [], None
            for i, r in enumerate(records):
                if start is None:
                    start = r
                if i + 1 == len(records) or records[i + 1] != r + 1:
                    out.append(start if start == r else f"{start}-{r}")
                    start = None
            return out

        def transport(model, messages):
            with lock:
                state["calls"] += 1
                number = state["calls"]
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
            payload = json.loads(messages[-1]["content"])
            records = [r["i"] for r in payload["records"]]
            if "Just text" in messages[-1]["content"]:
                records = records[:0]
            if throttle_every and number % throttle_every == 0:
                for observe in tuple(transport.observers):
                    observe("http-429" if number % 2 else "http-503")
                with lock:
                    transport.retries["http-429"] += 1
            time.sleep(delay)
            content = json.dumps({
                "decision": "accept", "inspected_records": encode(records),
                "inspected_warnings": [w["ref"] for w in payload["warnings"]],
                "contradictions": [], "missing_evidence": [],
                "explanation": "Inspected all rendition records against standing policy."})
            with lock:
                state["in_flight"] -= 1
                state["completion"] += len(content) // 4
            return {"content": content, "usage": {"prompt_tokens": len(messages[-1]["content"]) // 4,
                                                  "completion_tokens": len(content) // 4}}
        transport.retries, transport.observers = Counter(), []
        return transport, state

    def run_copy(self, name, transport, **extra):
        import shutil
        from tools.review_events import event_store
        root = self.root
        self.root = Path(self.temp.name) / name
        shutil.copytree(root, self.root)
        try:
            report = self._qualify(transport, **extra)
            ledger = {r["correlation_id"]: [(st["id"], st["status"]) for st in
                                            store.inspect(self.root, r["correlation_id"])["stages"]]
                      for r in store.list_requests(self.root)}
            events = sorted(e["event_id"] for e in
                            __import__("tools.review_events.event_reader", fromlist=["x"])
                            .read_snapshot(self.root).events)
            findings = sorted(hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in (self.root / "documents").parent.rglob("findings/**/*.json"))
            manifest = (self.root / "config/selection.local.json").read_text()
            del event_store
        finally:
            self.root = root
        return report, ledger, events, findings, manifest

    def same(self, a, b):
        self.assertEqual(a[0]["entries"], b[0]["entries"])
        self.assertEqual(a[0]["counts"], b[0]["counts"])
        self.assertEqual(a[1:], b[1:])

    def test_adaptive_run_under_throttling_has_the_outcomes_of_a_fixed_run_and_never_exceeds_the_cap(self):
        fixed_t, fixed = self.stub(delay=0.01)
        fixed_report = self.run_copy("fixed", fixed_t, concurrency=1, adaptive=False)
        adaptive_t, adaptive = self.stub(throttle_every=2, delay=0.01)
        adaptive_report = self.run_copy("adaptive", adaptive_t, concurrency=4)
        self.same(fixed_report, adaptive_report)
        self.assertEqual(fixed_report[0]["usage"], adaptive_report[0]["usage"])
        self.assertEqual((fixed["calls"], adaptive["calls"]), (6, 6))
        self.assertLessEqual(adaptive["peak"], 4)
        throughput = adaptive_report[0]["throughput"]
        self.assertEqual((throughput["cap"], throughput["calls"]), (4, 6))
        self.assertLessEqual(throughput["max_in_flight"], 4)
        self.assertGreater(throughput["backoffs"], 0)
        self.assertLess(throughput["lowest"], 4)
        self.assertEqual(adaptive_report[0]["retries"], {"http-429": 3})

    def test_an_adaptive_run_with_no_throttling_stays_at_the_configured_cap(self):
        transport, state = self.stub(delay=0.02)
        report = self._qualify(transport, concurrency=3)
        throughput = report["throughput"]
        self.assertEqual((throughput["final"], throughput["lowest"], throughput["backoffs"]), (3, 3, 0))
        self.assertLessEqual(state["peak"], 3)
        self.assertGreater(throughput["calls_per_minute"], 0)

    def test_initial_concurrency_starts_below_the_cap_and_is_validated(self):
        transport, state = self.stub(delay=0.01)
        report = self._qualify(transport, concurrency=4, initial_concurrency=1)
        # It starts at one call and earns the rest one clean window at a time.
        self.assertEqual(report["throughput"]["initial"], 1)
        self.assertLessEqual(state["peak"], 4)
        self.assertGreater(report["throughput"]["final"], 1)
        for bad in (0, 5, True):
            with self.assertRaises(IntakeError):
                self._qualify(transport, concurrency=4, initial_concurrency=bad)
        with self.assertRaises(IntakeError):
            self._qualify(transport, inspected_form="sparse")

    def test_range_form_has_identical_entries_findings_events_and_selection_with_fewer_tokens(self):
        listed_t, listed = self.stub()
        ranged_t, ranged = self.stub(ranges=True)
        a = self.run_copy("list", listed_t, concurrency=2)
        b = self.run_copy("ranges", ranged_t, concurrency=2, inspected_form="ranges")
        self.same(a, b)
        self.assertEqual(a[0]["counts"]["qualification"], {"qualified": 5, "pending-review": 1})
        self.assertEqual((listed["calls"], ranged["calls"]), (6, 6))
        self.assertEqual(a[0]["usage"]["prompt_tokens"] <= b[0]["usage"]["prompt_tokens"], True)
        self.assertLessEqual(ranged["completion"], listed["completion"])
        self.assertEqual(b[0]["throughput"]["inspected_form"], "ranges")
        # The ranged request carries the range instruction; the list request does not.
        self.assertIn("first-last", model_review.SYSTEM + model_review.RANGE_NOTE)

    def test_range_replies_are_accepted_whatever_form_was_asked_and_gaps_stay_incomplete(self):
        base, _ = self._transport()

        def gappy(model, messages):
            payload = json.loads(messages[-1]["content"])
            records = [r["i"] for r in payload["records"]]
            gaps = ["0-%d" % (records[-1] - 1)] if len(records) > 1 else []
            reply = base(model, messages)
            value = json.loads(reply["content"])
            value["inspected_records"] = gaps
            return {**reply, "content": json.dumps(value)}
        report = self.run_copy("gappy", gappy, concurrency=2, inspected_form="ranges")[0]
        reference = self.run_copy("whole", base, concurrency=1)[0]
        self.assertEqual(reference["counts"]["qualification"], {"qualified": 6})
        self.assertNotEqual(report["counts"]["qualification"], reference["counts"]["qualification"])

    def test_restart_after_a_throttled_run_reuses_saved_reviews_without_calls(self):
        transport, state = self.stub(throttle_every=2)
        first = self._qualify(transport, concurrency=4)
        again_t, again = self.stub(throttle_every=2)
        second = self._qualify(again_t, concurrency=4, inspected_form="ranges")
        self.assertEqual(again["calls"], 0)
        self.assertEqual(second["counts"]["qualification"], first["counts"]["qualification"])
        self.assertEqual(second["usage"], {"prompt_tokens": 0, "completion_tokens": 0})


class PublishTests(QualifyCase):
    """The standalone ``publish`` command: the end-of-run publication, without a re-run."""

    def setUp(self):
        super().setUp()
        self.manifest = self.root / "config/selection.local.json"
        self.transport, self.calls = self._transport()

    def publish(self, **extra):
        args = dict(policy_path="config/standing-policy.local.json",
                    manifest="config/selection.local.json")
        args.update(extra)
        return cohort.publish(self.root, **args)

    def _bump_policy_version(self):
        policy_file = self.root / "config/standing-policy.local.json"
        raw = json.loads(policy_file.read_text())
        raw["version"] += 1
        policy_file.write_text(json.dumps(raw))

    def interrupted_qualify(self):
        """A qualify that was killed after its reviews and selections, before its publish."""
        with unittest.mock.patch.object(cohort, "_publish", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self._qualify(self.transport)

    def hits(self, text):
        from tools.context_index import context_index as index
        with index.readonly(self.root / cohort.DEFAULT_INDEX) as db:
            return index.search(db, text, 10, None, root=self.root)["hits"]

    def publish_requests(self):
        return [c for c in store.correlation_ids(self.root) if c.startswith("publish-")]

    def test_publish_serves_an_interrupted_runs_selections_without_a_model_call(self):
        self.interrupted_qualify()
        self.assertEqual(len(json.loads(self.manifest.read_text())["renditions"]), 6)
        self.assertFalse((self.root / cohort.DEFAULT_INDEX).exists())
        self.calls.clear()
        report = self.publish()
        self.assertEqual(self.calls, [])
        self.assertEqual(report["counts"], {"published": 1, "selections": 6})
        self.assertEqual(report["publication"], {"status": "succeeded", "available": True})
        self.assertEqual(len(self.hits("Fund codes")), 1)

    def test_publish_matches_the_publication_a_full_qualify_makes(self):
        self.interrupted_qualify()
        self.publish()
        manifest_bytes = self.manifest.read_bytes()
        requests = self.publish_requests()
        # A re-run finds the same manifest and the same published request: nothing is rebuilt.
        report = self._qualify(self.transport)
        self.assertEqual(report["counts"]["published"], 1)
        self.assertEqual(self.manifest.read_bytes(), manifest_bytes)
        self.assertEqual(self.publish_requests(), requests)
        self.assertEqual(len(self.calls), 6)

    def test_publish_is_idempotent_and_restages_only_when_the_index_goes_stale(self):
        self._qualify(self.transport)
        requests = self.publish_requests()
        self.assertEqual(len(requests), 1)
        first = self.publish()
        second = self.publish()
        self.assertEqual((first["counts"]["published"], second["counts"]["published"]), (1, 1))
        self.assertEqual(self.publish_requests(), requests)
        # An index that is no longer fresh is rebuilt under a new revision of the same request.
        (self.root / cohort.DEFAULT_INDEX).unlink()
        self.assertEqual(self.publish()["counts"]["published"], 1)
        self.assertEqual(len(self.publish_requests()), 2)
        self.assertEqual(len(self.hits("Fund codes")), 1)

    def test_publish_keeps_the_standing_checks(self):
        self._qualify(self.transport)
        self._bump_policy_version()
        report = self.publish()
        # The manifest still cites the older policy version's reviews: refused, nothing new served.
        self.assertEqual(report["counts"]["published"], 0)
        self.assertEqual(report["publication"]["status"], "blocked")
        self.assertEqual(report["publication"]["code"], "standing-selection-unauthorized")

    def test_publish_refuses_missing_or_unsafe_inputs_before_staging_anything(self):
        for extra in ({"policy_path": "config/missing.json"}, {"manifest": "config/missing.json"},
                      {"manifest": "../selection.json"}, {"index_path": "../index.sqlite3"}):
            with self.subTest(extra), self.assertRaises(IntakeError):
                self.publish(**extra)
        self.assertEqual(self.publish_requests(), [])

    def test_publish_refuses_a_malformed_manifest_with_a_body_free_error(self):
        self._qualify(self.transport)
        self.manifest.write_text("{not json")
        before = self.publish_requests()
        with self.assertRaises(IntakeError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "preflight_invalid")
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cohort.main(["publish", "--root", str(self.root), "--policy",
                                          "config/standing-policy.local.json",
                                          "--manifest", "config/selection.local.json"]), 2)
        self.assertEqual(json.loads(err.getvalue()), {"error": "preflight_invalid"})
        self.assertEqual(self.publish_requests(), before)

    def test_cli_publish_prints_body_free_counts_and_needs_no_model(self):
        import contextlib
        self.interrupted_qualify()
        out = io.StringIO()
        argv = ["publish", "--root", str(self.root), "--policy", "config/standing-policy.local.json",
                "--manifest", "config/selection.local.json"]
        with contextlib.redirect_stdout(out):
            code = cohort.main(argv)
        self.assertEqual(code, 0)
        printed = json.loads(out.getvalue())
        self.assertEqual(printed["counts"], {"published": 1, "selections": 6})
        for body in ("Fund codes", "Line", "Decided", "hello there"):
            self.assertNotIn(body, out.getvalue())
        self._bump_policy_version()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cohort.main(argv), 1)
        self.assertEqual(json.loads(out.getvalue())["publication"]["code"],
                         "standing-selection-unauthorized")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cohort.main(argv[:-1] + ["config/missing.json"]), 2)
        self.assertIn("error", json.loads(err.getvalue()))

class ScopePrecheckTests(QualifyCase):
    """The standing scope is checked before a paid model call; the adapter stays the authority."""

    def setUp(self):
        super().setUp()
        self.six = _cohort_files(Path(self.temp.name) / "scope-inbox", "a", 6)  # receipts D1..D6
        self.transport, self.calls = self._transport()
        self.policy_file = self.root / "config/standing-policy.local.json"
        self.generation = 0

    def fresh_instance(self):
        """A second empty instance (same layout) and a new counting transport."""
        from tools.review_events import event_store as events
        self.generation += 1
        root = Path(self.temp.name) / f"instance-{self.generation}"
        (root / "config").mkdir(parents=True)
        (root / "documents").mkdir()
        store.initialize(root)
        catalog.initialize(root)
        events.initialize(root)
        (root / "config/policy.local.json").write_text(json.dumps({"audience": "authorized-local"}))
        (root / "documents/context.md").write_text("# Cohort Context\nQualified Falcon cohort context.\n")
        self.root, self.policy_file = root, root / "config/standing-policy.local.json"
        self.transport, self.calls = self._transport()

    def _qualify(self, transport, **extra):
        return super()._qualify(transport, inbox=self.six, **extra)

    def write_policy(self, receipts, version=1, audiences=("source",)):
        self.policy_file.write_text(json.dumps({
            "schema": standing.POLICY_SCHEMA, "policy_id": "hand-written", "version": version,
            "status": "active", "reviewer": "usai:gpt-5.6-terra",
            "scope": {"receipts": list(receipts), "audiences": list(audiences)},
            "budget": {"max_pages": renditions.DOCLING_MAX_PAGES,
                       "max_records": renditions.DOCLING_MAX_RECORDS,
                       "max_findings_bytes": standing.LIMIT},
            "selection": {"context": ["documents/context.md"]}}, indent=2))
        self.policy_file.chmod(0o600)

    def outcomes(self, report):
        return sorted((e["relative"], e["qualification"], e.get("review_code"))
                      for e in report["entries"] if "qualification" in e)

    def test_out_of_scope_entries_cost_no_model_call_and_end_as_before(self):
        self.write_policy(["D1", "D2", "D3"])
        # A fresh instance per run keeps receipts D1..D6 for the same six files.
        for concurrency in (1, 3):
            with self.subTest(concurrency=concurrency):
                self.fresh_instance()
                self.write_policy(["D1", "D2", "D3"])
                report = self._qualify(self.transport, concurrency=concurrency)
                self.assertEqual(len(self.calls), 3)
                self.assertEqual(report["usage"], {"prompt_tokens": 30, "completion_tokens": 15})
                self.assertEqual(report["counts"]["qualification"], {"qualified": 3, "exception": 3})
                self.assertEqual(report["counts"]["out_of_scope"], {"entries": 3, "model_calls_skipped": 3})
                refused = [e for e in report["entries"] if e.get("qualification") == "exception"]
                self.assertEqual({e["review_code"] for e in refused},
                                 {"standing-exception-prohibited-scope"})
                self.assertEqual(report["counts"]["published"], 1)

    def test_outcomes_match_a_run_that_pays_for_every_model_call(self):
        self.write_policy(["D1", "D2", "D3"])
        report = self._qualify(self.transport)
        skipped = self.outcomes(report)
        self.assertEqual(len(self.calls), 3)
        self.fresh_instance()
        self.write_policy(["D1", "D2", "D3"])
        with unittest.mock.patch.object(standing, "refused_scope", return_value=False):
            paid = self._qualify(self.transport)
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(skipped, self.outcomes(paid))
        self.assertEqual(paid["counts"]["out_of_scope"], {"entries": 3, "model_calls_skipped": 0})

    def test_a_skipped_entry_ends_in_the_same_terminal_stage_as_a_paid_one(self):
        self.write_policy(["D1"])
        report = self._qualify(self.transport)
        entry = next(e for e in report["entries"] if e.get("review_code") ==
                     "standing-exception-prohibited-scope")
        sha = hashlib.sha256(self.policy_file.read_bytes()).hexdigest()
        stage = store.inspect(self.root, "standing-" + cohort._standing_token(entry, sha))["stages"][0]
        self.assertEqual(stage["status"], "blocked")
        self.assertEqual(stage["attempts"][-1]["code"], "standing-exception-prohibited-scope")
        self.assertEqual(stage["attempts"][-1]["category"], "blocked")

    def test_rerun_neither_calls_the_model_nor_changes_the_outcome(self):
        self.write_policy(["D1", "D2", "D3"])
        first = self._qualify(self.transport)
        self.calls.clear()
        second = self._qualify(self.transport)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.outcomes(first), self.outcomes(second))
        self.assertEqual(second["usage"], {"prompt_tokens": 0, "completion_tokens": 0})

    def test_the_adapter_still_refuses_an_out_of_scope_request_staged_without_the_precheck(self):
        self.write_policy(["D1"])
        with unittest.mock.patch.object(standing, "refused_scope", return_value=False):
            report = self._qualify(self.transport)
        self.assertEqual(report["counts"]["qualification"], {"qualified": 1, "exception": 5})
        self.assertEqual(len(self.calls), 6)

    def test_widening_scope_requires_a_new_version_and_costs_one_review_per_entry(self):
        self.write_policy(["D1", "D2", "D3"])
        first = self._qualify(self.transport)
        self.assertEqual(first["counts"]["qualification"], {"qualified": 3, "exception": 3})
        self.calls.clear()
        self.write_policy([f"D{i}" for i in range(1, 7)], version=2)
        widened = self._qualify(self.transport)
        # The policy hash is part of every standing request, so every entry is reviewed once
        # under the new policy; the three refused ones were never reviewed before.
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(widened["counts"]["qualification"], {"qualified": 6})
        self.assertEqual(widened["counts"]["out_of_scope"], {"entries": 0, "model_calls_skipped": 0})
        self.calls.clear()
        again = self._qualify(self.transport)
        self.assertEqual(self.calls, [])
        self.assertEqual(again["counts"]["qualification"], {"qualified": 6})

    def test_an_existing_policy_is_never_rewritten_or_widened(self):
        self.write_policy(["D1", "D2"])
        before = self.policy_file.read_bytes()
        report = self._qualify(self.transport)
        self.assertEqual(self.policy_file.read_bytes(), before)
        self.assertEqual(report["counts"]["qualification"], {"qualified": 2, "exception": 4})

    def test_the_generated_policy_covers_any_receipt_without_a_list(self):
        report = self._qualify(self.transport)
        scope = json.loads(self.policy_file.read_text())["scope"]
        self.assertEqual(scope, {"receipts": ["D*"], "audiences": ["source"]})
        self.assertEqual(report["counts"]["qualification"], {"qualified": 6})
        self.assertEqual(report["counts"]["out_of_scope"], {"entries": 0, "model_calls_skipped": 0})

    def test_the_generated_policy_covers_receipts_beyond_the_old_2500_enumeration(self):
        from tools.diagnostics.catalog_lock_benchmark import seed
        seed(self.root, 2600)  # synthetic registrations D0..D2599; the cohort gets D2601..
        report = self._qualify(self.transport)
        receipts = {e["receipt_id"] for e in report["entries"] if "receipt_id" in e}
        self.assertTrue(all(int(r[1:]) > 2500 for r in receipts), receipts)
        self.assertEqual(report["counts"]["qualification"], {"qualified": 6})
        self.assertEqual(len(self.calls), 6)

    def test_audience_is_still_part_of_the_scope_check(self):
        self.write_policy(["D*"], audiences=("agent",))
        report = self._qualify(self.transport)
        self.assertEqual(report["counts"]["qualification"], {"exception": 6})
        self.assertEqual(report["counts"]["out_of_scope"], {"entries": 6, "model_calls_skipped": 6})
        self.assertEqual(self.calls, [])

    def test_an_entry_whose_rendition_fails_validation_is_not_skipped_for_scope(self):
        self.write_policy(["D1"])
        original = standing._rendition
        with unittest.mock.patch.object(standing, "_rendition",
                                        side_effect=lambda *a: (b"", None, "rendition-fidelity")):
            report = self._qualify(self.transport)
        # An unvalidated rendition is not judged by scope: the model path runs as before.
        self.assertEqual(report["counts"]["out_of_scope"], {"entries": 0, "model_calls_skipped": 0})
        self.assertEqual(len(self.calls), 6)
        self.assertIs(standing._rendition, original)


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
        self.assertEqual(set(printed), {"counts", "usage", "throughput"})
        self.assertEqual(printed["throughput"]["cap"], 3)
        self.assertLessEqual(printed["throughput"]["max_in_flight"], 3)
        self.assertEqual(printed["counts"]["qualification"], {"qualified": 6})
        self.assertEqual(printed["usage"], {"prompt_tokens": 30, "completion_tokens": 12})
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)


class QualifyRepairedTests(BlockedCase, QualifyCase):
    def test_qualify_reviews_repaired_registrations_once_and_reuses_them_afterwards(self):
        self.block_registrations()
        cohort.recover_identity(self.root, apply=True)
        transport, calls = self._transport()
        report = self._qualify(transport)
        self.assertEqual(report["counts"]["outcome"]["extracted"], 6)
        self.assertEqual(report["counts"]["qualification"], {"qualified": 6})
        self.assertEqual(len(calls), 6)
        requests = len(store.correlation_ids(self.root))
        self.assertEqual(self._qualify(transport)["counts"]["qualification"], {"qualified": 6})
        self.assertEqual((len(calls), len(store.correlation_ids(self.root))), (6, requests))


if __name__ == "__main__":
    unittest.main()
