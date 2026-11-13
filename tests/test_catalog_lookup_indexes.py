"""Catalog lookup indexes replace the instr() scans without changing any decision (Kata 1h7h)."""
from contextlib import closing
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.catalog import catalog_store as catalog
from tools.catalog.contracts import CatalogError, _references, validate_graph, validate_records
from tools.diagnostics.catalog_lock_benchmark import registration
from tools.intake import store
from tests.reference_inspect import reference_inspect
from tests.support import clone_release
from tests.test_validation_cost import artifact, receipt, relationship, source

ROOT = Path(__file__).resolve().parents[1]


def new_instance(temp, name="instance", *, indexed=True):
    root = Path(temp) / name
    root.mkdir()
    store.initialize(root)
    catalog.initialize(root)
    if not indexed:
        edit(root, catalog.drop_lookup)
    return root


def edit(root, function):
    """Run ``function(connection)`` on the catalog file outside the writer, as an operator or attacker would."""
    db = catalog.locations(root)[1]
    with closing(sqlite3.connect(db, isolation_level=None)) as connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        function(connection)
    os.chmod(db, 0o600)


def submit(root, records, request_id, **kwargs):
    return catalog.submit(root, records, actor="a", request_id=request_id, reason="test",
                          expected_state=catalog.status(root)["state_token"], **kwargs)


def valid_artifact(identifier, **kwargs):
    record = artifact(identifier, **kwargs)
    record["data"]["presence"] = "present" if record["data"]["path"] else "unresolved"
    return record


def lookup_names(root):
    db = catalog.locations(root)[1]
    with closing(sqlite3.connect(db)) as connection:
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'versions_lookup_%' OR name LIKE '%refs%'")}


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def audit(self, root):
        return catalog.status(root, audit=True)


class NewCatalogs(Case):
    def test_init_creates_the_lookup_structures_and_they_verify(self):
        root = new_instance(self.temp.name)
        self.assertEqual(lookup_names(root), {f"versions_lookup_{field}" for field in catalog.LOOKUP_FIELDS}
                         | {"refs", "versions_lookup_refs"})
        submit(root, registration(0), "r0")
        self.assertEqual(self.audit(root)["revision"], 1)

    def test_schema_version_is_unchanged_by_the_lookup_structures(self):
        root = new_instance(self.temp.name)
        with closing(sqlite3.connect(catalog.locations(root)[1])) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(catalog.VERSION, 1)

    def test_the_production_statements_use_indexes_not_scans(self):
        root = new_instance(self.temp.name)
        for n in range(3):
            submit(root, registration(n), f"r{n}")
        statements = []
        with closing(catalog.connect(catalog.locations(root)[1])) as connection:
            connection.execute("BEGIN")
            connection.set_trace_callback(statements.append)
            lookups = catalog._CurrentRecords(connection)
            lookups.referrers(["S1", "A-upload-1-original"])
            lookups.claimants(["originals/uploads/s1/note.md"])
            connection.set_trace_callback(None)
            self.assertGreaterEqual(len(statements), 6)  # one probe per reference field, refs and path
            for sql in statements:
                self.assertNotIn("instr(", sql)
                if "sqlite_master" in sql:
                    continue  # the shape check read at construction
                plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql)]
                for step in plan:
                    if step.startswith("SCAN"):
                        self.fail(f"table scan in {sql}: {plan}")
                self.assertTrue(any(step.startswith("SEARCH") and ("lookup" in step or "refs" in step or "r USING PRIMARY" in step)
                                    for step in plan), plan)


