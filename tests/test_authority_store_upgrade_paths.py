"""Authority stores are never rebuilt or silently migrated by upgrade, init or recover paths.

A populated catalog or review-event store whose version or contract marker no longer matches the
installed code must make every entry point refuse explicitly and must leave every file of the
instance (stores, backups, bindings, index) byte-identical. Fixtures are synthetic release copies in
temporary directories. The review-event ``contract_hash`` refusal itself is pinned in
``tests/test_review_event_contract.py``; this file checks the surrounding upgrade, init and
recovery entry points against it as one more marker.
"""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from tests import test_kit
from tests.support import clone_release
from tools import policy
from tools.catalog import catalog_store as catalog
from tools.review_events import event_store as events

CATALOG_USER_VERSION = "Unsupported catalog version"
CATALOG_MARKER = "Catalog contract mismatch"
REVIEW_USER_VERSION = "Unsupported store version"
REVIEW_MARKER = "Stored contract differs from installed contract; explicit migration required"


def tree(root):
    """Relative path -> content digest for every file under the instance (caches excluded)."""
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(root).rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts}


def edit(db, *statements):
    with closing(sqlite3.connect(db, isolation_level=None)) as connection:
        for statement in statements:
            connection.execute(*statement)


def bump_user_version(store):
    def apply(db):
        edit(db, ("PRAGMA user_version=2",))
    return apply


def retag_catalog_contract(db):
    edit(db, ("DROP TRIGGER immutable_metadata_update",),
         ("UPDATE metadata SET value='2' WHERE key='contract_version'",),
         (catalog.trigger_sql("metadata", "update"),))


def retag_review_contract(db):
    edit(db, ("DROP TRIGGER immutable_metadata_update",),
         ("UPDATE metadata SET value=? WHERE key='contract_hash'", ("0" * 64,)),
         (events.trigger_sql("metadata", "update"),))


# name -> (store, tamper, expected refusal). "catalog"/"review" pick the store the marker lives in.
VARIANTS = {
    "catalog user_version": ("catalog", bump_user_version("catalog"), CATALOG_USER_VERSION),
    "catalog contract_version": ("catalog", retag_catalog_contract, CATALOG_MARKER),
    "review user_version": ("review", bump_user_version("review"), REVIEW_USER_VERSION),
    "review contract_hash": ("review", retag_review_contract, REVIEW_MARKER),
}


