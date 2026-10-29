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

    def publication(self):
        manifest = self.root / "config/selection.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"schema_version": 1, "audience": policy.SETTINGS["audience"],
                                        "documents": [], "context_rules": {}, "renditions": []}))
        stage_request(self.root, [stage("publication", "index-build", {"manifest": "config/selection.json", "db": "tools/context_index/.local/index.sqlite3"})])
        return manifest

    def assert_lock_recovery(self, lock):
        with lock:
            result = runner.run(self.root, "req-native", "native")
            self.assertEqual(result["stages"][0]["status"], "retryable")
            self.assertEqual(result["stages"][0]["attempts"][0]["code"], "native-busy")
            result = runner.run(self.root, "req-native", "native")
            self.assertEqual(result["stages"][0]["status"], "dead-letter")
        runner.replay(self.root, "req-native", "native")
        self.assertTrue(runner.run(self.root, "req-native", "native")["complete"])

    def test_publication_lock_is_retryable(self):
        self.publication()
        self.assert_lock_recovery(adapters.index.publication_lock(self.root / "tools/context_index/.local/index.sqlite3"))

    def test_catalog_lock_is_retryable(self):
        state = catalog.initialize(self.root)["state"]
        args = {"records": [{"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}}],
                "actor": "test-runner", "request_id": "req-source", "reason": "Structural test", "expected_state": state["state_token"]}
        stage_request(self.root, [stage("receipt", "catalog-submit", args)])
        self.assert_lock_recovery(catalog.writer_lock(catalog.locations(self.root)[0]))

    def test_review_lock_is_retryable(self):
        import test_kit
        event = test_kit.WorkspaceCase.pending(self)
        state = events.initialize(self.root)
        args = {"value": events.bundle([event]), "actor": "test-runner", "request_id": "req-review",
                "expected_revision": 0, "expected_state": state["state_token"]}
        stage_request(self.root, [stage("review", "review-append", args)])
        self.assert_lock_recovery(events.writer_lock(events.locations(self.root)[0]))

    def test_publication_decoding_failures_are_durable_without_tracebacks(self):
        import subprocess
        import sys
        manifest = self.publication()
        valid = json.loads(manifest.read_bytes())
        for content in (b"{broken", b"\xff", None):
            with self.subTest(content=content):
                if content is None:
                    source = self.root / "documents/invalid.md"
                    source.parent.mkdir()
                    source.write_bytes(b"# Invalid\n\xff")
                    valid["documents"] = [{"path": "documents/invalid.md", "role": "reference", "context": []}]
                    manifest.write_text(json.dumps(valid))
                else:
                    manifest.write_bytes(content)
                result = subprocess.run([sys.executable, "-m", "tools.intake.cli", "--root", str(self.root),
                                         "run", "req-native", "--stage", "native"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                saved = store.inspect(self.root, "req-native")["stages"][0]
                self.assertEqual(saved["status"], "blocked")
                self.assertEqual(saved["attempts"][-1]["code"], "native-input")
                self.assertIsNotNone(saved["attempts"][-1]["ended"])
                self.assertFalse((self.root / "tools/context_index/.local/index.sqlite3").exists())
                runner.replay(self.root, "req-native", "native")

    def test_corrupt_review_payload_blocks_without_traceback(self):
        import sqlite3 as database
        import subprocess
        import sys
        import test_kit
        event = test_kit.WorkspaceCase.pending(self)
        state = events.initialize(self.root)
        events.append(self.root, events.bundle([event]), "test-runner", "req-prior", 0, state["state_token"])
        other = deepcopy(event)
        other["event_id"], other["source"]["locator"] = "rev-next", "sources/next.txt"
        current = events.status(self.root)
        args = {"value": events.bundle([other]), "actor": "test-runner", "request_id": "req-review",
                "expected_revision": current["revision"], "expected_state": current["state_token"]}
        stage_request(self.root, [stage("review", "review-append", args)])
        # A decode failure inside a native writer is an input condition, not a crash.
        with closing(database.connect(events.locations(self.root)[1])) as c, c:
            c.execute("DROP TRIGGER immutable_events_update")
            c.execute("UPDATE events SET payload='{broken' WHERE event_id='rev-pending'")
        result = subprocess.run([sys.executable, "-m", "tools.intake.cli", "--root", str(self.root),
                                 "run", "req-native", "--stage", "native"], capture_output=True, text=True)
        self.assertNotIn("Traceback", result.stderr)
        saved = store.inspect(self.root, "req-native")["stages"][0]
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["attempts"][-1]["code"], "native-input")
        self.assertIsNotNone(saved["attempts"][-1]["ended"])

    def test_ledger_write_failure_is_not_reported_as_corruption(self):
        import sqlite3 as database
        self.publication()
        with patch.object(runner, "save_artifact", side_effect=database.OperationalError("disk I/O error")):
            with self.assertRaises(adapters.IntakeError) as caught:
                runner.run(self.root, "req-native", "native")
        self.assertEqual(caught.exception.code, "ledger_unavailable")
        self.assertEqual(store.inspect(self.root, "req-native")["stages"][0]["status"], "staged")

    def test_docling_timeout_is_retryable(self):
        import test_docling
        fixture = test_docling.DoclingLifecycle()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store.initialize(fixture.root)
        args = {"receipt": "D1", "format_name": "pdf", "artifacts_path": "/unused/models", "python": "/unused/python"}
        stage_request(fixture.root, [stage("extraction", "docling-prepare", args)])
        fixture.convert.side_effect = adapters.docling.AdapterError("Private worker diagnostic", "timeout")
        result = runner.run(fixture.root, "req-native", "native")
        self.assertEqual(result["stages"][0]["status"], "retryable")
        self.assertEqual(result["stages"][0]["attempts"][0]["code"], "native-timeout")
        self.assertNotIn("Private worker diagnostic", json.dumps(result))
        fixture.convert.side_effect = lambda *args: (deepcopy(fixture.report), hashlib.sha256(adapters.docling.WORKER.read_bytes()).hexdigest())
        self.assertTrue(runner.run(fixture.root, "req-native", "native")["complete"])

    def test_publication_lost_ack_reuses_matching_fresh_index(self):
        self.publication()
        real = adapters.execute
        def crash(*args):
            real(*args)
            raise SystemExit()
        with patch.object(adapters, "execute", side_effect=crash), self.assertRaises(SystemExit):
            runner.run(self.root, "req-native", "native")
        db = self.root / "tools/context_index/.local/index.sqlite3"
        before = db.stat().st_mtime_ns
        result = runner.run(self.root, "req-native", "native")
        self.assertTrue(result["complete"])
        self.assertEqual(db.stat().st_mtime_ns, before)
        self.assertTrue(result["stages"][0]["evidence"]["selection_validated"])

    def test_publication_changed_inputs_block_and_preserve_prior_index(self):
        manifest = self.publication()
        real = adapters.execute
        def crash(*args):
            real(*args)
            raise SystemExit()
        with patch.object(adapters, "execute", side_effect=crash), self.assertRaises(SystemExit):
            runner.run(self.root, "req-native", "native")
        db = self.root / "tools/context_index/.local/index.sqlite3"
        before = db.read_bytes()
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        result = runner.run(self.root, "req-native", "native")
        self.assertFalse(result["complete"])
        self.assertEqual(db.read_bytes(), before)

    def test_corrupt_index_inspection_preserves_historical_completion(self):
        from tools.intake.cli import inspection
        self.publication()
        self.assertTrue(runner.run(self.root, "req-native", "native")["complete"])
        db = self.root / "tools/context_index/.local/index.sqlite3"
        db.write_bytes(b"invalid SQLite bytes")
        result = inspection(self.root, "req-native")
        self.assertTrue(result["complete"])
        self.assertFalse(result["stages"][0]["availability"]["available"])

    def test_drift_before_build_snapshot_preserves_prior_index(self):
        manifest = self.publication()
        source = self.root / "documents/document.md"
        source.parent.mkdir()
        source.write_text("# Original\nOriginal text\n")
        config = json.loads(manifest.read_bytes())
        config["documents"] = [{"path": "documents/document.md", "role": "reference", "context": []}]
        manifest.write_text(json.dumps(config))
        index = adapters.index
        db = self.root / "tools/context_index/.local/index.sqlite3"
        config, checksum = index.load_manifest(manifest)
        with index.publication_lock(db):
            index.build(self.root, manifest, db, config, checksum)
        before = db.read_bytes()
        real_build = index.build
        def drift(*args, **kwargs):
            source.write_text("# Changed\nChanged text\n")
            return real_build(*args, **kwargs)
        # Force rebuild with a valid stale prior index. Drift occurs after the
        # intake intent check but before the native builder snapshots sources.
        source.write_text("# Pinned\nPinned text\n")
        with patch.object(index, "build", side_effect=drift):
            result = runner.run(self.root, "req-native", "native")
        self.assertEqual(result["stages"][0]["status"], "blocked")
        self.assertEqual(db.read_bytes(), before)

    def test_completed_publication_reports_current_staleness(self):
        manifest = self.publication()
        result = runner.run(self.root, "req-native", "native")
        self.assertTrue(result["complete"])
        evidence = result["stages"][0]["evidence"]
        saved_stage = stage("publication", "index-build", {"manifest": "config/selection.json", "db": "tools/context_index/.local/index.sqlite3"})
        self.assertTrue(adapters.availability(self.root, saved_stage, evidence)["available"])
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        self.assertFalse(adapters.availability(self.root, saved_stage, evidence)["available"])
        self.assertTrue(store.inspect(self.root, "req-native")["complete"])

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

    def docling_fixture(self):
        import test_docling
        fixture = test_docling.DoclingLifecycle()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store.initialize(fixture.root)
        return fixture

    def test_docling_stage_converts_once_per_execution(self):
        fixture = self.docling_fixture()
        args = {"receipt": "D1", "format_name": "pdf", "artifacts_path": "/unused/models", "python": "/unused/python"}
        stage_request(fixture.root, [stage("extraction", "docling-prepare", args)])
        real = adapters.execute
        def crash(*call):
            real(*call)
            raise SystemExit()
        with patch.object(adapters, "execute", side_effect=crash), self.assertRaises(SystemExit):
            runner.run(fixture.root, "req-native", "native")
        # One pinned intent conversion plus one conversion for the attempt.
        self.assertEqual(fixture.convert.call_count, 2)
        self.assertTrue(runner.run(fixture.root, "req-native", "native")["complete"])
        self.assertEqual(fixture.convert.call_count, 3)

    def test_source_drift_before_native_snapshot_publishes_nothing(self):
        from tools.extraction import docling_adapter as docling
        fixture = self.docling_fixture()
        args = {"receipt": "D1", "format_name": "pdf", "artifacts_path": "/unused/models", "python": "/unused/python"}
        stage_request(fixture.root, [stage("extraction", "docling-prepare", args)])
        real = docling.run
        def drift(*call, **named):
            raw = fixture.raw + b" revised"
            fixture.path.write_bytes(raw)
            record = {"id": "A-input", "kind": "artifact", "data": {
                "path": "sources/input.pdf", "parent_id": None, "receipt_id": "D1",
                "role": "original", "audience": "source", "coverage": "pending-extraction",
                "reason": "Revised synthetic structural input.", "presence": "present",
                "sha256": hashlib.sha256(raw).hexdigest(), "hash_basis": "observed",
                "bytes": len(raw), "extraction": "pending", "handling_refs": [], "provenance": []}}
            catalog.submit(fixture.root, [record], actor="test-runner", request_id="revise-input",
                           expected_state=catalog.status(fixture.root)["state_token"],
                           reason="Source revised between intake pinning and native snapshot")
            return real(*call, **named)
        with patch.object(adapters.docling, "run", side_effect=drift):
            result = runner.run(fixture.root, "req-native", "native")
        self.assertEqual(result["stages"][0]["status"], "blocked")
        self.assertEqual(list((fixture.root / docling.HOME).rglob("extraction.json")), [])

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