class Equivalence(Case):
    """Decisions with the indexes equal the ones without them and the full validate_graph."""

    @staticmethod
    def random_record(rng, universe):
        n = rng.choice(universe)
        kind = rng.choice(("source", "source", "artifact", "artifact", "receipt", "receipt", "relationship"))
        if kind == "source":
            return source(f"S{n}")
        if kind == "receipt":
            sources = sorted({f"S{rng.choice(universe)}" for _ in range(rng.randint(1, 3))})
            return receipt(f"D{n}", sources, f"A-{rng.choice(universe)}")
        if kind == "relationship":
            return relationship(f"R-{n}", f"A-{rng.choice(universe)}", rng.choice(
                (f"A-{rng.choice(universe)}", f"S{rng.choice(universe)}")))
        return valid_artifact(f"A-{n}", path=rng.choice((None, f"p/{rng.choice(universe)}", f"é/{n}")),
                        parent=rng.choice((None, None, f"A-{rng.choice(universe)}")),
                        receipt=rng.choice((None, f"D{rng.choice(universe)}")))

    def test_random_histories_decide_alike_with_and_without_the_indexes(self):
        rng = random.Random(20260512)
        universe = list(range(1, 6))
        indexed = new_instance(self.temp.name, "indexed", indexed=True)
        legacy = new_instance(self.temp.name, "legacy", indexed=False)
        committed, accepted, refused, steps = {}, 0, 0, 0
        for step in range(260):
            batch = list({r["id"]: r for r in (self.random_record(rng, universe) for _ in range(rng.randint(1, 4)))}.values())
            merged = {**committed, **{r["id"]: r for r in batch}}
            try:
                validate_records(batch)
                validate_graph(merged)
                expected = None
            except CatalogError as error:
                expected = str(error)
            outcomes = []
            for root in (indexed, legacy):
                try:
                    submit(root, batch, f"s{step}")
                    outcomes.append(None)
                except CatalogError as error:
                    outcomes.append(str(error))
            self.assertEqual(outcomes[0], outcomes[1], (step, batch))
            self.assertEqual(outcomes[0] is None, expected is None, (step, batch, outcomes, expected))
            if expected is None:
                committed = merged
                accepted += 1
            else:
                refused += 1
            ids = {f"{p}{rng.choice(universe)}" for p in ("S", "A-", "D", "R-") for _ in range(2)}
            paths = {f"p/{rng.choice(universe)}", f"é/{rng.choice(universe)}", "p/none"}
            truth_referrers = sorted(r["id"] for r in committed.values() if ids & set(_references(r)))
            truth_claimants = sorted(r["id"] for r in committed.values()
                                     if r["kind"] == "artifact" and r["data"]["path"] in paths)
            for root in (indexed, legacy):
                with closing(catalog.connect(catalog.locations(root)[1])) as connection:
                    connection.execute("BEGIN")
                    lookups = catalog._CurrentRecords(connection)
                    self.assertEqual(sorted(r["id"] for r in lookups.referrers(ids)), truth_referrers)
                    self.assertEqual(sorted(r["id"] for r in lookups.claimants(paths)), truth_claimants)
        self.assertGreater(accepted, 40)
        self.assertGreater(refused, 40)
        for root in (indexed, legacy):
            self.assertEqual(self.audit(root)["records"], len(committed))

    def test_a_receipt_source_is_found_through_refs_and_protects_the_source(self):
        for indexed in (True, False):
            with self.subTest(indexed=indexed):
                root = new_instance(self.temp.name, f"i{indexed}", indexed=indexed)
                submit(root, registration(1), "r1")
                with closing(catalog.connect(catalog.locations(root)[1])) as connection:
                    connection.execute("BEGIN")
                    found = catalog._CurrentRecords(connection).referrers(["S1"])
                    self.assertEqual([r["id"] for r in found], ["D1"])
                # Rewriting the referenced source keeps the receipt valid and is accepted either way.
                submit(root, [source("S1")], "again")
                self.assertEqual(self.audit(root)["revision"], 2)

    def test_only_current_versions_of_a_receipt_count(self):
        for indexed in (True, False):
            with self.subTest(indexed=indexed):
                root = new_instance(self.temp.name, f"c{indexed}", indexed=indexed)
                first = [source("S1"), source("S2"), receipt("D1", ["S1"], "A-1"),
                         artifact("A-1", path="p/1", receipt="D1")]
                submit(root, first, "r1")
                submit(root, [receipt("D1", ["S2"], "A-1")], "r2")  # S1 is no longer referenced
                with closing(catalog.connect(catalog.locations(root)[1])) as connection:
                    connection.execute("BEGIN")
                    lookups = catalog._CurrentRecords(connection)
                    self.assertEqual(lookups.referrers(["S1"]), [])
                    self.assertEqual([r["id"] for r in lookups.referrers(["S2"])], ["D1"])

    def test_a_dropped_catalog_keeps_working_and_returns_the_same_results(self):
        root = new_instance(self.temp.name)
        for n in range(3):
            submit(root, registration(n), f"r{n}")
        before = catalog.read_records(root, limit=50)
        edit(root, catalog.drop_lookup)
        self.assertEqual(lookup_names(root), set())
        self.assertEqual(catalog.read_records(root, limit=50), before)
        submit(root, registration(3), "r3")
        self.assertEqual(self.audit(root)["revision"], 4)

    def test_both_shapes_pass_the_reference_inspection(self):
        for indexed in (True, False):
            root = new_instance(self.temp.name, f"r{indexed}", indexed=indexed)
            for n in range(3):
                submit(root, registration(n), f"r{n}")
            with closing(catalog.connect(catalog.locations(root)[1])) as connection:
                connection.execute("BEGIN")
                self.assertEqual(catalog.inspect(connection), reference_inspect(connection))


