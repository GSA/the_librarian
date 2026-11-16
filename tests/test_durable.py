import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest

from tools import durable


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.target = self.root / "target"
        self.target.mkdir()
        (self.target / "file").write_bytes(b"data")

    def test_new_files_are_private_and_never_reuse_an_entry(self):
        reserved, written = self.root / "reserved", self.root / "written"
        durable.reserve(reserved)
        durable.write_new_private(written, b"bytes")
        self.assertEqual(written.read_bytes(), b"bytes")
        for path in (reserved, written):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                durable.reserve(path)
            with self.assertRaises(FileExistsError):
                durable.write_new_private(path, b"other")
        self.assertEqual(written.read_bytes(), b"bytes")

    def test_symlinks_are_refused(self):
        link_dir, link_file = self.root / "link-dir", self.root / "link-file"
        os.symlink(self.target, link_dir)
        os.symlink(self.target / "file", link_file)
        for call, path in ((durable.fsync_directory, link_dir), (durable.fsync_file, link_file),
                           (durable.sha256_file, link_file), (durable.reserve, link_file),
                           (durable.write_new_private, link_file)):
            with self.subTest(call=call.__name__), self.assertRaises(OSError):
                call(path, b"x") if call is durable.write_new_private else call(path)
        self.assertEqual((self.target / "file").read_bytes(), b"data")
        durable.fsync_directory(self.target)
        durable.fsync_file(self.target / "file")
        self.assertEqual(durable.sha256_file(self.target / "file"), hashlib.sha256(b"data").hexdigest())


if __name__ == "__main__":
    unittest.main()
