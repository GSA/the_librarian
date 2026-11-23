"""Read-only hash manifest of a legacy source tree, joined to catalog receipts (Kata j5wh)."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools import policy, retirement
from tools.catalog import catalog_store as catalog
from tools.intake import cohort, cohort_archives, store
from tests.test_intake_archives import make_zip


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def snapshot(*roots):
    """Path -> (mode, size, mtime_ns, sha256) of every file and link below the roots."""
    result = {}
    for root in roots:
        for path in sorted(Path(root).rglob("*")):
            info = path.lstat()
            body = None if path.is_symlink() or path.is_dir() else digest(path.read_bytes())
            result[str(path)] = (info.st_mode, info.st_size, info.st_mtime_ns, body)
    return result


class RetirementCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "instance"
        self.root.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)
        self.tree = base / "legacy"
        self.inbox = self.tree / "INBOX/01-NEW"
        self.out = base / "out"
        self.put("INBOX/01-NEW/a/notes.md", b"# Notes\nalpha\n")
        self.put("INBOX/01-NEW/changed.md", b"# Changed\nbefore\n")
        self.put("INBOX/01-NEW/drop/bundle.zip", make_zip([("docs/a.md", b"# A\n")]))
        cohort_archives.expand(self.root, self.inbox, apply=True)
        cohort.ingest(self.root, self.inbox, system="legacy-new", actor="runner")
        # Everything below is added after intake: nothing here is in the ledger or the catalog.
        self.put("INBOX/01-NEW/changed.md", b"# Changed\nafter intake\n")
        self.put("INBOX/01-NEW/drop/other.zip", make_zip([("x.md", b"# X\n")]))
        self.put("INBOX/90-DUP/copy-of-notes.md", b"# Notes\nalpha\n")
        self.put("INBOX/01-NEW/video.mp4", b"\x00\x00\x00 ftypmp42 not text")
        self.put("INBOX/01-NEW/empty.md", b"")
        self.put("INBOX/01-NEW/a/notes.md:Zone.Identifier", b"[ZoneTransfer]\n")
        self.put("README.md", b"# legacy\n")
        self.put("tools/helper/run.py", b"print(1)\n")
        self.put(".git/HEAD", b"ref: refs/heads/main\n")
        os.symlink("README.md", self.tree / "link.md")
        self.mapping = ["INBOX/01-NEW=legacy-new"]

    def put(self, name, raw):
        path = self.tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def build(self, rules=None, workers=2):
        args = ["manifest", "--root", str(self.root), "--tree", str(self.tree), "--out", str(self.out),
                "--workers", str(workers)]
        for item in self.mapping:
            args += ["--system", item]
        if rules is not None:
            path = Path(self.temp.name) / "rules.json"
            path.write_text(json.dumps(rules))
            args += ["--dispositions", str(path)]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = retirement.main(args)
        summary = json.loads(stdout.getvalue())
        manifest = {}
        for line in Path(summary["manifest"]).read_text().splitlines()[1:]:
            entry = json.loads(line)
            manifest[entry["path"]] = entry
        return status, summary, manifest


class ManifestTests(RetirementCase):
    def test_every_file_gets_size_hash_and_one_explicit_category(self):
        status, summary, manifest = self.build()
        categories = {path: entry["category"] for path, entry in manifest.items()}
        self.assertEqual(categories, {
            "INBOX/01-NEW/a/notes.md": "receipt-verified",
            "INBOX/01-NEW/changed.md": "ledger-hash-mismatch",
            "INBOX/01-NEW/drop/bundle.zip": "archive-expanded",
            "INBOX/01-NEW/drop/other.zip": "archive-not-expanded",
            "INBOX/90-DUP/copy-of-notes.md": "receipt-by-content",
            "INBOX/01-NEW/video.mp4": "no-receipt",
            "INBOX/01-NEW/empty.md": "empty-file",
            "INBOX/01-NEW/a/notes.md:Zone.Identifier": "no-receipt",
            "README.md": "no-receipt",
            "tools/helper/run.py": "no-receipt",
            ".git/HEAD": "git-internal",
            "link.md": "symlink",
        })
        notes = manifest["INBOX/01-NEW/a/notes.md"]
        self.assertEqual((notes["size"], notes["sha256"]), (14, digest(b"# Notes\nalpha\n")))
        self.assertRegex(notes["receipt"], r"\AD\d+\Z")
        self.assertEqual(manifest["INBOX/90-DUP/copy-of-notes.md"]["receipt"], notes["receipt"])
        self.assertEqual(manifest["link.md"]["target"], "README.md")
        self.assertIsNone(manifest["link.md"]["sha256"])
        self.assertEqual(manifest["INBOX/01-NEW/drop/bundle.zip"]["archive"]["members"], 1)
        self.assertEqual(status, 1)
        self.assertEqual(summary["counts"]["files"], 12)
        self.assertEqual(summary["counts"]["bytes"], sum(e["size"] for e in manifest.values()))
        self.assertEqual(summary["categories"]["no-receipt"]["files"], 4)
        self.assertEqual(summary["unresolved"], 6)
        self.assertFalse(summary["ready"])

    def test_explicit_dispositions_resolve_gaps_but_never_override_a_receipt(self):
        rules = {"schema": "retirement-dispositions/1", "rules": [
            {"match": {"suffix": ".mp4"}, "disposition": "deferred-local-copy", "reason": "video, not reviewed"},
            {"match": {"suffix": ":Zone.Identifier"}, "disposition": "stream-sidecar", "reason": "sidecar"},
            {"match": {"prefix": "tools/"}, "disposition": "tooling", "reason": "legacy tooling"},
            {"match": {"path": "README.md"}, "disposition": "tooling", "reason": "readme"},
            {"match": {"sha256": digest(b"# Changed\nafter intake\n")}, "disposition": "superseded",
             "reason": "later version"},
            {"match": {"path": "INBOX/01-NEW/drop/other.zip"}, "disposition": "owner-waived", "reason": "waived"},
            {"match": {"prefix": "INBOX/"}, "disposition": "bogus", "reason": "must not win over a receipt"},
        ]}
        status, summary, manifest = self.build(rules)
        self.assertEqual(manifest["INBOX/01-NEW/video.mp4"]["category"], "disposition:deferred-local-copy")
        self.assertEqual(manifest["INBOX/01-NEW/video.mp4"]["reason"], "video, not reviewed")
        self.assertEqual(manifest["tools/helper/run.py"]["category"], "disposition:tooling")
        self.assertEqual(manifest["INBOX/01-NEW/changed.md"]["category"], "disposition:superseded")
        self.assertEqual(manifest["INBOX/01-NEW/drop/other.zip"]["category"], "disposition:owner-waived")
        self.assertEqual(manifest["INBOX/01-NEW/a/notes.md"]["category"], "receipt-verified")
        self.assertEqual(manifest["INBOX/01-NEW/drop/bundle.zip"]["category"], "archive-expanded")
        self.assertEqual((summary["unresolved"], summary["ready"], status), (0, True, 0))

    def test_bad_rule_files_are_refused(self):
        for rules in ({"schema": "x", "rules": []},
                      {"schema": "retirement-dispositions/1", "rules": [{"match": {}, "disposition": "d", "reason": "r"}]},
                      {"schema": "retirement-dispositions/1",
                       "rules": [{"match": {"prefix": "a", "suffix": "b"}, "disposition": "d", "reason": "r"}]},
                      {"schema": "retirement-dispositions/1",
                       "rules": [{"match": {"path": "a"}, "disposition": "d"}]}):
            with self.subTest(rules=rules), self.assertRaises(retirement.RetirementError):
                self.build(rules)

    def test_run_is_read_only_and_output_is_private(self):
        before = snapshot(self.tree, self.root)
        self.build()
        self.assertEqual(snapshot(self.tree, self.root), before)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)
        for path in self.out.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)

    def test_unreadable_and_changing_files_are_flagged_not_skipped(self):
        locked = self.tree / "locked.bin"
        locked.write_bytes(b"x")
        locked.chmod(0)
        if os.access(locked, os.R_OK):
            self.skipTest("running with permission override")
        self.addCleanup(locked.chmod, 0o600)
        _, summary, manifest = self.build()
        self.assertEqual(manifest["locked.bin"]["category"], "unreadable")
        self.assertIsNone(manifest["locked.bin"]["sha256"])
        self.assertEqual(summary["categories"]["unreadable"]["files"], 1)
        self.assertFalse(summary["ready"])

    def test_output_inside_the_tree_or_over_an_existing_run_is_refused(self):
        self.out = self.tree / "out"
        with self.assertRaises(retirement.RetirementError) as caught:
            self.build()
        self.assertEqual(str(caught.exception), "output_inside_tree")
        self.assertFalse(self.out.exists())

    def test_missing_stores_are_refused_rather_than_reported_as_all_unregistered(self):
        (self.root / policy.storage_roots(self.root)["catalog_home"] / "catalog.sqlite3").unlink()
        with self.assertRaises(retirement.RetirementError) as caught:
            self.build()
        self.assertEqual(str(caught.exception), "catalog_unavailable")


class VerifyTests(RetirementCase):
    def verify(self, copy):
        if not hasattr(self, "summary"):
            self.summary = self.build()[1]
        summary = self.summary
        (Path(self.temp.name) / "verify.json").unlink(missing_ok=True)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = retirement.main(["verify", "--manifest", summary["manifest"], "--tree", str(copy),
                                      "--report", str(Path(self.temp.name) / "verify.json")])
        return status, json.loads(stdout.getvalue())

    def copy(self):
        import shutil
        copy = Path(self.temp.name) / "restored"
        shutil.copytree(self.tree, copy, symlinks=True)
        return copy

    def test_an_exact_copy_verifies_and_every_difference_is_counted(self):
        copy = self.copy()
        status, report = self.verify(copy)
        self.assertEqual(status, 0)
        self.assertEqual(report["mismatches"], 0)
        self.assertEqual(report["entries"], report["matched"])
        (copy / "README.md").write_bytes(b"# tampered\n")
        (copy / "tools/helper/run.py").write_bytes(b"print(2)\n")
        (copy / "INBOX/01-NEW/video.mp4").unlink()
        (copy / "extra.txt").write_bytes(b"new")
        (copy / "link.md").unlink()
        os.symlink("elsewhere", copy / "link.md")
        status, report = self.verify(copy)
        self.assertEqual(status, 1)
        self.assertEqual({k: report[k] for k in ("hash_mismatch", "size_mismatch", "missing", "extra", "link_mismatch")},
                         {"hash_mismatch": 1, "size_mismatch": 1, "missing": 1, "extra": 1, "link_mismatch": 1})
        detail = json.loads((Path(self.temp.name) / "verify.json").read_text())
        self.assertIn("INBOX/01-NEW/video.mp4", detail["missing"])
        self.assertEqual((Path(self.temp.name) / "verify.json").stat().st_mode & 0o777, 0o600)

    def test_a_tampered_manifest_is_refused(self):
        _, summary, _ = self.build()
        path = Path(summary["manifest"])
        path.write_text(path.read_text().replace('"size": 14', '"size": 15'))
        with self.assertRaises(retirement.RetirementError):
            retirement.main(["verify", "--manifest", str(path), "--tree", str(self.tree)])


if __name__ == "__main__":
    unittest.main()