class TamperDetection(Case):
    """A dropped or altered lookup structure is refused by every reader and writer, never trusted."""

    def setUp(self):
        super().setUp()
        self.root = new_instance(self.temp.name)
        for n in range(3):
            submit(self.root, registration(n), f"r{n}")

    def assert_refused(self, fragment="lookup"):
        for action in (lambda: catalog.status(self.root, audit=True), lambda: catalog.status(self.root),
                       lambda: catalog.read_records(self.root, limit=5),
                       lambda: submit(self.root, registration(9), "r9")):
            with self.assertRaisesRegex(CatalogError, fragment):
                action()

    def test_each_dropped_index_is_refused(self):
        for field in catalog.LOOKUP_FIELDS:
            with self.subTest(field):
                edit(self.root, lambda c, f=field: c.execute(f"DROP INDEX versions_lookup_{f}"))
                self.assert_refused()
                catalog.migrate_indexes(self.root, rebuild=True)
                self.assertEqual(self.audit(self.root)["revision"], 3)

    def test_an_index_with_another_definition_is_refused(self):
        def alter(connection):
            connection.execute("DROP INDEX versions_lookup_path")
            connection.execute("CREATE INDEX versions_lookup_path ON versions("
                               "json_extract(payload,'$.data.path'))")  # no longer partial
        edit(self.root, alter)
        self.assert_refused()

    def test_a_dropped_refs_table_or_trigger_is_refused(self):
        for statement in ("DROP TABLE refs", "DROP TRIGGER versions_lookup_refs"):
            with self.subTest(statement):
                root = new_instance(self.temp.name, "t" + str(abs(hash(statement))))
                submit(root, registration(0), "r0")
                edit(root, lambda c, s=statement: c.execute(s))
                with self.assertRaisesRegex(CatalogError, "lookup"):
                    catalog.status(root, audit=True)

    def test_a_refs_row_deleted_added_or_changed_is_refused(self):
        cases = {
            "deleted": "DELETE FROM refs WHERE ref='S1'",
            "added": "INSERT INTO refs VALUES ('S7',1,1)",
            "added for another version": "INSERT INTO refs VALUES ('S1',2,0)",
            "changed": "UPDATE refs SET ref='S9' WHERE ref='S1'",
            "version moved": "UPDATE refs SET revision=3, ordinal=0 WHERE ref='S1'",
        }
        for label, statement in cases.items():
            with self.subTest(label):
                root = new_instance(self.temp.name, "x" + label.replace(" ", "-"))
                for n in range(3):
                    submit(root, registration(n), f"r{n}")
                edit(root, lambda c, s=statement: (c.execute("PRAGMA foreign_keys=OFF"), c.execute(s)))
                with self.assertRaises(CatalogError):
                    catalog.status(root, audit=True)
                with self.assertRaises(CatalogError):
                    catalog.status(root)
                with self.assertRaises(CatalogError):
                    submit(root, registration(9), "r9")

    def test_a_stale_refs_row_after_a_forged_receipt_is_still_caught_by_the_audit(self):
        submit(self.root, registration(4), "r4")  # the fast path is armed
        saved = self.home_receipt()
        edit(self.root, lambda c: c.execute("DELETE FROM refs WHERE ref='S1'"))
        self.restore_receipt(saved)
        with self.assertRaises(CatalogError):
            catalog.status(self.root, audit=True)

    def home_receipt(self):
        path = catalog.locations(self.root)[0] / ".local" / "read-validation.json"
        return path, path.read_bytes()

    def restore_receipt(self, saved):
        path, data = saved
        path.write_bytes(data)

    def test_a_corrupted_index_page_is_caught_by_integrity_check(self):
        db = catalog.locations(self.root)[1]
        with closing(sqlite3.connect(db)) as connection:
            root_page = connection.execute(
                "SELECT rootpage FROM sqlite_master WHERE name='versions_lookup_path'").fetchone()[0]
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        with open(db, "r+b") as stream:
            stream.seek((root_page - 1) * page_size + 8)  # inside the b-tree page header/cells
            stream.write(b"\xff" * 64)
        with self.assertRaises((CatalogError, sqlite3.DatabaseError)):
            catalog.status(self.root, audit=True)


