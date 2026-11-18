"""Archive expansion into instance storage and verified deletion of archive originals (Kata 12sc)."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
import unittest.mock
import warnings
import zipfile

from tools.catalog import catalog_store as catalog
from tools.intake import archives, cohort, cohort_archives, store
from tools.intake.contracts import IntakeError


def make_zip(members):
    """A zip from ``{name: bytes}``; a ``ZipInfo`` key keeps its attributes."""
    buffer = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        warnings.simplefilter("ignore")  # duplicate names are written on purpose
        for name, raw in members:
            z.writestr(name, raw)
    return buffer.getvalue()


def encrypt_flag(raw, name):
    """Set the 'encrypted' flag of one member in both its local and central headers."""
    data = bytearray(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        info = z.getinfo(name)
    data[info.header_offset + 6] |= 0x1
    at = 0
    encoded = name.encode()
    while (at := data.find(b"PK\x01\x02", at)) >= 0:
        size = int.from_bytes(data[at + 28:at + 30], "little")
        if bytes(data[at + 46:at + 46 + size]) == encoded:
            data[at + 8] |= 0x1
        at += 4
    return bytes(data)


def symlink_info(name):
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (0o120777 << 16)
    return info


def make_tar(members, mode="w:gz"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as t:
        for info, raw in members:
            t.addfile(info, io.BytesIO(raw) if raw is not None else None)
    return buffer.getvalue()


def tar_file(name, raw):
    info = tarfile.TarInfo(name)
    info.size = len(raw)
    return info, raw


def tar_special(name, kind, linkname=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info, None


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)
        self.inbox = Path(self.temp.name) / "inbox"
        self.inbox.mkdir()

    def put(self, name, raw):
        path = self.inbox / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def tree(self):
        return {p.relative_to(self.inbox).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.inbox.rglob("*")) if p.is_file()}

    def store_tree(self):
        directory = self.root / ".local/intake" / archives.DIRECTORY
        return sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*")) if directory.exists() else []

    def entries(self):
        return {e["relative"]: e for e in cohort.classify(self.root, self.inbox)}

    def expand(self, apply=True):
        report = cohort_archives.expand(self.root, self.inbox, apply=apply)
        return report, {r["relative"]: r for r in report["entries"]}


class MemberNameTests(unittest.TestCase):
    def test_unsafe_names_are_refused_and_safe_names_normalized(self):
        for name in ("../x", "a/../../x", "/etc/passwd", "C:/x", "c:x", "a\\b", "a/b:c", "", "a/\x01",
                     "x!/y", "/", "a/" + "x" * 256):
            self.assertIsNone(archives.member_path(name), name)
        self.assertEqual(archives.member_path("./a//b/./c.txt"), "a/b/c.txt")
        self.assertEqual(archives.member_path("cafe\u0301.md"), "caf\u00e9.md")

    def test_kind_and_stem(self):
        self.assertEqual([archives.kind(n) for n in ("a.ZIP", "a.tar.gz", "a.tgz", "a.tar", "a.tar.xz", "a.gz")],
                         ["zip", "tar", "tar", "tar", "tar", None])
        self.assertEqual(archives.stem("Repo.Tar.Gz"), "Repo")


class ExpandTests(ArchiveCase):
    def hostile_zip(self):
        raw = make_zip([
            ("docs/a.md", b"# A\nalpha\n"), ("docs/A.MD", b"# collision\n"), ("docs/a.md", b"# dup\n"),
            ("../evil.md", b"x"), ("/abs.md", b"x"), ("C:/drive.md", b"x"), ("win\\path.md", b"x"),
            (symlink_info("docs/link"), b"/etc/passwd"), ("secret.md", b"hidden\n"),
            ("docs/a.md/under.md", b"x"), ("empty/", b""), ("web/app.js", b"x()"),
            ("inner.zip", make_zip([("deep.md", b"# deep\n")])),
            ("report.docx", b"PK\x03\x04 not really"),
        ])
        return encrypt_flag(raw, "secret.md")

    def test_dry_run_reads_every_member_and_writes_nothing(self):
        self.put("drop/bundle.zip", self.hostile_zip())
        before = self.tree()
        report, rows = self.expand(apply=False)
        row = rows["drop/bundle.zip"]
        self.assertEqual(row["status"], "would-expand")
        self.assertEqual(row["member_codes"], {"case-collision": 1, "duplicate-name": 1, "unsafe-name": 4, "link": 1,
                                       "encrypted": 1, "path-conflict": 1})
        self.assertEqual(row["member_dispositions"], {"extracted": 4, "refused": 8, "skipped": 1})
        self.assertEqual((row["members"], row["directories"], row["nested_archives"]), (13, 1, 1))
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.store_tree(), [])

    def test_apply_expands_by_hash_with_a_member_manifest_and_classify_reads_members(self):
        raw = self.hostile_zip()
        self.put("drop/bundle.zip", raw)
        before = self.tree()
        self.expand()
        sha = hashlib.sha256(raw).hexdigest()
        directory = self.root / ".local/intake/archives" / sha
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(manifest["archive"], {"relative": "drop/bundle.zip", "kind": "zip",
                                               "bytes": len(raw), "sha256": sha})
        stored = {m["path"]: m for m in manifest["members"] if m["disposition"] == "extracted"}
        self.assertEqual(set(stored), {"docs/a.md", "web/app.js", "inner.zip", "report.docx"})
        for path, member in stored.items():
            data = (directory / "members" / path).read_bytes()
            self.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (member["bytes"], member["sha256"]))
            self.assertEqual((directory / "members" / path).stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "evil.md").exists() or (directory / "evil.md").exists())
        self.assertEqual(self.tree(), before)
        entries = self.entries()
        self.assertEqual(entries["drop/bundle.zip"]["disposition"], "archive-expanded")
        got = {k: (e["disposition"], e.get("code")) for k, e in entries.items() if "!/" in k}
        self.assertEqual(got["drop/bundle.zip!/docs/a.md"], ("admit", None))
        self.assertEqual(got["drop/bundle.zip!/web/app.js"], ("skipped-asset", None))
        self.assertEqual(got["drop/bundle.zip!/report.docx"], ("needs-reader", None))
        self.assertEqual(got["drop/bundle.zip!/inner.zip"], ("needs-reader", "archive-not-expanded"))
        self.assertEqual(got["drop/bundle.zip!/secret.md"], ("unreadable", "archive-encrypted"))
        self.assertEqual(got["drop/bundle.zip!/docs/link"], ("unsafe-path", "archive-link"))
        self.assertEqual(got["drop/bundle.zip!/docs/A.MD"], ("unsafe-path", "archive-case-collision"))
        self.assertEqual(sum(c == "archive-unsafe-name" for _, c in got.values()), 4)
        self.assertNotIn("_source", json.dumps(list(entries.values()), default=str))
        # Idempotent by archive hash: a second run and a copy elsewhere reuse the expansion.
        self.put("copy/same.zip", raw)
        _, rows = self.expand()
        self.assertEqual({r["status"] for r in rows.values() if "!/" not in r["relative"]}, {"existing"})

    def test_nested_archives_expand_one_level_per_explicit_run(self):
        inner = make_zip([("deep.md", b"# deep\n")])
        self.put("outer.zip", make_zip([("inner.zip", inner), ("top.md", b"# top\n")]))
        _, rows = self.expand()
        self.assertEqual(rows["outer.zip"]["status"], "expanded")
        self.assertNotIn("outer.zip!/inner.zip", rows)
        _, rows = self.expand()
        self.assertEqual((rows["outer.zip"]["status"], rows["outer.zip!/inner.zip"]["status"]),
                         ("existing", "expanded"))
        entries = self.entries()
        self.assertEqual(entries["outer.zip!/inner.zip"]["disposition"], "archive-expanded")
        self.assertEqual(entries["outer.zip!/inner.zip!/deep.md"]["disposition"], "admit")

    def test_tar_links_devices_and_traversal_are_not_written(self):
        raw = make_tar([tar_file("repo/README.md", b"# Repo\n"), tar_file("../escape.md", b"x"),
                        tar_special("repo/sym", tarfile.SYMTYPE, "/etc/passwd"),
                        tar_special("repo/hard", tarfile.LNKTYPE, "repo/README.md"),
                        tar_special("repo/fifo", tarfile.FIFOTYPE), tar_special("repo/dev", tarfile.CHRTYPE),
                        tar_special("repo/sub", tarfile.DIRTYPE)])
        self.put("code/repo.tar.gz", raw)
        _, rows = self.expand()
        self.assertEqual(rows["code/repo.tar.gz"]["member_codes"], {"unsafe-name": 1, "link": 2, "special": 2})
        self.assertEqual(rows["code/repo.tar.gz"]["member_dispositions"], {"extracted": 1, "refused": 1, "skipped": 4})
        members = self.root / ".local/intake/archives" / hashlib.sha256(raw).hexdigest() / "members"
        self.assertEqual(sorted(p.relative_to(members).as_posix() for p in members.rglob("*")),
                         ["repo", "repo/README.md"])
        self.assertEqual(self.entries()["code/repo.tar.gz!/repo/README.md"]["disposition"], "admit")

    def test_bomb_caps_refuse_the_whole_archive_and_store_no_members(self):
        bomb = make_zip([("zeros.txt", b"\0" * 400_000)])
        many = make_tar([tar_file("f%d.md" % i, b"x\n") for i in range(5)])
        self.put("bomb.zip", bomb)
        self.put("many.tar", many)
        self.put("broken.zip", b"PK\x03\x04")
        with unittest.mock.patch.object(archives, "RATIO_FLOOR", 1000), \
                unittest.mock.patch.object(archives, "MAX_MEMBERS", 4):
            _, rows = self.expand()
        self.assertEqual({k: (r["status"], r["code"]) for k, r in rows.items()},
                         {"bomb.zip": ("refused", "ratio-cap"), "many.tar": ("refused", "too-many-members"),
                          "broken.zip": ("refused", "archive-unreadable")})
        self.assertFalse(any("members" in p for p in self.store_tree()))
        entries = self.entries()
        self.assertEqual((entries["bomb.zip"]["disposition"], entries["bomb.zip"]["code"]),
                         ("needs-reader", "archive-ratio-cap"))
        with unittest.mock.patch.object(archives, "MAX_EXPANDED_BYTES", 1000):
            manifest = archives.read(self.inbox / "bomb.zip", "bomb.zip")
        self.assertEqual((manifest["status"], manifest["code"]), ("refused", "expanded-size-cap"))

    def test_compressed_tar_payloads_count_against_the_caps_even_when_refused(self):
        for name in ("../zeros.md", "a/zeros.md"):
            raw = make_tar([tar_file(name, b"\0" * 400_000), tar_file("ok.md", b"# ok\n")])
            for patch, code in (("RATIO_FLOOR", "ratio-cap"), ("MAX_EXPANDED_BYTES", "expanded-size-cap")):
                with self.subTest(name=name, code=code), unittest.mock.patch.object(archives, patch, 1000), \
                        unittest.mock.patch.object(archives, "TAR_OVERHEAD_PER_MEMBER", 0):
                    self.put("t.tar.gz", raw)
                    manifest = archives.read(self.inbox / "t.tar.gz", "t.tar.gz")
                    self.assertEqual((manifest["status"], manifest["code"]), ("refused", code))
        for mode in ("w:bz2", "w:xz", "w:"):
            raw = make_tar([tar_file("a.md", b"# a\n")], mode=mode)
            self.put("t.tar", raw)
            self.assertEqual(archives.read(self.inbox / "t.tar", "t.tar")["members"][0]["disposition"], "extracted")

    def test_sibling_folder_is_verified_by_hash_and_classify_is_unchanged(self):
        self.put("site.zip", make_zip([("a.md", b"# A\n"), ("b.md", b"# B\n"), ("c.md", b"# C\n")]))
        self.put("_extracted/site/a.md", b"# A\n")
        self.put("_extracted/site/b.md", b"# changed\n")
        before_entries = self.entries()
        _, rows = self.expand(apply=False)
        self.assertEqual(rows["site.zip"]["status"], "would-verify")
        self.assertEqual(rows["site.zip"]["member_codes"], {"pre-expanded-differs": 1, "pre-expanded-missing": 1})
        _, rows = self.expand()
        self.assertEqual(rows["site.zip"]["status"], "pre-expanded")
        manifest = archives.load(self.root / ".local/intake/archives", rows["site.zip"]["sha256"])
        self.assertEqual([(m["path"], m["disposition"], m.get("source")) for m in manifest["members"]],
                         [("a.md", "pre-expanded", "_extracted/site/a.md"), ("b.md", "refused", None),
                          ("c.md", "refused", None)])
        self.assertEqual(self.entries(), before_entries)
        self.assertEqual(self.store_tree().count(rows["site.zip"]["sha256"] + "/members"), 0)

    def test_cli_prints_counts_and_refuses_an_instance_inside_the_inbox(self):
        self.put("a.zip", make_zip([("a.md", b"# A\n")]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cohort.main(["expand-archives", "--root", str(self.root),
                                          "--inbox", str(self.inbox)]), 0)
        self.assertEqual(json.loads(out.getvalue())["counts"]["status"], {"would-expand": 1})
        with self.assertRaises(IntakeError) as caught:
            cohort_archives.expand(self.root, self.root.parent, apply=True)
        self.assertEqual(caught.exception.code, "instance_inside_inbox")


class IngestMemberTests(ArchiveCase):
    def test_members_are_ingested_with_archive_provenance_and_rerun_is_idempotent(self):
        self.put("drop/bundle.zip", make_zip([("docs/a.md", b"# A\nalpha\n"), ("web/app.js", b"x()")]))
        self.expand()
        report = cohort.ingest(self.root, self.inbox, system="inbox", actor="runner")
        entry = next(e for e in report["entries"] if e["relative"] == "drop/bundle.zip!/docs/a.md")
        self.assertEqual(entry["outcome"], "extracted")
        self.assertEqual(entry["locator"], "drop/bundle.zip%21/docs/a.md")
        sidecar = json.loads((self.root / Path(entry["original"]).parent / "submission.json").read_text())
        self.assertEqual(sidecar["origin"]["locator"], "drop/bundle.zip%21/docs/a.md")
        self.assertEqual(sidecar["original"]["sha256"], hashlib.sha256(b"# A\nalpha\n").hexdigest())
        self.assertNotIn("_source", json.dumps(report, default=str))
        stored = (self.root / ".local/intake/archives" / hashlib.sha256((self.inbox / "drop/bundle.zip").read_bytes())
                  .hexdigest() / "members")
        (stored / "web/app.js").write_bytes(b"y()")
        (stored / "docs/a.md").write_bytes(b"")
        entries = self.entries()
        self.assertEqual({k: (entries[k]["disposition"], entries[k]["code"]) for k in
                          ("drop/bundle.zip!/web/app.js", "drop/bundle.zip!/docs/a.md")},
                         {"drop/bundle.zip!/web/app.js": ("unsafe-path", "archive-member-changed"),
                          "drop/bundle.zip!/docs/a.md": ("unsafe-path", "archive-member-changed")})
        (stored / "web/app.js").write_bytes(b"x()")
        (stored / "docs/a.md").write_bytes(b"# A\nalpha\n")
        again = cohort.ingest(self.root, self.inbox, system="inbox", actor="runner")
        self.assertEqual(again["counts"]["outcome"], report["counts"]["outcome"])
        trusted = cohort.ingest(self.root, self.inbox, system="inbox", actor="runner", trust_stat=True)
        self.assertEqual(trusted["counts"]["outcome"]["extracted"], 1)


class DeleteOriginalTests(ArchiveCase):
    def setUp(self):
        super().setUp()
        self.raw = make_zip([("docs/a.md", b"# A\n"), ("docs/b.txt", b"plain\n"), ("web/app.js", b"x()"),
                             (symlink_info("docs/link"), b"a.md"), ("blank.md", b"")])
        self.put("drop/bundle.zip", self.raw)

    def delete(self, apply=False, actor="operator"):
        return cohort_archives.delete_original(self.root, self.inbox, "drop/bundle.zip", actor=actor, apply=apply)

    def deletions(self):
        path = self.root / ".local/intake/archives/deletions.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_refused_until_expanded_and_every_member_registered(self):
        with self.assertRaises(IntakeError) as caught:
            self.delete()
        self.assertEqual(caught.exception.code, "archive_not_expanded")
        self.expand()
        (self.inbox / "drop/other.zip").write_bytes(make_zip([("x.md", b"# x\n")]))
        report = cohort_archives.delete_original(self.root, self.inbox, "drop/other.zip")
        self.assertEqual((report["status"], report["blockers"][0]["code"]), ("blocked", "archive-not-expanded"))
        report = self.delete(apply=True)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual({b["member"]: (b["code"], b["disposition"]) for b in report["blockers"]},
                         {"drop/bundle.zip!/docs/a.md": ("not-registered", "admit"),
                          "drop/bundle.zip!/docs/b.txt": ("not-registered", "admit")})
        self.assertTrue((self.inbox / "drop/bundle.zip").exists())
        self.assertEqual(self.deletions(), [])

    def test_verified_dry_run_then_apply_records_before_deleting_and_is_idempotent(self):
        self.expand()
        log = self.root / ".local/intake/archives/deletions.jsonl"
        log.write_bytes(b'{"inbox": "/elsewhere", "relative": "earlier.zip", "sha256": "0"}\n{"relative": "torn')
        cohort.ingest(self.root, self.inbox, system="inbox", actor="runner")
        report = self.delete()
        self.assertEqual((report["status"], report["registered"], report["blockers"]), ("verified", 2, []))
        self.assertTrue((self.inbox / "drop/bundle.zip").exists())
        with self.assertRaises(IntakeError) as caught:
            self.delete(apply=True, actor=None)
        self.assertEqual(caught.exception.code, "actor_required")
        real_unlink, calls = os.unlink, []

        def crash(path, *args, **kwargs):
            calls.append(path)
            raise OSError("simulated crash after the record")
        with unittest.mock.patch.object(cohort_archives.os, "unlink", crash):
            with self.assertRaises(OSError):
                self.delete(apply=True)
        self.assertEqual(len(self.deletions()), 2)
        self.assertTrue((self.inbox / "drop/bundle.zip").exists())
        self.assertIs(os.unlink, real_unlink)
        report = self.delete(apply=True)
        self.assertEqual((report["status"], report["deleted"]), ("deleted", True))
        self.assertFalse((self.inbox / "drop/bundle.zip").exists())
        earlier, record = self.deletions()
        self.assertEqual(earlier["relative"], "earlier.zip")
        sha = hashlib.sha256(self.raw).hexdigest()
        manifest = self.root / ".local/intake/archives" / sha / "manifest.json"
        self.assertEqual({k: record[k] for k in ("relative", "bytes", "sha256", "manifest_sha256", "actor")},
                         {"relative": "drop/bundle.zip", "bytes": len(self.raw), "sha256": sha,
                          "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                          "actor": "operator"})
        self.assertEqual(self.delete(apply=True)["status"], "already-deleted")
        self.assertEqual(len(self.deletions()), 2)

    def test_changed_stored_member_unsupported_member_and_nested_archive_block(self):
        self.expand()
        cohort.ingest(self.root, self.inbox, system="inbox", actor="runner")
        member = (self.root / ".local/intake/archives" / hashlib.sha256(self.raw).hexdigest()
                  / "members/docs/b.txt")
        member.write_bytes(b"tampered\n")
        report = self.delete(apply=True)
        self.assertEqual(report["blocked_by_code"], {"member-changed": 1})
        self.assertTrue((self.inbox / "drop/bundle.zip").exists())
        raw = encrypt_flag(make_zip([("a.md", b"# A\n"), ("s.md", b"x"), ("r.docx", b"PK"),
                                     ("in.zip", make_zip([("d.md", b"# d\n")]))]), "s.md")
        self.put("drop/mixed.zip", raw)
        self.expand()
        cohort.ingest(self.root, self.inbox, system="inbox", actor="runner")
        report = cohort_archives.delete_original(self.root, self.inbox, "drop/mixed.zip")
        self.assertEqual({b["member"]: (b["code"], b["disposition"]) for b in report["blockers"]},
                         {"drop/mixed.zip!/s.md": ("encrypted", None),
                          "drop/mixed.zip!/in.zip": ("nested-not-expanded", None),
                          "drop/mixed.zip!/r.docx": ("not-registered", "needs-reader")})


if __name__ == "__main__":
    unittest.main()
