"""Per-chunk reuse of policy loads and path-safety walks (Kata ajmk) never weakens a refusal."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from tools import policy, validation_scope
from tools.catalog import catalog_store as catalog
from tools.catalog.storage import CatalogError, safe_path
from tools.diagnostics.ingest_benchmark import fixture
from tools.extraction import extract
from tools.intake import cohort, cohort_ingest, store
from tools.intake.contracts import IntakeError


def lstat_counter():
    calls = []
    real = os.lstat

    def counting(path, *args, **kwargs):
        calls.append(str(path))
        return real(path, *args, **kwargs)
    return calls, patch("os.lstat", counting)


class Workspace(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "instance"
        self.root.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)


class PathWalk(Workspace):
    def setUp(self):
        super().setUp()
        self.deep = self.root / "a/b/c"
        self.deep.mkdir(parents=True)
        (self.deep / "f.txt").write_text("x")

    def test_outside_a_scope_every_component_is_checked_every_time(self):
        calls, patched = lstat_counter()
        with patched:
            safe_path(self.deep / "f.txt")
            first = len(calls)
            safe_path(self.deep / "f.txt")
        self.assertEqual(len(calls), 2 * first)

    def test_inside_a_scope_a_repeated_walk_stops_at_a_confirmed_ancestor(self):
        with validation_scope.scoped():
            safe_path(self.deep / "f.txt")
            calls, patched = lstat_counter()
            with patched:
                safe_path(self.deep / "f.txt")
        # the file and its parent, plus the one confirmed ancestor that ends the walk
        self.assertLessEqual(len(calls), 3)

    def test_the_path_and_its_parent_are_always_checked(self):
        with validation_scope.scoped():
            safe_path(self.deep / "f.txt")
            (self.deep / "f.txt").unlink()
            (self.deep / "f.txt").symlink_to(self.root)
            with self.assertRaisesRegex(CatalogError, "Symlink paths are forbidden"):
                safe_path(self.deep / "f.txt")
            (self.deep / "f.txt").unlink()
            real = self.root / "a/b/c-real"
            self.deep.rename(real)
            self.deep.symlink_to(real)
            with self.assertRaisesRegex(CatalogError, "Symlink paths are forbidden"):
                safe_path(self.deep / "f.txt")

    def test_a_confirmed_ancestor_that_was_replaced_is_rechecked(self):
        with validation_scope.scoped():
            safe_path(self.deep / "f.txt")
            moved = self.root / "a/b-real"
            (self.root / "a/b").rename(moved)
            (self.root / "a/b").symlink_to(moved)
            with self.assertRaisesRegex(CatalogError, "Symlink paths are forbidden"):
                safe_path(self.deep / "f.txt")

    def test_a_path_to_be_created_is_walked_in_full(self):
        with validation_scope.scoped():
            safe_path(self.deep / "f.txt")
            calls, patched = lstat_counter()
            with patched:
                safe_path(self.deep / "new" / "file.json")
        self.assertGreater(len(calls), 3)

    def test_a_symlink_swapped_in_high_above_is_seen_at_the_next_boundary(self):
        with validation_scope.scoped():
            safe_path(self.deep / "f.txt")
            moved = self.base / "moved"
            self.root.rename(moved)
            self.root.symlink_to(moved)
            validation_scope.boundary()
            with self.assertRaisesRegex(CatalogError, "Symlink paths are forbidden"):
                safe_path(self.deep / "f.txt")

    def test_a_refusal_is_never_remembered(self):
        link = self.root / "link"
        link.symlink_to(self.deep)
        with validation_scope.scoped():
            for _ in range(2):
                with self.assertRaises(CatalogError):
                    safe_path(link / "f.txt")

    def test_scope_ends_with_the_block_and_is_not_shared_with_another_thread(self):
        seen = []
        with validation_scope.scoped():
            safe_path(self.deep / "f.txt")
            thread = threading.Thread(target=lambda: seen.append(validation_scope.current()))
            thread.start()
            thread.join()
            self.assertIsNotNone(validation_scope.current())
        self.assertEqual(seen, [None])
        self.assertIsNone(validation_scope.current())

    def test_another_process_starts_without_a_scope(self):
        with validation_scope.scoped():
            code = "from tools import validation_scope as v; print(v.current() is None)"
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                 cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(out.stdout.strip(), "True")


class PolicyReuse(Workspace):
    def test_a_policy_edit_inside_a_scope_is_seen_by_the_next_read(self):
        with validation_scope.scoped():
            before = policy.for_root(self.root)
            (self.root / "config").mkdir()
            (self.root / "config/policy.local.json").write_text(json.dumps({"audience": "someone-else"}))
            after = policy.for_root(self.root)
        self.assertNotEqual(before[1], after[1])
        self.assertEqual(after[0]["audience"], "someone-else")

    def test_the_policy_is_loaded_once_per_scope_and_callers_get_copies(self):
        with validation_scope.scoped():
            policy.for_root(self.root)
            with patch.object(policy, "load", wraps=policy.load) as load:
                first = policy.for_root(self.root)
                first[0]["audience"] = "mutated"
                second = policy.for_root(self.root)
        self.assertEqual(load.call_count, 0)
        self.assertNotEqual(second[0]["audience"], "mutated")

    def test_nothing_is_reused_outside_a_scope(self):
        with patch.object(policy, "load", wraps=policy.load) as load:
            policy.for_root(self.root)
            policy.for_root(self.root)
        self.assertEqual(load.call_count, 2)

    def test_the_cache_is_not_shared_across_roots(self):
        other = self.base / "other"
        other.mkdir()
        (other / "config").mkdir()
        (other / "config/policy.local.json").write_text(json.dumps({"audience": "other-audience"}))
        with validation_scope.scoped():
            one = policy.for_root(self.root)
            two = policy.for_root(other)
            again = policy.for_root(self.root)
        self.assertNotEqual(one[1], two[1])
        self.assertEqual(one, again)

    def test_a_root_replaced_by_a_symlink_inside_a_scope_is_refused(self):
        with validation_scope.scoped():
            policy.for_root(self.root)
            moved = self.base / "moved"
            self.root.rename(moved)
            self.root.symlink_to(moved)
            with self.assertRaisesRegex(ValueError, "symlinks are not permitted"):
                policy.for_root(self.root)

    def test_in_process_settings_changes_are_seen(self):
        with validation_scope.scoped():
            policy.for_root(self.root)
            with patch.dict(policy.SETTINGS, {"audience": "patched"}):
                self.assertEqual(policy.for_root(self.root)[0]["audience"], "patched")

    def test_the_extractor_fingerprint_follows_the_policy(self):
        with validation_scope.scoped():
            before = extract.fingerprint(self.root, "text")
            (self.root / "config").mkdir()
            (self.root / "config/policy.local.json").write_text(json.dumps({"audience": "someone-else"}))
            changed = extract.fingerprint(self.root, "text")
        self.assertNotEqual(changed, before)
        self.assertEqual(extract.fingerprint(self.root, "text"), changed)


class InstanceRootsReuse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_instance_root_cache import build_instance
        cls.templates = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.templates.cleanup)
        cls.template = Path(cls.templates.name) / "a"
        cls.other = Path(cls.templates.name) / "b"
        build_instance(cls.template)
        build_instance(cls.other)

    def setUp(self):
        import shutil
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.root = self.base / "instance"
        shutil.copytree(self.template, self.root)

    def test_a_migration_lock_inside_a_scope_is_refused(self):
        with validation_scope.scoped():
            catalog.locations(self.root)
            (self.root / policy.MIGRATION_FILE).write_text("{}")
            with self.assertRaisesRegex(CatalogError, "Interrupted store migration"):
                catalog.locations(self.root)

    def test_a_substituted_store_inside_a_scope_is_refused(self):
        with validation_scope.scoped():
            catalog.locations(self.root)
            import shutil
            shutil.copyfile(self.other / ".local/catalog/catalog.sqlite3",
                            self.root / ".local/catalog/catalog.sqlite3")
            with self.assertRaisesRegex(CatalogError, "Cross-instance or substituted catalog store"):
                catalog.locations(self.root)

    def test_a_symlinked_storage_component_is_refused_at_the_next_boundary(self):
        with validation_scope.scoped():
            catalog.locations(self.root)
            home = self.root / ".local/catalog"
            home.rename(self.root / ".local/catalog-real")
            home.symlink_to(self.root / ".local/catalog-real")
            validation_scope.boundary()
            with self.assertRaisesRegex(CatalogError, "symlinks are not permitted|Symlink paths"):
                catalog.locations(self.root)

    def test_loosened_permissions_on_the_catalog_are_refused(self):
        with validation_scope.scoped():
            _, db, _ = catalog.locations(self.root)
            catalog.private_file(db)
            db.chmod(0o644)
            with self.assertRaisesRegex(CatalogError, "private regular file"):
                catalog.private_file(db)

    def test_the_stores_are_not_probed_again_inside_a_scope(self):
        with validation_scope.scoped():
            catalog.locations(self.root)
            with patch.object(policy, "_probe_store_id", wraps=policy._probe_store_id) as probe:
                for _ in range(5):
                    catalog.locations(self.root)
        self.assertEqual(probe.call_count, 0)

    def test_inside_a_scope_the_directory_components_are_not_compared_again(self):
        with validation_scope.scoped():
            catalog.locations(self.root)
            calls, patched = lstat_counter()
            with patched:
                catalog.locations(self.root)
            inside = len(calls)
        calls, patched = lstat_counter()
        with patched:
            catalog.locations(self.root)
        self.assertLess(inside, len(calls))


class IngestChunks(unittest.TestCase):
    def build(self, files=6):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        root, inbox = base / "instance", base / "inbox"
        root.mkdir()
        store.initialize(root)
        catalog.initialize(root)
        fixture(inbox, files, mixed=True)
        return base, root, inbox

    def test_a_policy_edit_between_chunks_is_picked_up_by_the_next_chunk(self):
        _, root, inbox = self.build()
        seen = []
        real = cohort_ingest._settled_batch

        def watch(root_, *args, **kwargs):
            seen.append(policy.for_root(root_)[1])
            if len(seen) == 1:
                (root_ / "config").mkdir(exist_ok=True)
                (root_ / "config/policy.local.json").write_text(json.dumps({"audience": "changed-midway"}))
            return real(root_, *args, **kwargs)
        with patch.object(cohort_ingest, "_settled_batch", watch):
            cohort_ingest.ingest(root, inbox, system="s", actor="a", batch_size=2, parse_workers=1)
        self.assertGreaterEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])

    def test_a_root_swapped_for_a_symlink_between_chunks_stops_the_run(self):
        base, root, inbox = self.build()
        real = cohort_ingest._settled_batch
        calls = []

        def swap(root_, *args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                moved = base / "moved"
                root_.rename(moved)
                root_.symlink_to(moved)
            return real(root_, *args, **kwargs)
        with patch.object(cohort_ingest, "_settled_batch", swap):
            with self.assertRaises((ValueError, CatalogError, IntakeError)):
                cohort_ingest.ingest(root, inbox, system="s", actor="a", batch_size=2, parse_workers=1)

    def test_a_scoped_ingest_leaves_no_scope_behind_and_matches_the_unscoped_result(self):
        outcomes = []
        for scoped in (True, False):
            _, root, inbox = self.build()
            context = (patch.object(validation_scope, "scoped", validation_scope.scoped) if scoped
                       else patch.object(cohort_ingest.validation_scope, "scoped", _noop))
            with context, patch.object(cohort_ingest.validation_scope, "boundary", lambda: None) if not scoped \
                    else patch.object(validation_scope, "boundary", validation_scope.boundary):
                report = cohort_ingest.ingest(root, inbox, system="s", actor="a", batch_size=2, parse_workers=1)
            self.assertIsNone(validation_scope.current())
            with closing(catalog.connect(catalog.locations(root)[1])) as connection:
                connection.execute("BEGIN")
                records = catalog.inspect(connection)[1]
            outcomes.append((report["counts"], sorted(records)))
        self.assertEqual(outcomes[0], outcomes[1])


import contextlib


@contextlib.contextmanager
def _noop():
    yield


if __name__ == "__main__":
    unittest.main()
