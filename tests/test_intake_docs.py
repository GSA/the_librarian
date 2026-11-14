"""Derived per-document state table in the intake ledger (Kata e22r)."""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import unittest
import unittest.mock

from tools.catalog import catalog_store as catalog
from tools.intake import adapters, cohort, docs, docsync, runner, store, upload
from tools.intake.contracts import IntakeError
from tests.test_intake_cohort import CohortCase

AUTHORITATIVE = ("requests", "stages", "attempts", "replays", "acknowledgments")


def rows(root, where=""):
    db = store.locations(root)[2]
    with closing(sqlite3.connect(db)) as c:
        c.row_factory = sqlite3.Row
        return {(r["system"], r["path"]): dict(r)
                for r in c.execute("SELECT * FROM docs " + where)}


def authoritative(root):
    db = store.locations(root)[2]
    with closing(sqlite3.connect(db)) as c:
        return {t: c.execute(f"SELECT * FROM {t} ORDER BY 1,2").fetchall() for t in AUTHORITATIVE}


def stat_of(path):
    info = os.stat(path, follow_symlinks=False)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class DocsCase(CohortCase):
    def ingest(self, **extra):
        return cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner", **extra)

    def entry(self, report, relative):
        return next(e for e in report["entries"] if e["relative"] == relative)

    def drop_table(self):
        with closing(sqlite3.connect(store.locations(self.root)[2])) as c, c:
            c.execute("DROP TABLE docs")


class RecordingTests(DocsCase):
    def test_a_new_ledger_has_the_docs_table_and_ingest_records_one_row_per_admitted_file(self):
        report = self.ingest()
        saved = rows(self.root)
        admitted = [e for e in report["entries"] if e["disposition"] == "admit"]
        self.assertEqual({p for _, p in saved}, {e["relative"] for e in admitted})
        for e in admitted:
            row = saved[("inbox", e["relative"])]
            self.assertEqual((row["state"], row["sha256"], row["format"], row["receipt_id"], row["original"],
                              row["rendition"], row["rendition_sha256"], row["conflicts"]),
                             ("extracted", e["sha256"], e["format"], e["receipt_id"], e["original"],
                              e["rendition"], e["rendition_sha256"], e["conflicts"]))
            self.assertEqual(e["submission_id"], row["submission_id"])
            self.assertEqual("extract-" + e["submission_id"] + ("-m%d" % cohort.mhtml.METHOD_VERSION
                                                                if e["format"] == "mhtml" else ""),
                             row["extract_head"])
            self.assertEqual((row["dev"], row["ino"], row["size"], row["mtime_ns"], row["ctime_ns"]),
                             stat_of(self.inbox / e["relative"]))
            self.assertIsNotNone(row["last_run"])

    def test_replay_report_is_unchanged_with_and_without_trusting_stat(self):
        first = self.ingest()
        self.assertEqual(self.ingest(), first)
        self.assertEqual(self.ingest(trust_stat=True), first)

    def test_unchanged_replay_writes_nothing(self):
        self.ingest()
        before = rows(self.root)
        db = store.locations(self.root)[2]
        stamp = db.stat().st_mtime_ns
        self.ingest()
        self.assertEqual(rows(self.root), before)
        self.assertEqual(db.stat().st_mtime_ns, stamp)

    def test_a_ledger_without_the_table_is_never_migrated_by_ingest(self):
        self.drop_table()
        first = self.ingest()
        self.assertEqual(first["counts"]["outcome"]["extracted"], 6)
        self.assertEqual(self.ingest(), first)
        with closing(sqlite3.connect(store.locations(self.root)[2])) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='docs'").fetchone())

    def test_trust_stat_without_a_table_still_hashes_everything(self):
        self.drop_table()
        first = self.ingest()
        self.assertEqual(self.ingest(trust_stat=True), first)


