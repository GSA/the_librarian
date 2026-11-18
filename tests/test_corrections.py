"""Claim corrections: append-only records that mark indexed claims shaky or confirmed at read time."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import sqlite3

from tools.context_index import cli, context_index as index
from tools.context_index import corrections as reading, packets
from tools.review_events import corrections
from tools.review_events import event_store as events
from tools.review_events.review_events import ReviewError

from tests.support import WorkspaceCase

POLICY = """# Retention policy

Backups are kept for 30 days before deletion.
Logs are reviewed weekly by the operations team.

## Exceptions

Legal holds suspend deletion.
"""
CLAIM = "Backups are kept for 30 days"
DOCUMENT = {"type": "document", "receipt_id": "D1", "locator": "sources/README.txt#retention"}
QUESTION = {"type": "question", "question": "How long are backups kept?", "answer": "Thirty days.",
            "answered_by": "records-officer", "answered_on": "2024-05-01"}


class Corrections(WorkspaceCase):
    def setUp(self):
        super().setUp()
        (self.root / "documents").mkdir()
        self.write(POLICY)
        self.config["documents"] = [{"path": "documents/policy.md", "role": "policy", "context": []}]
        self.registration()
        events.initialize(self.root)
        corrections.initialize(self.root)
        self.build()

    def write(self, text):
        (self.root / "documents/policy.md").write_text(text)

    def state(self):
        return corrections.status(self.root)["state_token"]

    def record(self, request, kind="dispute", claim=CLAIM, source=DOCUMENT, **fields):
        drafts = [{"kind": kind, "locator": "documents/policy.md#retention-policy", "claim": claim,
                   "summary": "The records schedule says 90 days.", "source": source, **fields}]
        return corrections.append(self.root, drafts, "test-runner", request, self.state())

    def resolve(self, request, kind, ids, source=QUESTION):
        drafts = [{"kind": kind, "summary": "Settled with the records officer.", "source": source, "resolves": ids}]
        return corrections.append(self.root, drafts, "test-runner", request, self.state())

    def get(self, locator="documents/policy.md#retention-policy"):
        with closing(index.readonly(self.db)) as connection:
            config = index.load_manifest(self.manifest, self.root)[0]
            return packets.packet(connection, config, locator, 30000, root=self.root)

    def search(self, query, low="include"):
        with closing(index.readonly(self.db)) as connection:
            return index.search(connection, query, 10, None, root=self.root, low_confidence=low)

    def test_open_correction_marks_the_claim_and_lowers_only_its_text(self):
        before = self.get()
        self.assertEqual(before["confidence"]["level"], "normal")
        correction = self.record("req-1", kind="correct", statement="Backups are kept for 90 days.")
        result = self.get()
        piece = result["pieces"][0]
        self.assertEqual(piece["confidence"]["level"], "low")
        self.assertEqual(result["confidence"]["level"], "low")
        [entry] = piece["corrections"]
        self.assertEqual(entry["correction_id"], correction["correction_ids"][0])
        self.assertEqual((entry["status"], entry["statement"], entry["source"]["receipt_id"]),
                         ("open", "Backups are kept for 90 days.", "D1"))
        reason = [r for r in piece["confidence"]["reasons"] if r["code"] == "open-correction"][0]
        self.assertIn("Corrected: Backups are kept for 90 days.", " ".join(reason["details"]))
        # The subsection does not carry the claim; its inherited parent context does.
        exact, parent = self.get("documents/policy.md#exceptions")["pieces"]
        self.assertEqual((exact["confidence"]["level"], "corrections" in exact), ("normal", False))
        self.assertEqual((parent["confidence"]["level"], len(parent["corrections"])), ("low", 1))
        # Compact projection keeps the document label and puts the lowered label on the piece.
        compact = packets.compact_packet(result)
        self.assertEqual(compact["documents"]["d1"]["confidence"]["level"], "normal")
        self.assertEqual(compact["pieces"][0]["confidence"]["level"], "low")
        with closing(index.readonly(self.db)) as connection:
            config = index.load_manifest(self.manifest, self.root)[0]
            many = packets.packet_many(connection, config, ["documents/policy.md#exceptions"], root=self.root)
        self.assertEqual(many["confidence"]["level"], "low")
        self.assertEqual(many["documents"]["d1"]["confidence"]["level"], "normal")
        pieces = many["requests"][0]["packet"]["pieces"]
        self.assertEqual([p.get("confidence", {}).get("level") for p in pieces], [None, "low"])

    def test_search_flags_and_filters_open_corrections(self):
        correction = self.record("req-1")["correction_ids"][0]
        hit = self.search("Backups")["hits"][0]
        self.assertEqual(hit["corrections"]["open"], [correction])
        self.assertIn("open-correction", hit["confidence"]["reasons"])
        excluded = self.search("Backups", "exclude")
        self.assertEqual((excluded["hits"], excluded["low_confidence_excluded"]), ([], 1))
        self.assertEqual(self.search("Legal", "exclude")["low_confidence_excluded"], 0)
        self.assertEqual(len(self.search("Backups", "last")["hits"]), 1)

    def test_confirm_restores_normal_and_retract_closes(self):
        first = self.record("req-1")["correction_ids"][0]
        confirmed = self.resolve("req-2", "confirm", [first])["correction_ids"][0]
        piece = self.get()["pieces"][0]
        self.assertEqual(piece["confidence"]["level"], "normal")
        [entry] = piece["corrections"]
        self.assertEqual((entry["status"], entry["resolved_by"]["correction_id"]), ("resolved", confirmed))
        self.assertEqual(self.search("Backups")["hits"][0]["corrections"]["resolved"], [first])
        second = self.record("req-3", source=QUESTION)["correction_ids"][0]
        self.assertEqual(self.get()["confidence"]["level"], "low")
        self.resolve("req-4", "retract", [second], source=None)
        self.assertEqual(self.get()["confidence"]["level"], "normal")
        with self.assertRaisesRegex(ReviewError, "already resolved"):
            self.resolve("req-5", "retract", [second], source=None)

    def test_drifted_claim_stays_flagged_until_resolved(self):
        self.record("req-1")
        self.write(POLICY.replace("30 days", "90 days"))
        self.build()
        piece = self.get()["pieces"][0]
        self.assertEqual(piece["corrections"][0]["drift"], "claim-not-found")
        self.assertEqual(piece["confidence"]["level"], "low")
        with closing(index.readonly(self.db)) as connection:
            summary = reading.summary(connection, corrections.read(self.root))
        self.assertEqual(summary["open_drifted"], {"claim-not-found": 1})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["--root", str(self.root), "status"]), 0)
        self.assertEqual(json.loads(out.getvalue())["claim_corrections"]["open"], 1)

    def test_refusals_leave_the_store_unchanged(self):
        before = self.state()
        cases = [
            ("not in the indexed text", dict(claim="Backups are kept forever")),
            ("not in the catalog", dict(source={**DOCUMENT, "receipt_id": "D9"})),
            ("must cite its source", dict(source=None)),
            ("Only a correct record", dict(statement="x")),
        ]
        for message, fields in cases:
            with self.subTest(message), self.assertRaisesRegex(ReviewError, message):
                self.record("req-bad", **fields)
        with self.assertRaisesRegex(ReviewError, "Stale state"):
            corrections.append(self.root, [{"kind": "dispute", "locator": "documents/policy.md#retention-policy",
                                            "claim": CLAIM, "summary": "x", "source": QUESTION}],
                               "test-runner", "req-stale", "0" * 64)
        with self.assertRaisesRegex(ReviewError, "unknown correction"):
            self.resolve("req-bad", "confirm", ["cor-" + "0" * 20])
        self.assertEqual(self.state(), before)
        self.record("req-1")
        with self.assertRaisesRegex(ReviewError, "different actor or payload"):
            self.record("req-1", source=QUESTION)
        self.assertTrue(self.record("req-1")["idempotent"])

    def test_index_stale_refuses_recording(self):
        self.write(POLICY + "\nMore.\n")
        with self.assertRaisesRegex(ReviewError, "stale"):
            self.record("req-1")

    def test_intake_file_records_all_or_nothing(self):
        path = self.root / "corrections.json"
        good = {"schema": "claim-corrections-v1", "source": DOCUMENT, "corrections": [
            {"kind": "dispute", "locator": "documents/policy.md#retention-policy", "claim": CLAIM,
             "summary": "Transcript disputes the period."},
            {"kind": "correct", "locator": "documents/policy.md#exceptions", "claim": "Legal holds suspend deletion.",
             "statement": "Legal holds and audits suspend deletion.", "summary": "Audits also suspend."}]}
        bad = {**good, "corrections": [*good["corrections"], {**good["corrections"][0], "claim": "absent"}]}
        before = self.state()
        path.write_text(json.dumps(bad))
        self.assertEqual(self.cli("intake", str(path), "--actor", "test-runner", "--request-id", "req-1",
                                  "--expected-state", before)[0], 1)
        self.assertEqual(self.state(), before)
        path.write_text(json.dumps(good))
        code, output = self.cli("intake", str(path), "--actor", "test-runner", "--request-id", "req-1",
                                "--expected-state", before)
        self.assertEqual((code, len(output["correction_ids"]), output["open"]), (0, 2, 2))
        listed = self.cli("list", "--status", "open")[1]
        self.assertEqual(listed["total_matching"], 2)

    def test_store_is_append_only_and_backups_restore(self):
        self.record("req-1")
        _, db, _ = corrections.locations(self.root)
        with closing(sqlite3.connect(db)) as connection, self.assertRaises(sqlite3.DatabaseError):
            connection.execute("UPDATE corrections SET payload='{}'")
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("DROP TRIGGER immutable_corrections_update")
            connection.commit()
        with self.assertRaisesRegex(index.IndexError_, "failed validation"):
            self.get()
        self.assertEqual(self.cli("status")[0], 1)

    def test_backup_verify_and_restore(self):
        snapshot = corrections.backup(self.root)["snapshot"]
        self.record("req-1")
        self.assertEqual(self.cli("verify-backup", snapshot)[1]["records"], 0)
        restored = corrections.restore(self.root, snapshot, self.state())
        self.assertEqual((restored["records"], corrections.status(self.root)["records"]), (0, 0))
        self.assertIsNotNone(restored["safety_backup"])

    def test_absent_store_leaves_reads_unchanged(self):
        _, db, _ = corrections.locations(self.root)
        db.unlink()
        self.assertNotIn("corrections", self.get()["pieces"][0])
        self.assertNotIn("corrections", self.search("Backups")["hits"][0])

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = corrections.main(["--root", str(self.root), *argv])
        return code, json.loads(out.getvalue()) if code == 0 else err.getvalue()