class AuthorityStoreUpgradePaths(unittest.TestCase):
    def build(self, *, roots=None):
        """A populated release copy; returns (root, aux) after catalog and review events hold data."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        base = Path(scratch.name)
        root, aux = base / "instance", base / "aux"
        root.mkdir()
        aux.mkdir()
        clone_release(root)
        if roots is not None:
            (root / "config/policy.local.json").write_text(json.dumps({"storage_roots": roots}))
        self.run_cli(root, "tools/initialize.py", check=True)
        state = catalog.status(root)
        catalog.submit(root, [{"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}}],
                       actor="test-runner", request_id="req-populate", expected_state=state["state_token"],
                       reason="Populate the synthetic catalog")
        pending = test_kit.WorkspaceCase.pending(type("Stub", (), {"root": root})())
        review = events.status(root)
        events.append(root, events.bundle([pending]), "test-runner", "req-populate",
                      review["revision"], review["state_token"])
        self.assertEqual((catalog.status(root)["revision"], events.status(root)["events"]), (1, 1))
        self.pending = pending
        return root, aux

    @staticmethod
    def run_cli(root, *argv, check=False):
        result = subprocess.run([sys.executable, *argv], cwd=root, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL)
        if check:
            assert result.returncode == 0, result.stderr
        return result

    def assert_refused_and_untouched(self, root, argv, refusal, label):
        before = tree(root)
        result = self.run_cli(root, *argv)
        self.assertNotEqual(result.returncode, 0, f"{label}: {result.stdout}")
        self.assertIn(refusal, result.stderr + result.stdout, label)
        self.assertNotIn("Traceback", result.stderr, label)
        self.assertEqual(tree(root), before, f"{label}: instance files changed")

    def commands(self, root, aux, store, refusal):
        """Every CLI that opens the store under test, upgrade/init/recover paths first."""
        snapshot = next((root / (".local/catalog" if store == "catalog" else ".local/review-events")
                         / "backups").glob("*.sqlite3"))
        token = "0" * 64
        submission = aux / "submission.json"
        submission.write_text(json.dumps({"records": [
            {"id": "S2", "kind": "source", "data": {"category": "artifact", "provenance": []}}]}))
        bundle = aux / "bundle.json"
        bundle.write_text(json.dumps(events.bundle([dict(self.pending, event_id="rev-second")])))
        module = {"catalog": "tools.catalog.catalog_store", "review": "tools.review_events.event_store"}[store]
        yield ("initialize", ("tools/initialize.py",))
        yield ("initialize --recover-migration", ("tools/initialize.py", "--recover-migration"))
        yield ("initialize --relocate", ("tools/initialize.py", "--relocate"))
        for name in ("init", "status", "audit", "backup"):
            yield (f"{store} {name}", ("-m", module, "--root", str(root), name))
        yield (f"{store} restore", ("-m", module, "--root", str(root), "restore", str(snapshot),
                                    "--expected-state", token))
        if store == "catalog":
            for name in ("query", "export"):
                yield (f"catalog {name}", ("-m", module, "--root", str(root), name))
            yield ("catalog submit", ("-m", module, "--root", str(root), "submit", str(submission),
                                      "--actor", "test-runner", "--request-id", "req-refused",
                                      "--expected-state", token, "--reason", "Must be refused"))
            yield ("context-index catalog", ("-m", "tools.context_index.cli", "--root", str(root), "catalog"))
        else:
            yield ("review get", ("-m", module, "--root", str(root), "get", "rev-pending"))
            yield ("review append", ("-m", module, "--root", str(root), "append", str(bundle),
                                     "--actor", "test-runner", "--request-id", "req-refused",
                                     "--expected-revision", "1", "--expected-state", token))
            for name in ("query", "export"):
                yield (f"review reader {name}", ("-m", "tools.review_events.event_reader", "--root",
                                                 str(root), name))

    def test_every_entry_point_refuses_and_leaves_the_instance_byte_identical(self):
        for name, (store, tamper, refusal) in VARIANTS.items():
            with self.subTest(variant=name):
                root, aux = self.build()
                db = root / (".local/catalog/catalog.sqlite3" if store == "catalog"
                             else ".local/review-events/events.sqlite3")
                commands = list(self.commands(root, aux, store, refusal))
                tamper(db)
                tampered = tree(root)
                for label, argv in commands:
                    # `init` refuses an existing store before opening it; recovery with no journal
                    # has nothing to roll back. Neither may "repair" or rebuild the store.
                    expected = ("No interrupted store migration to recover" if "recover" in label
                                else "never overwrites" if label.endswith(" init") else refusal)
                    self.assert_refused_and_untouched(root, argv, expected, f"{name}: {label}")
                self.assertEqual(tree(root), tampered)
                self.assertTrue(db.read_bytes())  # still the populated store, not an empty replacement

    def test_library_init_and_restore_refuse_without_touching_the_store(self):
        for name, (store, tamper, refusal) in VARIANTS.items():
            with self.subTest(variant=name):
                root, _ = self.build()
                module = catalog if store == "catalog" else events
                db = module.locations(root)[1]
                snapshot = next(module.locations(root)[2].glob("*.sqlite3"))
                tamper(db)
                tampered = tree(root)
                with self.assertRaises(ValueError) as context:
                    module.initialize(root)
                self.assertIn("never overwrite", str(context.exception))
                with self.assertRaisesRegex(ValueError, refusal):
                    module.restore(root, snapshot, "0" * 64)
                with self.assertRaisesRegex(ValueError, refusal):
                    module.status(root)
                self.assertEqual(tree(root), tampered)

    def test_intake_progress_reports_a_refused_catalog_and_does_not_touch_it(self):
        for name, (store, tamper, refusal) in VARIANTS.items():
            if store != "catalog":
                continue
            with self.subTest(variant=name):
                root, _ = self.build()
                tamper(catalog.locations(root)[1])
                before = tree(root)
                result = self.run_cli(root, "-m", "tools.intake.cli", "--root", str(root), "progress")
                report = json.loads(result.stdout)
                self.assertFalse(report["catalog"]["available"])
                self.assertEqual(report["catalog"]["detail"], refusal)
                self.assertEqual(tree(root), before)

    def test_derived_index_build_never_rewrites_a_changed_authority_store(self):
        for name, (store, tamper, refusal) in VARIANTS.items():
            with self.subTest(variant=name):
                root, _ = self.build()
                module = catalog if store == "catalog" else events
                tamper(module.locations(root)[1])
                authority = {path: path.read_bytes() for home in (catalog.locations(root)[0],
                                                                  events.locations(root)[0])
                             for path in home.rglob("*") if path.is_file()}
                for command in ("build", "status"):
                    self.run_cli(root, "-m", "tools.context_index.context_index", command)
                self.assertEqual({path: path.read_bytes() for path in authority}, authority, name)

    def test_storage_migration_refuses_a_changed_store_before_moving_anything(self):
        for name, (store, tamper, refusal) in VARIANTS.items():
            with self.subTest(variant=name):
                root, _ = self.build()
                module = catalog if store == "catalog" else events
                tamper(module.locations(root)[1])
                (root / "config/policy.local.json").write_text(json.dumps({"storage_roots": {
                    "catalog_home": ".local/moved/catalog", "review_events_home": ".local/moved/review-events"}}))
                self.assert_refused_and_untouched(root, ("tools/initialize.py", "--migrate-storage"),
                                                  refusal, f"{name}: --migrate-storage")
                self.assertFalse((root / ".local/migration.json").exists())

    def test_recover_migration_rolls_files_back_but_never_repairs_a_changed_store(self):
        old = {"catalog_home": ".local/catalog", "review_events_home": ".local/review-events"}
        new = {"catalog_home": ".local/moved/catalog", "review_events_home": ".local/moved/review-events"}
        for name, (store, tamper, refusal) in VARIANTS.items():
            with self.subTest(variant=name):
                root, _ = self.build()
                (root / "config/policy.local.json").write_text(json.dumps({"storage_roots": new}))
                # Interrupt after the first move (the catalog), then change the marker of the store under test.
                with self.assertRaisesRegex(RuntimeError, "Simulated interruption"):
                    policy.migrate_storage(root, interrupt_after=1)
                journal = root / ".local/migration.json"
                self.assertTrue(journal.is_file())
                stores = {"catalog": root / ".local/moved/catalog/catalog.sqlite3",
                          "review": root / ".local/review-events/events.sqlite3"}
                tamper(stores[store])
                digests = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in stores.items()}
                for attempt in (1, 2):  # a refused recovery must stay refusable, not half-complete
                    result = self.run_cli(root, "tools/initialize.py", "--recover-migration")
                    self.assertNotEqual(result.returncode, 0, name)
                    self.assertIn(refusal, result.stderr, f"{name} attempt {attempt}")
                    self.assertTrue(journal.is_file(), "journal must survive a refused recovery")
                    located = {"catalog": root / ".local/catalog/catalog.sqlite3",
                               "review": root / ".local/review-events/events.sqlite3"}
                    self.assertEqual({key: hashlib.sha256(path.read_bytes()).hexdigest()
                                      for key, path in located.items()}, digests, name)
                    self.assertFalse((root / ".local/moved/catalog/catalog.sqlite3").exists())
                # Ordinary entry points stay blocked until the human resolves it.
                other = self.run_cli(root, "tools/initialize.py")
                self.assertNotEqual(other.returncode, 0)
                self.assertIn("Interrupted store migration", other.stderr)
                self.assertEqual(json.loads((root / ".local/instance.json").read_text())["storage_roots"]
                                 ["catalog_home"], old["catalog_home"])


if __name__ == "__main__":
    unittest.main()
