"""Real native commitments behind durable intake stage recovery."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import policy
from tools.catalog import catalog_store as catalog
from tools.review_events import event_store as events
from tools.intake import store, runner, adapters


def stage_request(root, stages, correlation="req-native"):
    raw = json.dumps({"version": 1, "correlation_id": correlation, "correction_of": None,
                      "policy": {"version": 1, "sha256": policy.load()[1]},
                      "retry": {"max_attempts": 2, "backoff_seconds": 0}, "stages": stages}).encode()
    return store.stage(root, raw)


def stage(kind, operation, args, name="native"):
    return {"id": name, "kind": kind, "operation": operation, "arguments": args}


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        store.initialize(self.root)

    def test_catalog_commit_lost_ack_reconciles_original_revision(self):
        state = catalog.initialize(self.root)["state"]
        record = {"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}}
        args = {"records": [record], "actor": "test-runner", "request_id": "req-source",
                "reason": "Structural test", "expected_state": state["state_token"]}
        stage_request(self.root, [stage("receipt", "catalog-submit", args)])
        real = adapters.execute
        def crash(*args):
            real(*args)
            raise SystemExit()
        with patch.object(adapters, "execute", side_effect=crash), self.assertRaises(SystemExit):
            runner.run(self.root, "req-native", "native")
        catalog.submit(self.root, [{**record, "id": "S2"}], actor="test-runner", request_id="req-other",
                       expected_state=catalog.status(self.root)["state_token"], reason="Unrelated")
        result = runner.run(self.root, "req-native", "native")
        self.assertTrue(result["complete"])
        evidence = result["stages"][0]["evidence"]
        self.assertEqual(evidence["committed_revision"], 1)
        self.assertEqual(catalog.status(self.root)["revision"], 2)
        catalog.verify_backup(Path(evidence["backup"]["snapshot"]))

    def test_review_commit_lost_ack_reconciles_original_revision(self):
        import test_kit
        event = test_kit.WorkspaceCase.pending(self)
        state = events.initialize(self.root)
        args = {"value": events.bundle([event]), "actor": "test-runner", "request_id": "req-review",
                "expected_revision": 0, "expected_state": state["state_token"]}
        stage_request(self.root, [stage("review", "review-append", args)])
        real = adapters.execute
        def crash(*args):
            real(*args)
            raise SystemExit()
        with patch.object(adapters, "execute", side_effect=crash), self.assertRaises(SystemExit):
            runner.run(self.root, "req-native", "native")
        other = deepcopy(event)
        other["event_id"] = "rev-other"
        other["source"]["locator"] = "sources/other.txt"
        current = events.status(self.root)
        events.append(self.root, events.bundle([other]), "test-runner", "req-other", 1, current["state_token"])
        result = runner.run(self.root, "req-native", "native")
        self.assertTrue(result["complete"])
        self.assertEqual(result["stages"][0]["evidence"]["committed_revision"], 1)
        self.assertEqual(result["stages"][0]["evidence"]["event_ids"], ["rev-pending"])
        self.assertEqual(events.status(self.root)["revision"], 2)

    def test_docling_changed_and_missing_artifacts_block_recovery(self):
        import test_docling
        fixture = test_docling.DoclingLifecycle()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.root
        store.initialize(root)
        args = {"receipt": "D1", "format_name": "pdf", "artifacts_path": "/unused/models", "python": "/unused/python"}
        stage_request(root, [stage("extraction", "docling-verify", args)])
        prepared = fixture.call("prepare")
        path = root / prepared["rendition"]
        path.write_bytes(b"corrupt")
        result = runner.run(root, "req-native", "native")
        self.assertEqual(result["stages"][0]["status"], "blocked")
        path.unlink()
        runner.replay(root, "req-native", "native")
        result = runner.run(root, "req-native", "native")
        self.assertEqual(result["stages"][0]["status"], "blocked")
        self.assertEqual(catalog.status(root)["revision"], 1)

    def test_docling_stage_boundaries_and_restart(self):
        import test_docling
        from tools.extraction import docling_adapter
        fixture = test_docling.DoclingLifecycle()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.root
        store.initialize(root)
        base = {"receipt": "D1", "format_name": "pdf", "artifacts_path": "/unused/models", "python": "/unused/python"}
        stages = [stage("extraction", "docling-prepare", base, "prepare"),
                  stage("extraction", "docling-verify", base, "verify"),
                  stage("extraction", "docling-register", {**base, "actor": "operator", "expected_state": catalog.status(root)["state_token"]}, "register")]
        stage_request(root, stages)
        real = adapters.execute
        def crash(*args):
            real(*args)
            raise SystemExit()
        for name in ("prepare", "verify", "register"):
            with patch.object(adapters, "execute", side_effect=crash), self.assertRaises(SystemExit):
                runner.run(root, "req-native", name)
            result = runner.run(root, "req-native", name)
            self.assertEqual(result["stages"][("prepare", "verify", "register").index(name)]["status"], "succeeded")
        self.assertTrue(result["complete"])
        self.assertFalse((root / ".local/review-events").exists())
        child = next(r for r in catalog.read_records(root, receipt="D1", limit=20)["records"] if r["id"].startswith("A-docling-"))
        self.assertEqual(child["data"]["coverage"], "deferred")
