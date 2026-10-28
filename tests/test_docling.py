"""Docling lifecycle contracts using isolated temporary catalogs and stub inference."""

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
import io
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from tools.catalog import catalog_store as catalog
from tools.extraction import docling_adapter as adapter
from tools.extraction import docling_bridge as bridge
from tools.extraction import docling_extract


class DoclingLifecycle(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.raw = b"%PDF-1.7\nsynthetic structural test"
        self.path = self.root / "sources/input.pdf"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(self.raw)
        records = [
            {"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}},
            {"id": "D1", "kind": "receipt", "data": {
                "source_ids": ["S1"], "artifact_id": "A-input", "provenance": []}},
            {"id": "A-input", "kind": "artifact", "data": {
                "path": "sources/input.pdf", "parent_id": None, "receipt_id": "D1",
                "role": "original", "audience": "source", "coverage": "pending-extraction",
                "reason": "Synthetic structural input.", "presence": "present",
                "sha256": hashlib.sha256(self.raw).hexdigest(), "hash_basis": "observed",
                "bytes": len(self.raw), "extraction": "pending", "handling_refs": [],
                "provenance": [],
            }},
        ]
        state = catalog.initialize(self.root)["state"]
        self.input = catalog.submit(self.root, records, actor="test-runner",
                                    request_id="register-input", expected_state=state["state_token"],
                                    reason="Synthetic Docling lifecycle test")
        self.selection = self.root / "config/selection.json"
        self.selection.parent.mkdir()
        self.selection.write_text('{"sentinel":"unchanged"}\n')
        self.selection_before = self.selection.read_bytes()
        self.review_home = self.root / ".local/review-events"
        self.review_before = list(self.review_home.rglob("*")) if self.review_home.exists() else []
        self.report = {
            "conversion": {"schema": "docling-rendition-v1", "format": "pdf",
                           "document": {"texts": [], "tables": []}, "pages": [],
                           "confidence": {}, "warnings": [], "omissions": []},
            "runtime": {"docling": "test"},
            "profile": {"version": 2, "limits": {}},
            "assets": {"schema": "docling-assets-v1", "files": [], "sha256": "a" * 64},
        }
        worker_sha = hashlib.sha256(adapter.WORKER.read_bytes()).hexdigest()
        self.patch = patch.object(
            adapter, "_convert_with_identity",
            side_effect=lambda *args: (deepcopy(self.report), worker_sha))
        self.convert = self.patch.start()
        self.addCleanup(self.patch.stop)

    def tearDown(self):
        self.assertEqual(self.selection.read_bytes(), self.selection_before)
        observed = list(self.review_home.rglob("*")) if self.review_home.exists() else []
        self.assertEqual(observed, self.review_before)

    def call(self, command, **kwargs):
        result = adapter.run(self.root, command, "D1", "pdf", Path("/unused/models"),
                             Path("/unused/python"), **kwargs)
        self.assertEqual(self.selection.read_bytes(), self.selection_before)
        observed = list(self.review_home.rglob("*")) if self.review_home.exists() else []
        self.assertEqual(observed, self.review_before)
        return result

    def test_prepare_verify_register_and_retry_each_repeat_conversion(self):
        prepared = self.call("prepare")
        self.assertTrue(prepared["created"])
        verified = self.call("verify")
        self.assertFalse(verified["created"])
        state = catalog.status(self.root)["state_token"]
        registered = self.call("register", actor="operator", expected_state=state)
        retry = self.call("register", actor="operator", expected_state=state)
        self.assertTrue(registered["registered"])
        self.assertTrue(retry["catalog"]["idempotent"])
        self.assertEqual(self.convert.call_count, 4)
        result = catalog.read_records(self.root, receipt="D1", limit=20)
        child = next(r for r in result["records"] if r["id"].startswith("A-docling-"))
        relation = next(r for r in result["records"] if r["id"].startswith("R-docling-"))
        parent = next(r for r in result["records"] if r["id"] == "A-input")
        self.assertEqual(child["data"]["coverage"], "deferred")
        self.assertEqual(parent["data"]["coverage"], "pending-extraction")
        self.assertEqual(relation["data"]["relation"], "rendition-of")
        self.assertIn("not visual equivalence", relation["data"]["scope"])

    def test_catalog_mutation_during_conversion_refuses_before_publication(self):
        def mutate(*args):
            record = {"id": "S2", "kind": "source",
                      "data": {"category": "artifact", "provenance": []}}
            catalog.submit(self.root, [record], actor="test-runner", request_id="mutation",
                           expected_state=catalog.status(self.root)["state_token"],
                           reason="Unrelated concurrent catalog mutation")
            return deepcopy(self.report), hashlib.sha256(adapter.WORKER.read_bytes()).hexdigest()
        self.convert.side_effect = mutate
        with patch.object(adapter, "_publish") as publish:
            with self.assertRaisesRegex(adapter.AdapterError, "Catalog changed"):
                self.call("prepare")
            publish.assert_not_called()
        self.assertEqual(list((self.root / adapter.HOME).rglob("extraction.json")), [])

    def test_postpublication_mutation_rolls_back_only_new_rendition(self):
        publish = adapter._publish
        def mutate(path, raw):
            created = publish(path, raw)
            self.path.write_bytes(self.raw + b"changed")
            return created
        with patch.object(adapter, "_publish", side_effect=mutate):
            with self.assertRaisesRegex(bridge.BridgeError, "Source differs"):
                self.call("prepare")
        self.assertEqual(list((self.root / adapter.HOME).rglob("extraction.json")), [])
        self.path.write_bytes(self.raw)
        prepared = self.call("prepare")
        with patch.object(adapter, "_publish", side_effect=mutate):
            with self.assertRaisesRegex(bridge.BridgeError, "Source differs"):
                self.call("prepare")
        self.assertTrue((self.root / prepared["rendition"]).exists())

    def test_catalog_writer_is_locked_during_publication(self):
        publish = adapter._publish
        def competing_writer(path, raw):
            with self.assertRaisesRegex(catalog.CatalogError, "writer is active"):
                with catalog.writer_lock(catalog.locations(self.root)[0]):
                    self.fail("catalog publication lock was not held")
            return publish(path, raw)
        with patch.object(adapter, "_publish", side_effect=competing_writer):
            self.call("prepare")

    def test_loaded_coordinator_code_mismatch_refuses_before_conversion(self):
        for module, field in ((adapter, "LOADED_CODE_SHA256"),
                              (bridge, "LOADED_CODE_SHA256"),
                              (bridge, "POLICY_CODE_SHA256")):
            with self.subTest(module=module.__name__, field=field), \
                 patch.object(module, field, "0" * 64):
                with self.assertRaisesRegex(bridge.BridgeError, "fresh process"):
                    self.call("prepare")
        self.convert.assert_not_called()
        self.assertFalse((self.root / adapter.HOME).exists())

    def test_worker_mutation_refuses_before_publication(self):
        with patch.object(adapter, "_require_worker",
                          side_effect=adapter.AdapterError("worker changed", "mutation")):
            with self.assertRaisesRegex(adapter.AdapterError, "worker changed"):
                self.call("prepare")
        extraction_home = self.root / adapter.HOME / "D1"
        self.assertFalse(extraction_home.exists())
        self.assertEqual(catalog.status(self.root), self.input["state"])

    def test_changed_profile_or_assets_cannot_match_prepared_identity(self):
        first = self.call("prepare")
        self.report["profile"] = {"version": 3, "limits": {}}
        with self.assertRaisesRegex(adapter.AdapterError, "Prepared rendition missing"):
            self.call("verify")
        self.report["profile"] = {"version": 2, "limits": {}}
        self.report["assets"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(adapter.AdapterError, "Prepared rendition missing"):
            self.call("verify")
        self.assertTrue((self.root / first["rendition"]).exists())

    def test_tampered_prepared_output_refuses(self):
        result = self.call("prepare")
        path = self.root / result["rendition"]
        path.write_bytes(b"tampered")
        with self.assertRaisesRegex((adapter.AdapterError, catalog.CatalogError), "differs"):
            self.call("verify")

    def test_source_mutation_and_stale_state_refuse_registration(self):
        self.call("prepare")
        self.path.write_bytes(self.raw + b"changed")
        with self.assertRaisesRegex((bridge.BridgeError, catalog.CatalogError), "Source differs"):
            self.call("register", actor="operator", expected_state="stale")
        self.path.write_bytes(self.raw)
        with self.assertRaisesRegex(catalog.CatalogError, "Stale expected state"):
            self.call("register", actor="operator", expected_state="stale")
        records = catalog.read_records(self.root, limit=20)["records"]
        self.assertFalse(any(r["id"].startswith(("A-docling-", "R-docling-")) for r in records))

    def test_saved_request_rejects_actor_change(self):
        self.call("prepare")
        state = catalog.status(self.root)["state_token"]
        self.call("register", actor="operator", expected_state=state)
        with self.assertRaisesRegex(adapter.AdapterError, "actor"):
            self.call("register", actor="different", expected_state=state)

    def test_committed_retry_preserves_newer_parent_metadata(self):
        self.call("prepare")
        state = catalog.status(self.root)["state_token"]
        first = self.call("register", actor="operator", expected_state=state)
        parent = next(r for r in catalog.read_records(self.root, receipt="D1", limit=20)["records"]
                      if r["id"] == "A-input")
        parent["data"]["reason"] = "Newer authorized parent metadata."
        newer = catalog.submit(self.root, [parent], actor="maintainer", request_id="newer-parent",
                               expected_state=first["catalog"]["state"]["state_token"],
                               reason="Newer metadata test")
        retry = self.call("register", actor="operator",
                          expected_state=newer["state"]["state_token"])
        self.assertTrue(retry["catalog"]["idempotent"])
        current = next(r for r in catalog.read_records(self.root, receipt="D1", limit=20)["records"]
                       if r["id"] == "A-input")
        self.assertEqual(current["data"]["reason"], "Newer authorized parent metadata.")

    def test_source_or_policy_changed_during_backup_aborts_registration(self):
        self.call("prepare")
        state = catalog.status(self.root)["state_token"]
        snapshot = catalog._snapshot
        binding = bridge.binding()
        for target in ("source", "policy"):
            with self.subTest(target=target):
                mutated = [False]
                def mutate(connection, backups, label):
                    result = snapshot(connection, backups, label)
                    if label == "pre-write":
                        mutated[0] = True
                        if target == "source":
                            self.path.write_bytes(self.raw + b"changed")
                    return result
                def current_binding():
                    return {**binding, "policy_sha256": "changed"} if mutated[0] and target == "policy" else binding
                with patch.object(catalog, "_snapshot", side_effect=mutate), \
                     patch.object(bridge, "binding", side_effect=current_binding):
                    with self.assertRaisesRegex(catalog.CatalogError, "changed before catalog commit"):
                        self.call("register", actor="operator", expected_state=state)
                self.path.write_bytes(self.raw)
                self.assertEqual(catalog.status(self.root)["state_token"], state)

    def test_rendition_changed_during_catalog_backup_aborts_registration(self):
        prepared = self.call("prepare")
        path = self.root / prepared["rendition"]
        state = catalog.status(self.root)["state_token"]
        snapshot = catalog._snapshot
        def mutate(connection, backups, label):
            result = snapshot(connection, backups, label)
            if label == "pre-write":
                path.write_bytes(b"tampered during backup")
            return result
        with patch.object(catalog, "_snapshot", side_effect=mutate):
            with self.assertRaisesRegex(catalog.CatalogError, "rendition changed"):
                self.call("register", actor="operator", expected_state=state)
        self.assertEqual(catalog.status(self.root)["state_token"], state)
        records = catalog.read_records(self.root, limit=20)["records"]
        self.assertFalse(any(r["id"].startswith(("A-docling-", "R-docling-")) for r in records))

    def test_postcommit_failure_reports_committed_and_recovers(self):
        self.call("prepare")
        state = catalog.status(self.root)["state_token"]
        real_publish = adapter._publish

        def fail_completion(path, raw):
            if path.name == "completion.json":
                raise OSError("simulated durable completion failure")
            return real_publish(path, raw)

        with patch.object(adapter, "_publish", side_effect=fail_completion):
            with self.assertRaisesRegex(adapter.AdapterError, "committed.*retry"):
                self.call("register", actor="operator", expected_state=state)
        retry = self.call("register", actor="operator", expected_state=state)
        self.assertTrue(retry["catalog"]["idempotent"])

    def test_uncommitted_metadata_drift_requires_reconciliation(self):
        prepared = self.call("prepare")
        registration = self.root / Path(prepared["rendition"]).parent / "registration.json"
        registration.write_bytes(adapter.canonical_bytes({
            "actor": "operator", "request_id": "docling-D1-" + prepared["run_id"],
            "reason": "Explicit Docling rendition registration; no review or indexing.",
            "records": [],
        }))
        registration.chmod(0o600)
        state = catalog.status(self.root)["state_token"]
        with self.assertRaisesRegex((adapter.AdapterError, catalog.CatalogError),
                                    "drift|reconcile|records"):
            self.call("register", actor="operator", expected_state=state)

    def test_extension_format_and_member_option_refuse(self):
        with self.assertRaisesRegex(bridge.BridgeError, "format disagrees"):
            bridge.source(self.root, "D1", "image")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            docling_extract.parser().parse_args([
                "prepare", "D1", "--format", "pdf", "--artifacts-path", "models",
                "--python", "python", "--artifact", "member"])

    def test_excluded_policy_refuses_before_source_read(self):
        tightened = deepcopy(bridge.policy.SETTINGS)
        tightened["original_roots"] = []
        with (patch.object(bridge.policy, "load", return_value=(tightened, "tight")),
              patch.object(bridge, "_read_source") as read):
            with self.assertRaisesRegex(bridge.BridgeError, "outside configured"):
                bridge.source(self.root, "D1", "pdf")
            read.assert_not_called()

    def test_importing_cli_does_not_import_optional_inference_modules(self):
        source = (ROOT / "tools/extraction/docling_extract.py").read_text()
        self.assertNotIn("import docling\n", source)
        self.assertNotIn("import torch", source)
        self.assertNotIn("import onnxruntime", source)


class AdapterProcess(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.worker_before = adapter.WORKER.read_bytes()
        self.source = self.root / "sources/input.pdf"
        self.source.parent.mkdir()
        self.source.write_bytes(b"synthetic failure-side-effect sentinel")
        self.source_before = self.source.read_bytes()
        self.selection = self.root / "config/selection.json"
        self.selection.parent.mkdir()
        self.selection.write_text('{"sentinel":"unchanged"}\n')
        self.selection_before = self.selection.read_bytes()
        catalog.initialize(self.root)
        self.catalog_path = self.root / ".local/catalog/catalog.sqlite3"
        self.catalog_before = self.catalog_path.read_bytes()
        self.review = self.root / ".local/review-events"
        self.review_before = {}

    def tearDown(self):
        self.assertEqual(adapter.WORKER.read_bytes(), self.worker_before)
        self.assertEqual(self.source.read_bytes(), self.source_before)
        self.assertEqual(self.selection.read_bytes(), self.selection_before)
        self.assertEqual(self.catalog_path.read_bytes(), self.catalog_before)
        self.assertFalse((self.root / adapter.HOME).exists())
        observed = {p.relative_to(self.review).as_posix(): p.read_bytes()
                    for p in self.review.rglob("*") if p.is_file()}
        self.assertEqual(observed, self.review_before)

    def test_isolated_worker_ignores_python_path_and_drains_large_stderr(self):
        shadow = self.root / "shadow"
        shadow.mkdir()
        (shadow / "sitecustomize.py").write_text("raise RuntimeError('startup injected')")
        (shadow / "shadow_probe.py").write_text("VALUE = 'injected'")
        worker = self.root / "worker.py"
        worker.write_text(
            "import importlib.util,json,os,sys\n"
            "assert importlib.util.find_spec('shadow_probe') is None\n"
            "assert 'PYTHONPATH' not in os.environ and 'LD_PRELOAD' not in os.environ\n"
            "os.write(2, b'x' * 2000000)\n"
            "with open(sys.argv[2], 'w') as f:\n"
            " json.dump({'schema':'docling-worker-result-v1','ok':True,'payload':{}}, f)\n"
            "os.chmod(sys.argv[2], 0o600)\n")
        worker.with_name("requirements-docling.txt").write_text("# fixture\n")
        popen = adapter.subprocess.Popen
        def launch(args, **kwargs):
            self.assertEqual(args[:2], ["unshare", "-Urn"])
            self.assertIn("-I", args)
            # Exercise the real interpreter/environment without requiring namespaces
            # for this generic subprocess fixture; real acceptance tests cover unshare.
            return popen(args[2:], **kwargs)
        with patch.dict(adapter.os.environ, {"PYTHONPATH": str(shadow),
                                             "LD_PRELOAD": "/invalid/injection.so"}), \
             patch.object(adapter, "WORKER", worker), \
             patch.object(adapter.subprocess, "Popen", side_effect=launch):
            payload, _ = adapter._operation("inspect", self.root, Path(sys.executable))
        self.assertEqual(payload, {})

    def test_successful_worker_cannot_leave_quiet_child_running(self):
        worker = self.root / "worker.py"
        marker = self.root / "child.pid"
        worker.write_text(
            "import json,os,signal,subprocess,sys\n"
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])\n"
            f"open({str(marker)!r},'w').write(str(child.pid))\n"
            "with open(sys.argv[2],'w') as f:\n"
            " json.dump({'schema':'docling-worker-result-v1','ok':True,'payload':{}},f)\n"
            "os.chmod(sys.argv[2],0o600)\n")
        worker.with_name("requirements-docling.txt").write_text("# fixture\n")
        popen = adapter.subprocess.Popen
        child_pid = None
        try:
            with patch.object(adapter, "WORKER", worker), \
                 patch.object(adapter.subprocess, "Popen",
                              side_effect=lambda args, **kwargs: popen(args[2:], **kwargs)):
                payload, _ = adapter._operation("inspect", self.root, Path(sys.executable))
            child_pid = int(marker.read_text())
            self.assertEqual(payload, {})
            # Linux may retain a zombie until init reaps it; it must not execute.
            import time
            deadline = time.monotonic() + 3
            status = Path(f"/proc/{child_pid}/stat")
            while status.exists() and status.read_text().split(") ", 1)[1][0] != "Z":
                self.assertLess(time.monotonic(), deadline, "worker child is still running")
                time.sleep(0.01)
        finally:
            if marker.exists():
                child_pid = int(marker.read_text())
                try:
                    adapter.os.kill(child_pid, adapter.signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_mutated_interpreter_is_terminated(self):
        identity = adapter._executable_identity(Path(sys.executable))
        with patch.object(adapter, "_executable_identity", side_effect=[identity, ("changed",)]), \
             patch.object(adapter.subprocess, "Popen") as popen, \
             patch.object(adapter, "_terminate_group") as terminate:
            with self.assertRaisesRegex(adapter.AdapterError, "interpreter changed"):
                adapter._operation("inspect", self.root, Path(sys.executable))
            terminate.assert_called_once_with(popen.return_value)

    def test_result_replacement_during_open_is_refused(self):
        path = self.root / "result.json"
        path.write_bytes(b"{}")
        path.chmod(0o600)
        real_open = adapter.os.open
        def replace(name, flags):
            path.unlink()
            path.write_bytes(b"{\"changed\":true}")
            path.chmod(0o600)
            return real_open(name, flags)
        with patch.object(adapter.os, "open", side_effect=replace):
            with self.assertRaisesRegex(adapter.AdapterError, "changed while opening"):
                adapter._read_result(path)

    def test_missing_optional_dependency_refuses(self):
        with self.assertRaises(adapter.AdapterError) as raised:
            adapter._validate_executable(ROOT / "missing-docling-python")
        self.assertEqual(raised.exception.code, "missing_dependency")

    def test_worker_error_code_is_preserved(self):
        with patch.object(adapter, "_validate_executable", return_value=Path(sys.executable)), \
             patch.object(adapter.subprocess, "Popen") as popen:
            process = popen.return_value
            process.pid = 2147483647
            process.wait.return_value = 1
            process.returncode = 1

            def write_result(*args, **kwargs):
                result = Path(popen.call_args.args[0][-1])
                result.write_bytes(adapter.canonical_bytes({
                    "schema": "docling-worker-result-v1", "ok": False,
                    "error": {"code": "missing_assets", "message": "provision pinned assets"},
                }))
                result.chmod(0o600)
                return 1
            process.wait.side_effect = write_result
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter._operation("inspect", ROOT, Path(sys.executable))
        self.assertEqual(raised.exception.code, "missing_assets")

    def test_invalid_assets_error_is_preserved(self):
        envelope = {"schema": "docling-worker-result-v1", "ok": False,
                    "error": {"code": "invalid_assets", "message": "invalid pinned asset"}}
        with patch.object(adapter, "_validate_executable", return_value=Path(sys.executable)), \
             patch.object(adapter.subprocess, "Popen") as popen:
            process = popen.return_value
            process.pid = 2147483647
            process.returncode = 1

            def write_result(*args, **kwargs):
                result = Path(popen.call_args.args[0][-1])
                result.write_bytes(adapter.canonical_bytes(envelope))
                result.chmod(0o600)
                return 1
            process.wait.side_effect = write_result
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter._operation("inspect", ROOT, Path(sys.executable))
        self.assertEqual(raised.exception.code, "invalid_assets")

    def test_namespace_launch_failure_is_actionable(self):
        with patch.object(adapter, "_validate_executable", return_value=Path(sys.executable)), \
             patch.object(adapter.subprocess, "Popen", side_effect=PermissionError("unshare")):
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter._operation("inspect", ROOT, Path(sys.executable))
        self.assertEqual(raised.exception.code, "isolation_unavailable")

    def test_worker_failure_without_result_refuses(self):
        with patch.object(adapter, "_validate_executable", return_value=Path(sys.executable)), \
             patch.object(adapter.subprocess, "Popen") as popen:
            popen.return_value.pid = 2147483647
            popen.return_value.wait.return_value = 1
            popen.return_value.returncode = 1
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter._operation("inspect", ROOT, Path(sys.executable))
        self.assertEqual(raised.exception.code, "worker_failed")

    def test_diagnostic_collector_drains_large_stream_without_disk_storage(self):
        with adapter._DiagnosticCollector(1024) as collector:
            process = adapter.subprocess.Popen(
                [sys.executable, "-c", "import os; os.write(2, b'x' * 2000000 + b'END')"],
                stderr=collector.stream, stdout=adapter.subprocess.DEVNULL)
            process.wait(timeout=10)
        self.assertEqual(len(collector.tail), 1024)
        self.assertTrue(collector.tail.endswith(b"END"))
        self.assertLessEqual(collector.peak_bytes, 1024)
        self.assertFalse(collector.thread.is_alive())

    def test_bounded_diagnostic_and_output_limit(self):
        with patch.object(adapter, "_validate_executable", return_value=Path(sys.executable)), \
             patch.object(adapter, "MAX_DIAGNOSTIC", 16), \
             patch.object(adapter.subprocess, "Popen") as popen:
            process = popen.return_value
            process.pid = 2147483647
            process.returncode = 1

            def diagnostic(*args, **kwargs):
                popen.call_args.kwargs["stderr"].write(b"x" * 100)
                popen.call_args.kwargs["stderr"].flush()
                return 1
            process.wait.side_effect = diagnostic
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter._operation("inspect", ROOT, Path(sys.executable))
        self.assertLessEqual(str(raised.exception).count("x"), 16)

        with patch.object(adapter, "_validate_executable", return_value=Path(sys.executable)), \
             patch.object(adapter, "MAX_RESULT", 10), \
             patch.object(adapter.subprocess, "Popen") as popen:
            process = popen.return_value
            process.pid = 2147483647
            process.returncode = 0

            def oversized(*args, **kwargs):
                Path(popen.call_args.args[0][-1]).write_bytes(b"x" * 11)
                Path(popen.call_args.args[0][-1]).chmod(0o600)
                return 0
            process.wait.side_effect = oversized
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter._operation("inspect", ROOT, Path(sys.executable))
        self.assertEqual(raised.exception.code, "output_limit")

    def test_timeout_terminates_only_worker_group(self):
        with patch.object(adapter, "_validate_executable", return_value=Path(sys.executable)), \
             patch.object(adapter.subprocess, "Popen") as popen, \
             patch.object(adapter, "_terminate_group") as terminate:
            process = popen.return_value
            process.pid = 2147483647
            process.wait.side_effect = adapter.subprocess.TimeoutExpired("worker", 1)
            process.pid = 12345
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter._operation("inspect", ROOT, Path(sys.executable))
        self.assertEqual(raised.exception.code, "timeout")
        terminate.assert_called_once_with(process)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_inspect_accepts_worker_runtime_schema(self):
        report = {"schema": "docling-runtime-v1", "profile": {},
                  "runtime": {}, "assets": {}}
        with patch.object(adapter, "_operation", return_value=(report, "a" * 64)):
            self.assertEqual(adapter.inspect(ROOT, Path(sys.executable)), report)

    def test_saved_registration_malformed_envelopes_refuse_actionably(self):
        for saved in ({"extra": True}, None):
            with self.subTest(saved=saved):
                case = DoclingLifecycle("test_saved_request_rejects_actor_change")
                case.setUp()
                try:
                    prepared = case.call("prepare")
                    path = case.root / Path(prepared["rendition"]).parent / "registration.json"
                    path.write_bytes(adapter.canonical_bytes(saved))
                    path.chmod(0o600)
                    state = catalog.status(case.root)["state_token"]
                    with case.assertRaisesRegex(adapter.AdapterError, "shape"):
                        case.call("register", actor="operator", expected_state=state)
                finally:
                    case.tearDown()
                    case.doCleanups()

    def test_invalid_success_exit_combination_refuses(self):
        with patch.object(adapter, "_validate_executable", return_value=Path(sys.executable)), \
             patch.object(adapter.subprocess, "Popen") as popen:
            process = popen.return_value
            process.pid = 2147483647
            process.returncode = 1

            def write_result(*args, **kwargs):
                result = Path(popen.call_args.args[0][-1])
                result.write_bytes(adapter.canonical_bytes({
                    "schema": "docling-worker-result-v1", "ok": True,
                    "payload": {"profile": {}, "runtime": {}, "assets": {}},
                }))
                result.chmod(0o600)
                return 1
            process.wait.side_effect = write_result
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter._operation("inspect", ROOT, Path(sys.executable))
        self.assertEqual(raised.exception.code, "invalid_result")


if __name__ == "__main__":
    unittest.main()
