"""Report of inbox files that copy a declared agent-written tree (Kata rqr2), on synthetic fixtures."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest

from tools.intake import agent_copies, agent_origin, cohort, deselect, upload
from tools.intake.contracts import IntakeError
from tests.test_intake_cohort import QualifyCase, tree

AGENT = {
    "ACTIVE/risks.md": b"# Risks\n" + b"".join(b"Agent line %d about zebras.\n" % n for n in range(40)),
    "REFERENCE/meeting.md": b"# Meeting\n" + b"".join(b"Agent note %d about crossings.\n" % n for n in range(40)),
    "README.md": b"# Context\nAgent-maintained overview.\n",
}
LATER = AGENT["REFERENCE/meeting.md"] + b"One more line the snapshot lacks.\n"
COPIES = {
    "snapshots/.local/before/ACTIVE/risks.md": AGENT["ACTIVE/risks.md"],
    "snapshots/old/REFERENCE/meeting.md": LATER,
    "elsewhere/ACTIVE/risks.md": b"# Risks\nA human wrote an unrelated risk register.\n",
    "elsewhere/README.md": b"# Readme\nCompletely different content.\n",
    "elsewhere/notes.md": b"# Notes\nNo agent file shares this path.\n",
}
ORIGIN = {"version": 1, "generator": "the_librarian (previous version)", "provenance": "none"}


def write(directory, files):
    for name, raw in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(raw)


class CopyCase(QualifyCase):
    def setUp(self):
        super().setUp()
        self.agent = Path(self.temp.name) / "agent-docs"
        self.copies = Path(self.temp.name) / "copies"
        write(self.agent, AGENT)
        write(self.copies, COPIES)
        cohort.ingest(self.root, self.agent, system="agent-docs", actor="cohort-runner",
                      origin=agent_origin.validate(ORIGIN))
        cohort.ingest(self.root, self.copies, system="inbox-02", actor="cohort-runner")

    def report(self, **extra):
        return agent_copies.report(self.root, agent_systems=["agent-docs"], **extra)

    def by_path(self, report):
        return {e["inbox_path"]: e for e in report["entries"]}


class AgentCopyReportTests(CopyCase):
    def test_identical_and_near_identical_copies_of_agent_files_are_reported_by_path_tail(self):
        report = self.report()
        found = self.by_path(report)
        self.assertEqual(set(found), {"snapshots/.local/before/ACTIVE/risks.md",
                                      "snapshots/old/REFERENCE/meeting.md"})
        identical = found["snapshots/.local/before/ACTIVE/risks.md"]
        self.assertEqual((identical["match"], identical["similarity"], identical["system"],
                          identical["agent_path"], identical["agent_system"]),
                         ("identical", 1.0, "inbox-02", "ACTIVE/risks.md", "agent-docs"))
        similar = found["snapshots/old/REFERENCE/meeting.md"]
        self.assertEqual((similar["match"], similar["agent_path"]), ("similar", "REFERENCE/meeting.md"))
        self.assertTrue(0.9 <= similar["similarity"] < 1.0)
        self.assertTrue(identical["receipt_id"].startswith("D") and identical["agent_receipt_id"].startswith("D"))
        self.assertNotEqual(identical["receipt_id"], identical["agent_receipt_id"])

    def test_a_path_tail_alone_is_not_a_copy_and_counts_are_body_free(self):
        report = self.report()
        self.assertEqual(report["counts"], {
            "agent_files": 3, "candidates": 5, "tail_matches": 4, "identical": 1, "similar": 1,
            "different": 2, "already_declared": 0, "not_compared": 0})
        self.assertNotIn("elsewhere/ACTIVE/risks.md", self.by_path(report))
        self.assertNotIn("inbox_path", json.dumps(report["counts"]))

    def test_a_threshold_decides_what_near_identical_means(self):
        strict = self.report(min_similarity=1.0)
        self.assertEqual({e["match"] for e in strict["entries"]}, {"identical"})
        self.assertEqual(strict["counts"]["different"], 3)

    def test_an_agent_origin_copy_is_not_reported_as_undeclared(self):
        again = Path(self.temp.name) / "agent-again"
        write(again, {"snap/ACTIVE/risks.md": AGENT["ACTIVE/risks.md"]})
        cohort.ingest(self.root, again, system="agent-snap", actor="cohort-runner",
                      origin=agent_origin.validate(ORIGIN))
        report = self.report()
        self.assertNotIn("snap/ACTIVE/risks.md", self.by_path(report))
        self.assertEqual(report["counts"]["already_declared"], 1)

    def test_an_agent_system_without_a_declaration_is_refused(self):
        with self.assertRaises(IntakeError) as raised:
            agent_copies.report(self.root, agent_systems=["inbox-02"])
        self.assertEqual(raised.exception.code, "agent_copies_no_declared_files")
        with self.assertRaises(IntakeError) as raised:
            agent_copies.report(self.root, agent_systems=[])
        self.assertEqual(raised.exception.code, "agent_copies_system_required")

    def test_the_report_marks_copies_that_are_selected_and_writes_nothing(self):
        manifest = {"renditions": [{"receipt_id": e["receipt_id"]} for e in self.report()["entries"][:1]]}
        path = self.root / "config/selection.json"
        path.write_text(json.dumps(manifest))
        before = (tree(self.agent), tree(self.copies), path.read_bytes())
        report = self.report(manifest=path)
        self.assertEqual(sum(e["selected"] for e in report["entries"]), 1)
        self.assertEqual(report["counts"]["selected"], 1)
        self.assertEqual(before, (tree(self.agent), tree(self.copies), path.read_bytes()))

    def test_the_report_feeds_deselect_as_a_receipts_file(self):
        report = self.report()
        path = Path(self.temp.name) / "copies-report.json"
        path.write_text(json.dumps(report))
        self.assertEqual(sorted(deselect.read_receipts(path)),
                         sorted(e["receipt_id"] for e in report["entries"]))

    def test_cli_prints_counts_and_writes_the_private_report_with_paths(self):
        report_path = Path(self.temp.name) / "out/report.json"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = cohort.main(["agent-copies", "--root", str(self.root), "--agent-system", "agent-docs",
                                  "--report", str(report_path)])
        self.assertEqual(status, 0)
        printed = json.loads(output.getvalue())
        self.assertEqual(printed["counts"]["identical"], 1)
        self.assertNotIn("entries", printed)
        self.assertNotIn("risks.md", output.getvalue())
        self.assertEqual(os.stat(report_path).st_mode & 0o777, 0o600)
        self.assertEqual(len(json.loads(report_path.read_text())["entries"]), 2)


if __name__ == "__main__":
    unittest.main()