class TransactionTests(DocsCase):
    def blocked_first_run(self):
        real = adapters.prepare

        def failing(root, stage):
            if stage["operation"] == "extract-prepare":
                raise adapters.StageFailure("blocked", "native-input")
            return real(root, stage)
        with unittest.mock.patch.object(adapters, "prepare", side_effect=failing):
            return self.ingest()

    def test_failed_and_blocked_stages_move_the_row_in_the_same_transaction(self):
        report = self.blocked_first_run()
        self.assertEqual(report["counts"]["outcome"]["failed"], 6)
        self.assertEqual({r["state"] for r in rows(self.root).values()}, {"blocked"})
        corr = rows(self.root)[("inbox", "a/notes.md")]["extract_head"]
        runner.replay(self.root, corr, "prepare")
        self.assertEqual(rows(self.root)[("inbox", "a/notes.md")]["state"], "extracting")
        runner.settle(self.root, corr, "prepare")
        self.assertEqual(rows(self.root)[("inbox", "a/notes.md")]["state"], "extracting")
        # The register request does not exist yet, so a replay finishes this document only.
        self.assertEqual(self.ingest()["counts"]["outcome"]["extracted"], 1)
        states = {p: r["state"] for (_, p), r in rows(self.root).items()}
        self.assertEqual(states.pop("a/notes.md"), "extracted")
        self.assertEqual(set(states.values()), {"blocked"})

    def test_a_stage_update_and_its_docs_update_commit_or_roll_back_together(self):
        self.ingest()
        corr = rows(self.root)[("inbox", "a/notes.md")]["register_head"]
        with unittest.mock.patch.object(docs, "refresh", side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(IntakeError):
                runner.replay(self.root, corr, "register")  # not replayable: refused before any write
        before = store.inspect(self.root, corr)["stages"][0]["status"]
        self.assertEqual(before, "succeeded")

    def test_a_refresh_failure_rolls_the_stage_update_back(self):
        self.blocked_first_run()
        corr = rows(self.root)[("inbox", "a/notes.md")]["extract_head"]
        with unittest.mock.patch.object(docs, "refresh", side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(IntakeError):
                runner.replay(self.root, corr, "prepare")
        self.assertEqual(store.inspect(self.root, corr)["stages"][0]["status"], "blocked")
        self.assertEqual(rows(self.root)[("inbox", "a/notes.md")]["state"], "blocked")

    def test_a_correction_repoints_the_row_to_the_new_request(self):
        self.ingest()
        corr = rows(self.root)[("inbox", "a/notes.md")]["register_head"]
        fixed = cohort._correct(self.root, store.inspect(self.root, corr))
        row = rows(self.root)[("inbox", "a/notes.md")]
        self.assertEqual((row["register_head"], row["state"]), (fixed, "extracting"))
        self.assertEqual(docsync.audit(self.root)["differences"], [])


class BackfillAuditTests(DocsCase):
    def test_backfill_derives_the_rows_of_an_existing_ledger_without_touching_the_requests(self):
        self.ingest()
        before = rows(self.root)
        self.drop_table()
        frozen = authoritative(self.root)
        dry = docsync.backfill(self.root)
        self.assertEqual((dry["apply"], dry["table_present"], dry["missing"]), (False, False, 6))
        self.assertEqual(authoritative(self.root), frozen)
        done = docsync.backfill(self.root, apply=True)
        self.assertEqual((done["apply"], done["created"]), (True, 6))
        after = rows(self.root)
        strip = lambda r: {k: v for k, v in r.items()
                           if k not in {"dev", "ino", "size", "mtime_ns", "ctime_ns", "last_run"}}
        self.assertEqual({k: strip(v) for k, v in after.items()}, {k: strip(v) for k, v in before.items()})
        self.assertEqual(authoritative(self.root), frozen)
        self.assertEqual(docsync.audit(self.root)["differences"], [])

    def test_a_default_replay_after_backfill_fills_the_stat_identity(self):
        self.ingest()
        self.drop_table()
        docsync.backfill(self.root, apply=True)
        self.assertIsNone(rows(self.root)[("inbox", "a/notes.md")]["ino"])
        self.ingest()
        self.assertEqual(rows(self.root)[("inbox", "a/notes.md")]["ino"],
                         (self.inbox / "a/notes.md").stat().st_ino)

    def test_docs_can_be_dropped_and_rebuilt_without_losing_anything(self):
        self.ingest()
        before = rows(self.root)
        self.drop_table()
        docsync.backfill(self.root, apply=True)
        for key, row in rows(self.root).items():
            self.assertEqual(row["state"], before[key]["state"])
        report = self.ingest()
        self.assertEqual(report["counts"]["outcome"]["extracted"], 6)

    def test_backfill_follows_content_corrections_to_the_live_submission(self):
        first = self.ingest()
        old = self.entry(first, "a/notes.md")
        self.writable()
        (self.inbox / "a/notes.md").write_bytes(b"# Notes\nA different line\n")
        second = self.ingest()
        new = self.entry(second, "a/notes.md")
        self.assertEqual(rows(self.root)[("inbox", "a/notes.md")]["submission_id"], new["submission_id"])
        self.drop_table()
        docsync.backfill(self.root, apply=True)
        row = rows(self.root)[("inbox", "a/notes.md")]
        self.assertEqual((row["submission_id"], row["sha256"]), (new["submission_id"], new["sha256"]))
        self.assertNotEqual(old["submission_id"], new["submission_id"])

    def test_audit_reports_missing_extra_and_differing_rows_and_changes_nothing(self):
        self.ingest()
        self.assertEqual(docsync.audit(self.root)["counts"],
                         {"derived": 6, "rows": 6, "missing": 0, "extra": 0, "differing": 0, "skipped": {}})
        frozen = authoritative(self.root)
        with closing(sqlite3.connect(store.locations(self.root)[2])) as c, c:
            c.execute("UPDATE docs SET state='blocked' WHERE path='a/notes.md'")
            c.execute("DELETE FROM docs WHERE path='m/plain.txt'")
            c.execute("INSERT INTO docs(system,path,format,submission_id,sha256,receipt_id,original,state) "
                      "VALUES ('inbox','ghost.md','text','inbox-x','0','D1','o','extracted')")
        report = docsync.audit(self.root)
        self.assertEqual({k: report["counts"][k] for k in ("missing", "extra", "differing")},
                         {"missing": 1, "extra": 1, "differing": 1})
        self.assertEqual(sorted(d["path"] for d in report["differences"]),
                         ["a/notes.md", "ghost.md", "m/plain.txt"])
        self.assertEqual(authoritative(self.root), frozen)

    def test_status_counts_documents_by_state_from_the_table(self):
        self.ingest()
        self.assertEqual(docsync.status(self.root), {"available": True, "total": 6, "by_state": {"extracted": 6}})
        self.drop_table()
        self.assertEqual(docsync.status(self.root)["available"], False)

    def test_cli_commands(self):
        self.ingest()
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            self.assertEqual(cohort.main(["audit-docs", "--root", str(self.root)]), 0)
            self.assertEqual(cohort.main(["docs-status", "--root", str(self.root)]), 0)
            self.drop_table()
            self.assertEqual(cohort.main(["backfill-docs", "--root", str(self.root)]), 0)
            self.assertEqual(cohort.main(["backfill-docs", "--root", str(self.root), "--apply"]), 0)
            self.assertEqual(cohort.main(["audit-docs", "--root", str(self.root)]), 0)
        self.assertIn('"differing": 0', out.getvalue())


class ChangeDetectionTests(DocsCase):
    """The hash decides whether a file changed unless --trust-stat is asked for (efz3 D8)."""

    def reads(self, **extra):
        real = upload.read_file
        paths = []

        def counting(path, limit):
            paths.append(Path(path).name)
            return real(path, limit)
        with unittest.mock.patch.object(upload, "read_file", counting):
            report = self.ingest(**extra)
        return report, paths

    def changed(self, mutate, **extra):
        first = self.ingest()
        old = self.entry(first, "a/notes.md")
        self.writable()
        path = self.inbox / "a/notes.md"
        before = path.stat()
        mutate(path)
        report = self.ingest(**extra)
        return old, self.entry(report, "a/notes.md"), before, report

    def same_size_edit_restoring_mtime(self, path):
        info = path.stat()
        path.write_bytes(b"# Notes\nLINE\n")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))

    def rename_over(self, path):
        other = path.with_name("replacement.tmp")
        other.write_bytes(b"# Notes\nreplaced\n")
        os.replace(other, path)

    def test_default_replay_hashes_every_admitted_file(self):
        self.ingest()
        _, paths = self.reads()
        for name in ("notes.md", "plain.txt", "plan.odt"):
            self.assertIn(name, paths)

    def test_trust_stat_skips_unchanged_admitted_files_only(self):
        self.ingest()
        _, paths = self.reads(trust_stat=True)
        for name in ("notes.md", "plain.txt", "plan.odt"):
            self.assertNotIn(name, paths)
        self.assertIn("bad.txt", paths)  # never admitted, so never trusted

    def test_every_change_is_detected_by_default_and_with_trust_stat(self):
        cases = {
            "same-size edit with restored mtime": self.same_size_edit_restoring_mtime,
            "rename over": self.rename_over,
            "truncate": lambda p: p.write_bytes(b"# Notes\n"),
            "append": lambda p: p.write_bytes(p.read_bytes() + b"more\n"),
        }
        for trust in (False, True):
            for label, mutate in cases.items():
                with self.subTest(case=label, trust_stat=trust):
                    self.tearDown_inbox()
                    old, new, _, report = self.changed(mutate, trust_stat=trust)
                    self.assertEqual(new["outcome"], "extracted")
                    self.assertNotEqual(new["submission_id"], old["submission_id"])
                    self.assertEqual(rows(self.root)[("inbox", "a/notes.md")]["sha256"], new["sha256"])
                    self.assertEqual(report["counts"]["outcome"]["extracted"], 2)

    def test_touch_keeps_the_document_and_refreshes_the_recorded_stat(self):
        for trust in (False, True):
            with self.subTest(trust_stat=trust):
                self.tearDown_inbox()
                first = self.ingest()
                self.writable()
                path = self.inbox / "a/notes.md"
                os.utime(path, ns=(1, 1_700_000_000_000_000_000))
                again = self.ingest(trust_stat=trust)
                self.assertEqual(again, first)
                self.assertEqual(rows(self.root)[("inbox", "a/notes.md")]["mtime_ns"],
                                 path.stat().st_mtime_ns)

    def tearDown_inbox(self):
        """Fresh instance and inbox for each sub-case."""
        import shutil
        import tempfile
        for path in (self.root, self.inbox):
            if path.exists():
                self.writable()
                shutil.rmtree(path)
        self.root.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)
        self.inbox.mkdir()
        for name, raw in {"a/notes.md": b"# Notes\nLine\n", "m/plain.txt": b"Just text\n"}.items():
            target = self.inbox / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        self.extra = None
