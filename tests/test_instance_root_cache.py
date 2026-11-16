"""Instance-root validation is memoized per process and every detectable change invalidates it."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.support import clone_release
from tools import policy
from tools.catalog import catalog_store as catalog


def build_instance(path):
    path.mkdir()
    clone_release(path)
    result = subprocess.run([sys.executable, "tools/initialize.py"], cwd=path,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stderr


class InstanceRootCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.templates = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.templates.cleanup)
        cls.template = Path(cls.templates.name) / "a"
        cls.other = Path(cls.templates.name) / "b"
        build_instance(cls.template)
        build_instance(cls.other)

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name) / "instance"
        shutil.copytree(self.template, self.root)
        self.assertEqual(catalog.locations(self.root)[1].name, "catalog.sqlite3")

    def test_repeated_locations_do_not_reopen_the_stores(self):
        with patch.object(policy, "_probe_store_id", wraps=policy._probe_store_id) as probe:
            for _ in range(3):
                catalog.locations(self.root)
        self.assertEqual(probe.call_count, 0)

    def test_migration_lock_appearing_is_detected(self):
        (self.root / policy.MIGRATION_FILE).write_text("{}")
        with self.assertRaisesRegex(catalog.CatalogError, "Interrupted store migration"):
            catalog.locations(self.root)

    def test_failures_are_not_cached(self):
        lock = self.root / policy.MIGRATION_FILE
        lock.write_text("{}")
        with self.assertRaises(catalog.CatalogError):
            catalog.locations(self.root)
        lock.unlink()
        catalog.locations(self.root)

    def test_storage_roots_changing_in_policy_is_detected(self):
        (self.root / "config/policy.local.json").write_text(
            json.dumps({"storage_roots": {"intake_home": ".local/moved-intake"}}))
        with self.assertRaisesRegex(catalog.CatalogError, "differ from initialized instance layout"):
            catalog.locations(self.root)

    def test_substituted_authority_store_is_detected(self):
        shutil.copyfile(self.other / ".local/catalog/catalog.sqlite3",
                        self.root / ".local/catalog/catalog.sqlite3")
        with self.assertRaisesRegex(catalog.CatalogError, "Cross-instance or substituted catalog store"):
            catalog.locations(self.root)

    def test_symlinked_storage_root_is_detected(self):
        home = self.root / ".local/catalog"
        home.rename(self.root / ".local/catalog-real")
        home.symlink_to(self.root / ".local/catalog-real")
        with self.assertRaisesRegex(catalog.CatalogError, "symlinks are not permitted"):
            catalog.locations(self.root)

    def test_in_process_settings_change_is_detected(self):
        workspace = self.root.parent / "bare"
        workspace.mkdir()
        self.assertEqual(policy.validate_instance_roots(workspace), policy.DEFAULT_STORAGE_ROOTS)
        changed = dict(policy.DEFAULT_STORAGE_ROOTS, intake_home=".local/other-intake")
        with patch.dict(policy.SETTINGS, {"storage_roots": changed}):
            self.assertEqual(policy.validate_instance_roots(workspace)["intake_home"], ".local/other-intake")
        self.assertEqual(policy.validate_instance_roots(workspace), policy.DEFAULT_STORAGE_ROOTS)

    def test_callers_cannot_corrupt_the_cached_roots(self):
        policy.validate_instance_roots(self.root)["catalog_home"] = "x"
        self.assertEqual(policy.validate_instance_roots(self.root)["catalog_home"], ".local/catalog")


if __name__ == "__main__":
    unittest.main()