class Migration(Case):
    def legacy_with_history(self, count=4, name="instance"):
        root = new_instance(self.temp.name, name, indexed=False)
        for n in range(count):
            submit(root, registration(n), f"r{n}")
        submit(root, [receipt("D1", ["S0", "S1"], "A-upload-1-original")], "rewrite")
        return root

    def test_a_legacy_catalog_reads_writes_and_audits_until_it_is_migrated(self):
        root = self.legacy_with_history()
        self.assertEqual(lookup_names(root), set())
        self.assertEqual(self.audit(root)["revision"], 5)
        self.assertEqual(catalog.migrate_indexes(root, check=True)["lookup"], "legacy")

    def test_migration_adds_verified_structures_without_changing_authority_data(self):
        root = self.legacy_with_history()
        db = catalog.locations(root)[1]
        with closing(sqlite3.connect(db)) as connection:
            authority = [connection.execute(f"SELECT * FROM {t} ORDER BY 1,2").fetchall()
                         for t in ("revisions", "versions", "current_records", "metadata")]
        before = catalog.status(root, audit=True)
        result = catalog.migrate_indexes(root)
        self.assertTrue(result["migrated"])
        self.assertEqual(result["state"], before)
        self.assertEqual(self.audit(root), before)
        self.assertEqual(catalog.migrate_indexes(root, check=True)["lookup"], "indexed")
        with closing(sqlite3.connect(db)) as connection:
            self.assertEqual(authority, [connection.execute(f"SELECT * FROM {t} ORDER BY 1,2").fetchall()
                                         for t in ("revisions", "versions", "current_records", "metadata")])
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], catalog.VERSION)
            refs = connection.execute("SELECT * FROM refs ORDER BY ref,revision,ordinal").fetchall()
        self.assertIn(("S0", 5, 0), refs)  # the rewritten receipt's version carries both sources
        self.assertIn(("S1", 5, 0), refs)
        self.assertIn(("S1", 2, 1), refs)  # and the superseded version keeps its own rows

    def test_migration_takes_a_verified_snapshot_first_and_attests_the_new_bytes(self):
        root = self.legacy_with_history()
        result = catalog.migrate_indexes(root)
        self.assertTrue(Path(result["backup"]["snapshot"]).is_file())
        self.assertIn("pre-index-migration", result["backup"]["snapshot"])
        self.assertEqual(catalog.verify_backup(Path(result["backup"]["snapshot"]))["revision"], 5)
        with patch.object(catalog, "inspect", side_effect=AssertionError("replayed")):
            catalog.status(root)  # the receipt written after the migration vouches for the new file

    def test_migration_is_idempotent(self):
        root = self.legacy_with_history()
        catalog.migrate_indexes(root)
        names = lookup_names(root)
        second = catalog.migrate_indexes(root)
        self.assertFalse(second["migrated"])
        self.assertEqual(lookup_names(root), names)
        self.assertEqual(self.audit(root)["revision"], 5)

    def test_a_catalog_migrated_then_written_matches_one_written_then_migrated(self):
        first = self.legacy_with_history(name="first")
        second = self.legacy_with_history(name="second")
        catalog.migrate_indexes(first)
        for root in (first, second):
            submit(root, registration(7), "later")
        catalog.migrate_indexes(second)
        dumps = []
        for root in (first, second):
            with closing(sqlite3.connect(catalog.locations(root)[1])) as connection:
                dumps.append(connection.execute("SELECT * FROM refs ORDER BY ref,revision,ordinal").fetchall())
        self.assertEqual(dumps[0], dumps[1])

    def test_a_failed_migration_leaves_the_legacy_catalog_untouched(self):
        root = self.legacy_with_history()
        db = catalog.locations(root)[1]
        before = db.read_bytes()
        real = catalog.inspect
        calls = []

        def fail_on_the_final_check(connection, *args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise CatalogError("injected")
            return real(connection, *args, **kwargs)
        with patch.object(catalog, "inspect", fail_on_the_final_check):
            with self.assertRaisesRegex(CatalogError, "injected"):
                catalog.migrate_indexes(root)
        self.assertEqual(lookup_names(root), set())
        self.assertEqual(db.read_bytes(), before)
        self.assertTrue(catalog.migrate_indexes(root)["migrated"])

    def test_a_process_killed_mid_migration_leaves_a_catalog_that_recovers_to_legacy(self):
        root = self.legacy_with_history()
        script = (
            "import os, sys\nfrom pathlib import Path\nsys.path.insert(0, %r)\n"
            "from tools.catalog import catalog_store as c\n"
            "real_connect, real_create = c.connect, c.create_lookup\n"
            "def connect(path, write=False):\n"
            "    connection = real_connect(path, write)\n"
            "    connection.execute('PRAGMA cache_size=2')\n"  # spill pages so a journal exists at the kill
            "    return connection\n"
            "def create(connection):\n    real_create(connection)\n    os._exit(9)\n"
            "c.connect, c.create_lookup = connect, create\n"
            "c.migrate_indexes(Path(%r))\n" % (str(ROOT), str(root)))
        self.assertEqual(subprocess.run([sys.executable, "-c", script], cwd=ROOT).returncode, 9)
        db = catalog.locations(root)[1]
        # SQLite rolls a hot journal back when the file is next opened; that is the documented recovery.
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        self.assertFalse(Path(str(db) + "-journal").exists())
        self.assertEqual(lookup_names(root), set())
        self.assertEqual(self.audit(root)["revision"], 5)
        self.assertTrue(catalog.migrate_indexes(root)["migrated"])
        self.assertEqual(self.audit(root)["revision"], 5)

    def test_a_tampered_legacy_catalog_is_refused_not_indexed(self):
        root = self.legacy_with_history()
        edit(root, lambda c: ([c.execute(f"DROP TRIGGER immutable_versions_{a}") for a in ("update", "delete")],
                              c.execute("UPDATE versions SET payload='{' WHERE revision=1 AND ordinal=0")))
        with self.assertRaises((CatalogError, sqlite3.Error)):
            catalog.migrate_indexes(root)
        self.assertEqual(lookup_names(root), set())

    def test_a_half_present_set_is_refused_until_rebuilt(self):
        root = self.legacy_with_history()
        catalog.migrate_indexes(root)
        edit(root, lambda c: c.execute("DROP INDEX versions_lookup_to_id"))
        with self.assertRaisesRegex(CatalogError, "rebuild"):
            catalog.migrate_indexes(root)
        result = catalog.migrate_indexes(root, rebuild=True)
        self.assertTrue(result["migrated"])
        self.assertEqual(self.audit(root)["revision"], 5)

    def test_rebuild_refuses_when_the_rest_of_the_catalog_is_damaged(self):
        root = self.legacy_with_history()
        catalog.migrate_indexes(root)
        edit(root, lambda c: ([c.execute(f"DROP TRIGGER immutable_revisions_{a}") for a in ("update", "delete")],
                              c.execute("UPDATE revisions SET actor='mallory' WHERE revision=2"),
                              c.execute("DROP INDEX versions_lookup_to_id")))
        with self.assertRaises(CatalogError):
            catalog.migrate_indexes(root, rebuild=True)
        with closing(sqlite3.connect(catalog.locations(root)[1])) as connection:  # the drop was rolled back too
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'versions_lookup_%'").fetchone()[0], 6)

    def test_restoring_an_older_snapshot_yields_a_working_legacy_catalog_that_migrates_again(self):
        root = self.legacy_with_history(2)
        old = catalog.backup(root)
        catalog.migrate_indexes(root)
        submit(root, registration(8), "after")
        self.assertEqual(catalog.restore(root, Path(old["snapshot"]), catalog.status(root)["state_token"])["revision"], 2 + 1)
        self.assertEqual(lookup_names(root), set())
        self.assertEqual(self.audit(root)["revision"], 3)
        submit(root, registration(9), "post-restore")
        catalog.migrate_indexes(root)
        self.assertEqual(self.audit(root)["revision"], 4)

    def test_the_command_line_and_initialize_run_the_migration(self):
        kit = Path(self.temp.name) / "kit"
        clone_release(kit)
        store.initialize(kit)
        catalog.initialize(kit)
        edit(kit, catalog.drop_lookup)
        submit(kit, registration(0), "r0")
        run = lambda *argv: subprocess.run([sys.executable, *argv], cwd=kit, capture_output=True, text=True)
        checked = run("-m", "tools.catalog.catalog_store", "--root", str(kit), "migrate-indexes", "--check")
        self.assertEqual(json.loads(checked.stdout)["lookup"], "legacy", checked.stderr)
        done = run("tools/initialize.py", "--root", str(kit), "--migrate-catalog-indexes")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(json.loads(done.stdout)["migrated"])
        again = run("tools/initialize.py", "--root", str(kit), "--migrate-catalog-indexes")
        self.assertFalse(json.loads(again.stdout)["migrated"])
        self.assertEqual(lookup_names(kit), set(catalog.LOOKUP_OBJECTS))
        self.assertEqual(self.audit(kit)["revision"], 1)


if __name__ == "__main__":
    unittest.main()
