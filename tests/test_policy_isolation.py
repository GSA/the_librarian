"""A temporary workspace never inherits the toolkit checkout's local policy override."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import policy

SYNTHETIC_ROOTS = {
    "catalog_home": "var/synthetic/catalog",
    "review_events_home": "var/synthetic/review",
    "context_index_db": "var/synthetic/index.sqlite3",
    "intake_home": "var/synthetic/intake",
    "extractions_home": "var/synthetic/.local/extractions",
    "diagnostics_log": "var/synthetic/diagnostics.jsonl",
    "selection_manifest": "config/synthetic-selection.json",
}


class ToolkitLocalOverrideIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.toolkit_local = base / "toolkit-policy.local.json"
        self.toolkit_local.write_text(json.dumps({"storage_roots": SYNTHETIC_ROOTS}))
        patcher = patch.object(policy, "LOCAL", self.toolkit_local)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.workspace = base / "workspace"
        self.workspace.mkdir()

    def test_workspace_without_policy_resolves_default_roots(self):
        self.assertEqual(policy.storage_roots(self.workspace), policy.DEFAULT_STORAGE_ROOTS)
        self.assertEqual(policy.validate_instance_roots(self.workspace), policy.DEFAULT_STORAGE_ROOTS)

    def test_workspace_without_policy_uses_versioned_defaults_hash(self):
        defaults, defaults_hash = policy.load(self.workspace)
        self.assertNotIn("storage_roots", {k: v for k, v in defaults.items()
                                           if v == SYNTHETIC_ROOTS})
        self.assertEqual(policy.for_root(self.workspace)[1], defaults_hash)
        self.assertNotEqual(policy.for_root(self.workspace)[1], policy.load()[1])

    def test_toolkit_itself_still_loads_its_local_override(self):
        self.assertEqual(policy.storage_roots(None), SYNTHETIC_ROOTS)
        self.assertEqual(policy.for_root(None), policy.load())

    def test_workspace_with_its_own_local_policy_still_loads_it(self):
        own = {"storage_roots": {"catalog_home": "own/catalog"}}
        (self.workspace / "config").mkdir()
        (self.workspace / "config/policy.local.json").write_text(json.dumps(own))
        roots = policy.storage_roots(self.workspace)
        self.assertEqual(roots["catalog_home"], "own/catalog")
        self.assertEqual(roots["review_events_home"], policy.DEFAULT_STORAGE_ROOTS["review_events_home"])

    def test_in_process_settings_patch_applies_without_leaking_local_roots(self):
        with patch.dict(policy.SETTINGS, {"excluded_name_patterns": [r"^X$"]}):
            settings, _ = policy.for_root(self.workspace)
        self.assertEqual(settings["excluded_name_patterns"], [r"^X$"])
        self.assertNotIn("var/synthetic/catalog", json.dumps(settings))


if __name__ == "__main__":
    unittest.main()
