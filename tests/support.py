"""Small shared helpers for isolated release-copy and workspace tests."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tools import package, policy
from tools.catalog import catalog_store as catalog
from tools.context_index import context_index as index

ROOT = Path(__file__).resolve().parents[1]


def clone_release(destination: Path) -> None:
    for name, source in package.release_paths(ROOT):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def default_policy_hash():
    """Policy hash a temporary workspace without its own policy files resolves to."""
    with tempfile.TemporaryDirectory() as directory:
        return policy.for_root(Path(directory))[1]


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest = self.root / "config/selection.json"
        self.manifest.parent.mkdir()
        self.config = {"schema_version": 1, "audience": policy.for_root(self.root)[0]["audience"],
                       "documents": [], "context_rules": {}, "renditions": []}
        self.db = self.root / "tools/context_index/.local/index.sqlite3"
        self.save_config()

    def save_config(self):
        self.manifest.write_text(json.dumps(self.config))

    def build(self):
        self.save_config()
        config, checksum = index.load_manifest(self.manifest, self.root)
        with index.publication_lock(self.db):
            return index.build(self.root, self.manifest, self.db, config, checksum)

    def docs(self):
        (self.root / "documents").mkdir()
        for name in ("README.md", "AGENTS.md"):
            shutil.copyfile(ROOT / name, self.root / "documents" / name)
        self.config["documents"] = [
            {"path": "documents/README.md", "role": "toolkit-documentation",
             "context": ["documents/AGENTS.md"]},
            {"path": "documents/AGENTS.md", "role": "toolkit-instructions", "context": []},
        ]
        self.config["context_rules"] = {
            "documents/README.md#interfaces": ["documents/AGENTS.md"]
        }
        self.build()

    def registration(self, name="sources/README.txt", raw=None):
        raw = raw if raw is not None else (ROOT / "README.md").read_bytes()
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        record = {"id": "A-input", "kind": "artifact", "data": {
            "path": name, "parent_id": None, "receipt_id": "D1", "role": "original",
            "audience": "source", "coverage": "pending-extraction",
            "reason": "Toolkit documentation used for an isolated structural test.",
            "presence": "present", "sha256": hashlib.sha256(raw).hexdigest(),
            "hash_basis": "observed", "bytes": len(raw), "extraction": "pending",
            "handling_refs": [], "provenance": [],
        }}
        records = [
            {"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}},
            {"id": "D1", "kind": "receipt", "data": {
                "source_ids": ["S1"], "artifact_id": "A-input", "provenance": []}},
            record,
        ]
        state = catalog.initialize(self.root)["state"]
        result = catalog.submit(self.root, records, actor="test-runner", request_id="register-input",
                                expected_state=state["state_token"], reason="Structural contract test")
        return records, result

    def pending(self, locator="sources/README.txt"):
        now = datetime.now(timezone.utc)
        return {
            "event_id": "rev-pending", "source": {
                "receipt_id": "D1", "source_id": "S1", "locator": locator,
                "parent_locator": None, "checksum": {"sha256": None, "basis": "unknown"}},
            "scope": {"content": ["text"], "unit": "file", "extent": "defined_scope",
                      "coordinates": "not_applicable", "ranges": [],
                      "description": "Toolkit documentation input; no semantic review performed."},
            "outcome": "pending", "counts": {
                "total": 1, "reviewed": 0, "pending": 1, "unreadable": 0, "dispositioned": 0},
            "reason": "Only structural contract behavior is under test.",
            "reviewer": "test-runner", "reviewed_on": None, "recorded_at": now.isoformat(),
            "evidence": [{"kind": "agent_statement", "pointer": None,
                          "quote": "No semantic review performed by this test."}],
            "uncertainties": ["Source version is not attested by this event."],
            "handling": [policy.for_root(self.root)[0]["handling"]],
            "provenance": {"origin": "agent_review", "record": None,
                           "mapping_version": None, "unmapped_fields": []},
            "relationships": [], "supersedes": [], "correction_reason": None,
        }
