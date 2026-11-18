"""Agent-written context ingested as a cohort with a declared origin (``--agent-origin``)."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tools.context_index import confidence, packets
from tools.intake import agent_origin, cohort, upload
from tools.intake.contracts import IntakeError
from tests.test_intake_cohort import QualifyCase, tree

GENERATOR = "the_librarian (previous version)"
FILES = {
    "ACTIVE/risks.md": b"# Risks\nAgent summary of zebra risks.\n",
    "REFERENCE/notes/meeting.md": b"# Meeting\nAgent notes about zebra crossings.\n",
    "REFERENCE/notes/drafts/wip.md": b"# Draft\nUnfinished zebra draft.\n",
    "REFERENCE/extractions/old.json": b"{\"zebra\": 1}\n",
    "INBOX/01-NEW/primary.md": b"# Primary\nA primary zebra document.\n",
    "README.md": b"# Context\nAgent-maintained zebra overview.\n",
}
ORIGIN = {"version": 1, "generator": GENERATOR, "source_repository": "example-docs",
          "provenance": "git", "include": ["ACTIVE", "REFERENCE/notes", "README.md"],
          "exclude": ["REFERENCE/notes/drafts"]}


def git(directory, *args, date="2026-05-01T12:00:00+00:00"):
    environment = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
                   "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(directory)}
    return subprocess.run(["git", "-c", "user.name=Agent", "-c", "user.email=agent@example.invalid",
                           "-c", "init.defaultBranch=main", "-C", str(directory), *args],
                          check=True, capture_output=True, env=environment).stdout.decode().strip()


class AgentOriginTests(QualifyCase):
    def setUp(self):
        super().setUp()
        self.docs = Path(self.temp.name) / "agent-docs"
        for name, raw in FILES.items():
            (self.docs / name).parent.mkdir(parents=True, exist_ok=True)
            (self.docs / name).write_bytes(raw)

    def origin(self, **changes):
        return agent_origin.validate({**ORIGIN, **changes})

    def repository(self):
        """A git work tree: README committed earlier, a tracked file modified, a note untracked."""
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        git(self.docs, "init", "-q")
        git(self.docs, "add", "README.md")
        git(self.docs, "commit", "-q", "-m", "readme", date="2026-04-02T12:00:00+00:00")
        git(self.docs, "add", "ACTIVE", "REFERENCE/notes/drafts")
        git(self.docs, "commit", "-q", "-m", "active")
        (self.docs / "ACTIVE/risks.md").write_bytes(FILES["ACTIVE/risks.md"] + b"Edited later.\n")
        return git(self.docs, "rev-parse", "HEAD")

    def test_origin_selects_included_files_and_reports_excluded_ones_unread(self):
        entries = cohort.classify(self.root, self.docs, origin=self.origin(provenance="none"))
        self.assertEqual({e["relative"]: e["disposition"] for e in entries}, {
            "ACTIVE/risks.md": "admit", "REFERENCE/notes/meeting.md": "admit", "README.md": "admit",
            "REFERENCE/notes/drafts/wip.md": "excluded"})
        self.assertNotIn("sha256", next(e for e in entries if e["disposition"] == "excluded"))
        everything = cohort.classify(self.root, self.docs, origin=self.origin(include=None, exclude=[]))
        self.assertEqual(len(everything), len(FILES))
        with self.assertRaises(IntakeError) as raised:
            cohort.classify(self.root, self.docs, origin=self.origin(include=["MISSING"]))
        self.assertEqual(raised.exception.code, "agent_origin_include_missing")

    def test_a_symlinked_include_is_refused_not_followed(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "secret.md").write_bytes(b"# Secret\n")
        os.symlink(outside, self.docs / "linked")
        for include in ("linked", "linked/secret.md"):
            entries = cohort.classify(self.root, self.docs, origin=self.origin(
                provenance="none", include=[include]))
            self.assertEqual([(e["relative"], e["disposition"]) for e in entries], [(include, "unsafe-path")])

    def test_invalid_origin_files_are_refused(self):
        path = Path(self.temp.name) / "origin.json"
        for bad in ({**ORIGIN, "version": 2}, {**ORIGIN, "generator": ""}, {**ORIGIN, "extra": 1},
                    {**ORIGIN, "include": ["../escape"]}, {**ORIGIN, "include": []},
                    {**ORIGIN, "exclude": ["/abs"]}, {**ORIGIN, "provenance": "svn"}, ["x"]):
            with self.subTest(bad=bad):
                path.write_text(json.dumps(bad))
                with self.assertRaises(IntakeError) as raised:
                    agent_origin.load(path)
                self.assertEqual(raised.exception.code, "invalid_agent_origin")
        path.write_text(json.dumps(ORIGIN))
        self.assertEqual(agent_origin.load(path)["include"], ORIGIN["include"])

    def test_classify_cli_reports_the_declared_origin_for_a_dry_run(self):
        path = Path(self.temp.name) / "origin.json"
        path.write_text(json.dumps({**ORIGIN, "provenance": "none"}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cohort.main(["classify", "--root", str(self.root), "--inbox", str(self.docs),
                                          "--agent-origin", str(path)]), 0)
        counts = json.loads(output.getvalue())["counts"]
        self.assertEqual((counts["disposition"], counts["format"], counts["admitted_bytes"]),
                         ({"admit": 3, "excluded": 1}, {"text": 3},
                          sum(len(FILES[n]) for n in ("ACTIVE/risks.md", "REFERENCE/notes/meeting.md",
                                                      "README.md"))))
        self.assertEqual((counts["agent_origin"]["generator"], counts["agent_origin"]["declared"]),
                         (GENERATOR, {"committed": 0, "uncommitted": 0, "none": 3}))

    def test_git_provenance_declares_last_commit_and_date_and_leaves_the_tree_untouched(self):
        head = self.repository()
        status = git(self.docs, "status", "--porcelain")
        before = tree(self.docs)
        report = cohort.ingest(self.root, self.docs, system="agent-docs", actor="cohort-runner",
                               origin=self.origin())
        self.assertEqual(report["counts"]["outcome"]["extracted"], 3)
        self.assertEqual(report["counts"]["agent_origin"]["head"], head)
        self.assertEqual(report["counts"]["agent_origin"]["declared"],
                         {"committed": 1, "uncommitted": 2, "none": 0})
        declared = {e["relative"]: (e["provenance"], e["agent_generated"])
                    for e in report["entries"] if e["disposition"] == "admit"}
        readme_commit = git(self.docs, "log", "-1", "--format=%H", "--", "README.md")
        self.assertEqual(declared["README.md"], ("committed", {
            "generator": GENERATOR, "generated_on": "2026-04-02", "source_commit": readme_commit,
            "source_repository": "example-docs"}))
        for name in ("ACTIVE/risks.md", "REFERENCE/notes/meeting.md"):  # modified, untracked
            self.assertEqual(declared[name], ("uncommitted", {
                "generator": GENERATOR, "generated_on": None, "source_commit": None,
                "source_repository": "example-docs"}))
        readme = next(e for e in report["entries"] if e["relative"] == "README.md")
        sidecar = json.loads((self.root / readme["original"]).with_name(upload.SIDECAR).read_bytes())
        self.assertEqual((sidecar["version"], sidecar["origin"]["agent_generated"]),
                         (2, declared["README.md"][1]))
        self.assertEqual((tree(self.docs), git(self.docs, "status", "--porcelain")), (before, status))
        again = cohort.ingest(self.root, self.docs, system="agent-docs", actor="cohort-runner",
                              origin=self.origin())
        self.assertEqual(again["counts"]["outcome"]["extracted"], 3)
        git(self.docs, "add", "-A")
        git(self.docs, "commit", "-q", "-m", "later")
        relabelled = cohort.ingest(self.root, self.docs, system="agent-docs", actor="cohort-runner",
                                   origin=self.origin())
        self.assertEqual(relabelled["counts"]["failed_by_code"], {"receipt/submission_conflict": 2})
        self.assertEqual(relabelled["counts"]["outcome"]["extracted"], 1)

    def test_expanded_archive_members_are_declared_with_their_archive_files_commit(self):
        from tests.test_intake_archives import make_zip
        from tools.intake import cohort_archives
        (self.docs / "ACTIVE/bundle.zip").write_bytes(make_zip([("inner.md", b"# Inner\nZebra.\n")]))
        self.repository()  # commits ACTIVE, the archive with it
        cohort_archives.expand(self.root, self.docs, apply=True)
        entries = cohort.classify(self.root, self.docs, origin=self.origin())
        agent_origin.declare(self.docs, self.origin(), entries)
        member = next(e for e in entries if e["relative"] == "ACTIVE/bundle.zip!/inner.md")
        bundle = git(self.docs, "log", "-1", "--format=%H", "--", "ACTIVE/bundle.zip")
        self.assertEqual((member["provenance"], member["agent_generated"]["source_commit"]),
                         ("committed", bundle))

    def test_git_provenance_needs_the_repository_root(self):
        self.repository()
        with self.assertRaises(IntakeError) as raised:
            agent_origin.declare(self.docs / "ACTIVE", self.origin(include=None), [])
        self.assertEqual(raised.exception.code, "agent_origin_not_repository_root")

    def test_qualified_agent_context_is_low_confidence_and_labelled_in_search_and_get(self):
        from tools.context_index import context_index as index
        self.repository()
        transport, calls = self._transport()
        self._qualify(transport, inbox=self.inbox)
        report = self._qualify(transport, inbox=self.docs, system="agent-docs", origin=self.origin())
        self.assertEqual(report["counts"]["qualification"], {"qualified": 3})
        self.assertEqual(report["counts"]["published"], 1)
        readme_commit = git(self.docs, "log", "-1", "--format=%H", "--", "README.md")
        manifest, _ = index.load_manifest(self.root / "config/selection.local.json", self.root)
        with contextlib.closing(index.readonly(self.root / cohort.DEFAULT_INDEX)) as db:
            hits = index.search(db, "zebra", 10, None, root=self.root)["hits"]
            self.assertEqual(len(hits), 3)
            for hit in hits:
                self.assertEqual(hit["confidence"], {"level": "low", "reasons": ["agent-generated"]})
            readme = next(h for h in hits if "overview" in h["excerpt"])
            self.assertEqual(readme["origin"], "agent-generated (Generated by " + GENERATOR
                             + " on 2026-04-02 from example-docs@" + readme_commit + ":README.md)")
            uncommitted = next(h for h in hits if "crossings" in h["excerpt"])
            self.assertEqual(uncommitted["origin"], "agent-generated (Generated by " + GENERATOR
                             + " from example-docs:REFERENCE/notes/meeting.md)")
            primary = index.search(db, "Fund codes", 10, None, root=self.root)["hits"]
            self.assertEqual([("origin" in h, h["confidence"]["level"]) for h in primary], [(False, "normal")])
            excluded = index.search(db, "zebra", 10, None, root=self.root, low_confidence="exclude")
            self.assertEqual((excluded["hits"], excluded["low_confidence_excluded"]), ([], 3))
            got = packets.packet(db, manifest, readme["locator"], 60000, root=self.root)
            self.assertEqual(got["confidence"]["level"], "low")
            self.assertIn("agent-generated", got["confidence"]["reasons"])
            self.assertEqual(confidence.origin_label(got["pieces"][0]["confidence"]), readme["origin"])


if __name__ == "__main__":
    unittest.main()
