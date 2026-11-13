"""file_identity() must never drop the SQLite locks this process holds (Kata kkmv; docs/design/current-state.md, "What a catalog write validates")."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.file_identity import file_identity
from tests.test_catalog_delta_commit import Case

ROOT = Path(__file__).resolve().parents[1]

# Another process tries to take the write lock without waiting: it only succeeds if the
# connection under test no longer holds its SQLite lock at the operating-system level.
FOREIGN_WRITE = (
    "import sqlite3, sys\n"
    "c = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)\n"
    "try:\n"
    "    c.execute('BEGIN IMMEDIATE')\n"
    "    c.execute('CREATE TABLE IF NOT EXISTS foreign_marker(x)')\n"
    "    c.execute('COMMIT')\n"
    "    print('wrote')\n"
    "except sqlite3.OperationalError as error:\n"
    "    print('locked' if 'locked' in str(error) else error)\n"
)


def has_dbpage():
    with closing(sqlite3.connect(":memory:")) as connection:
        try:
            connection.execute("SELECT count(*) FROM sqlite_dbpage").fetchall()
        except sqlite3.OperationalError:
            return False
    return True


needs_dbpage = unittest.skipUnless(has_dbpage(), "SQLite built without sqlite_dbpage: identity falls back to the byte hash")


def without_dbpage(connection_class):
    """Make ``sqlite_dbpage`` unavailable on connections of this class, as in a build without it."""
    real = connection_class.execute

    def execute(self, sql, *args):
        if "sqlite_dbpage" in sql:
            raise sqlite3.OperationalError("no such table: sqlite_dbpage")
        return real(self, sql, *args)
    return patch.object(connection_class, "execute", execute)


def foreign_write(db):
    return subprocess.run([sys.executable, "-c", FOREIGN_WRITE, str(db)], check=True, cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()


class IdentityKeepsLocks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "x.sqlite3"
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("CREATE TABLE t(a)")
            connection.commit()
        self.probe = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(self.probe.close)

    def test_identity_inside_a_read_snapshot_keeps_the_read_lock(self):
        with closing(sqlite3.connect(self.db, isolation_level=None)) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM t").fetchone()
            identity = file_identity(self.db, reader)
            self.assertTrue(identity is not None or not has_dbpage())
            # A foreign commit needs EXCLUSIVE, which the reader's SHARED lock must refuse.
            self.assertEqual(foreign_write(self.db), "locked")

    def test_identity_under_a_write_transaction_keeps_the_reserved_lock(self):
        with closing(sqlite3.connect(self.db, isolation_level=None)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            identity = file_identity(self.db, writer)
            self.assertTrue(identity is not None or not has_dbpage())
            self.assertEqual(foreign_write(self.db), "locked")

    @needs_dbpage
    def test_repeated_identity_does_not_leak_descriptors(self):
        file_identity(self.db, self.probe)
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(50):
            file_identity(self.db, self.probe)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    @needs_dbpage
    def test_replaced_file_gets_its_own_identity(self):
        first = file_identity(self.db, self.probe)
        replacement = self.db.with_name("y.sqlite3")
        replacement.write_bytes(self.db.read_bytes())
        os.replace(replacement, self.db)
        second = file_identity(self.db, self.probe)
        self.assertNotEqual(first[:2], second[:2])
        self.assertEqual(second[:2], [os.stat(self.db).st_dev, os.stat(self.db).st_ino])

    @needs_dbpage
    def test_identity_follows_commits_made_by_this_process(self):
        before = file_identity(self.db, self.probe)
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("INSERT INTO t VALUES (1)")
            connection.commit()
        after = file_identity(self.db, self.probe)
        self.assertNotEqual(before, after)
        self.assertEqual(before[:2], after[:2])


class CatalogBindingKeepsLocks(Case):
    def test_without_dbpage_the_binding_is_the_byte_hash_and_keeps_the_lock(self):
        from tools.catalog import catalog_store as catalog
        self.write(0)
        with without_dbpage(catalog._Connection), closing(catalog.connect(self.db)) as reader:
            reader.execute("BEGIN")
            binding = catalog._binding(reader)
            self.assertEqual(binding["version"], 1)
            reader.execute("SELECT count(*) FROM current_records").fetchone()
            self.assertEqual(foreign_write(self.db), "locked")


    def test_binding_inside_a_read_snapshot_keeps_the_read_lock(self):
        self.write(0)
        from tools.catalog import catalog_store as catalog
        with closing(catalog.connect(self.db)) as reader:
            reader.execute("BEGIN")
            catalog._binding(reader)
            reader.execute("SELECT count(*) FROM current_records").fetchone()
            self.assertEqual(foreign_write(self.db), "locked")

    def test_binding_under_the_writers_immediate_transaction_keeps_its_lock(self):
        self.write(0)
        from tools.catalog import catalog_store as catalog
        with closing(catalog.connect(self.db, write=True)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            catalog._binding(writer)
            self.assertEqual(foreign_write(self.db), "locked")


if __name__ == "__main__":
    unittest.main()
