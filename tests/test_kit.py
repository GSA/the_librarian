"""Structural contracts against this toolkit's actual documentation, never a seeded corpus."""

from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
from tools import package, policy
from tools.catalog import catalog_store as catalog
from tools.context_index import compact_records
from tools.context_index import context_index as index
from tools.context_index import qualification
from tools.diagnostics import retrieval_log as diagnostics
from tools.extraction import docling_bridge, extract
from tools.review_events import event_reader
from tools.review_events import event_store as events
from tools.review_events import review_events as contract
from tests.support import clone_release


class PackageLayoutTests(unittest.TestCase):
    MODULES = (
        "tools.catalog.inventory",
        "tools.context_index.context_index",
        "tools.review_events.event_store",
        "tools.extraction.extract",
        "tools.diagnostics.retrieval_log",
    )

    def test_modules_import_without_path_mutation(self):
        before = list(sys.path)
        for name in self.MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                self.assertTrue(Path(module.__file__).resolve().is_relative_to(ROOT / "tools"))
        self.assertEqual(sys.path, before)

    def test_contracts_and_index_engine_avoid_adapter_dependencies(self):
        code = (
            "import sys; "
            "import tools.catalog.contracts; "
            "assert 'sqlite3' not in sys.modules; "
            "import tools.context_index.context_index; "
            "assert 'argparse' not in sys.modules"
        )
        result = subprocess.run(
            [sys.executable, "-S", "-c", code], cwd=ROOT,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_context_cli_and_transitional_module_expose_same_commands(self):
        outputs = []
        for name in ("tools.context_index.cli", "tools.context_index.context_index"):
            result = subprocess.run(
                [sys.executable, "-m", name, "--help"], cwd=ROOT,
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(result.stdout)
        for command in ("build", "status", "search", "get", "get-many", "catalog"):
            self.assertTrue(all(command in output for output in outputs), command)

    def test_context_cli_formats_engine_failures_without_traceback(self):
        result = subprocess.run(
            [sys.executable, "-m", "tools.context_index.cli", "--root", "/missing", "status"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("context-index:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_operator_modules_expose_help(self):
        for name in self.MODULES:
            with self.subTest(module=name):
                result = subprocess.run(
                    [sys.executable, "-m", name, "--help"], cwd=ROOT,
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout.lower())




class DiscoveryTests(unittest.TestCase):
    def test_workspace_scenarios_are_not_inherited_by_absence_tests(self):
        def flatten(suite):
            for test in suite:
                if isinstance(test, unittest.TestSuite):
                    yield from flatten(test)
                else:
                    yield test.id()

        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        ids = list(flatten(suite))
        workspace = {name.rsplit(".", 1)[-1] for name in ids if ".WorkspaceTests." in name}
        absence = {name.rsplit(".", 1)[-1] for name in ids if ".AbsenceClassificationTests." in name}
        self.assertFalse(workspace & absence)
        worker_modules = {
            name.rsplit(".", 2)[0] for name in ids
            if "docling_worker_contract_tests." in name
        }
        self.assertEqual(worker_modules, {"docling_worker_contract_tests"})


class StoreIdentity(unittest.TestCase):
    def test_new_application_ids_and_superseded_refusal(self):
        self.assertEqual(catalog.APPLICATION_ID, 0x4C434154)
        self.assertEqual(events.APPLICATION_ID, 0x4C524556)
        self.assertEqual(index.APPLICATION_ID, 0x4C435458)
        for module, old, message in (
                (catalog, 0x46434154, "Superseded FCAT"),
                (events, 0x46524556, "Superseded FREV")):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = module.initialize(root)
                database = module.locations(root)[1]
                with sqlite3.connect(database) as connection:
                    connection.execute(f"PRAGMA application_id={old}")
                with self.assertRaisesRegex(Exception, message + ".*migration or re-initialization"):
                    module.status(root)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("PRAGMA application_id=0x46435458")
            with self.assertRaisesRegex(Exception, "Superseded FCTX.*migration or re-initialization"):
                index.readonly(database)


class DoclingCleanCopy(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.kit = self.root / "kit"
        self.kit.mkdir()
        clone_release(self.kit)

    def test_bridge_is_the_only_installation_specific_file(self):
        bridge = self.kit / "tools/extraction/docling_bridge.py"
        bridge.unlink()
        process = subprocess.run(
            [sys.executable, "-m", "tools.extraction.docling_extract", "--help"],
            cwd=self.kit, capture_output=True, text=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("docling_bridge", process.stderr)
        excluded = {"tools/extraction/docling_bridge.py"}
        for name, source in package.release_paths(ROOT):
            if name not in excluded:
                self.assertEqual((self.kit / name).read_bytes(), source.read_bytes(), name)

    def test_clean_copy_runs_docling_lifecycle_under_its_own_policy(self):
        local = self.kit / "config/policy.local.json"
        local.write_text(json.dumps({"original_roots": ["clone-sources"]}))
        script = r'''
import hashlib,json,sys,subprocess
from pathlib import Path
from unittest.mock import patch
from tools.catalog import catalog_store as c
from tools.extraction import docling_adapter as a
from tools.extraction import docling_bridge as b
root=Path.cwd()
raw=b'%PDF-1.7\nclean-copy structural fixture'
(root/'sources').mkdir(); (root/'sources/refused.pdf').write_bytes(raw)
(root/'clone-sources').mkdir(); (root/'clone-sources/accepted.pdf').write_bytes(raw)
def records(receipt,artifact,path):
 return [
  {'id':'S'+receipt[1:],'kind':'source','data':{'category':'artifact','provenance':[]}},
  {'id':receipt,'kind':'receipt','data':{'source_ids':['S'+receipt[1:]],'artifact_id':artifact,'provenance':[]}},
  {'id':artifact,'kind':'artifact','data':{'path':path,'parent_id':None,'receipt_id':receipt,
   'role':'original','audience':'source','coverage':'pending-extraction','reason':'Clean-copy structural fixture.',
   'presence':'present','sha256':hashlib.sha256(raw).hexdigest(),'hash_basis':'observed','bytes':len(raw),
   'extraction':'pending','handling_refs':[],'provenance':[]}}]
s=c.initialize(root)['state']
out=c.submit(root,records('D1','A-refused','sources/refused.pdf')+records('D2','A-accepted','clone-sources/accepted.pdf'),
 actor='clean-copy',request_id='inputs',expected_state=s['state_token'],reason='Clean-copy structural acceptance')
try:
 b.source(root,'D1','pdf')
 raise AssertionError('checkout source root was accepted by clone policy')
except b.BridgeError:
 pass
report={'conversion':{'schema':'docling-rendition-v1','format':'pdf','document':{'texts':[],'tables':[]},
 'pages':[],'confidence':{},'warnings':[],'omissions':[]},'runtime':{'docling':'stub'},
 'profile':{'version':2,'limits':{}},'assets':{'schema':'docling-assets-v1','files':[],'sha256':'a'*64}}
worker=hashlib.sha256(a.WORKER.read_bytes()).hexdigest()
def invoke(command,receipt='D2',extra=()):
 args=['--root',str(root),command,receipt,'--format','pdf',
       '--artifacts-path','models','--python','python',*extra]
 # Execute the cloned entry point as __main__ in a fresh subprocess.
 # Only inference is substituted; parser, exit codes, serialization, policy,
 # publication and catalog writes are the real packaged implementation.
 seam="""import sys,json,runpy,hashlib
from pathlib import Path
from unittest.mock import patch
from tools.extraction import docling_adapter as a
report=json.loads(sys.argv.pop(1))
sys.argv[0]='tools.extraction.docling_extract'
def convert(*args):
 with Path('inference-calls').open('a') as log: log.write('convert\\n')
 return report,hashlib.sha256(a.WORKER.read_bytes()).hexdigest()
with patch.object(a,'_convert_with_identity',side_effect=convert):
 runpy.run_module(sys.argv[0],run_name='__main__')
"""
 result=subprocess.run([sys.executable,'-c',seam,json.dumps(report),*args],
                       cwd=root,capture_output=True,text=True)
 return result.returncode,result.stdout,result.stderr
code,output,error=invoke('prepare','D1')
assert code==1 and 'outside configured' in error and not output
assert not (root/'inference-calls').exists()
code,output,error=invoke('prepare'); assert code==0,error
prepared=json.loads(output)
code,output,error=invoke('verify'); assert code==0,error
verified=json.loads(output); assert verified['run_id']==prepared['run_id']
state=c.status(root)['state_token']
extra=['--actor','clean-copy','--expected-state',state]
code,output,error=invoke('register',extra=extra); assert code==0,error
registered=json.loads(output)
code,output,error=invoke('register',extra=extra); assert code==0,error
retry=json.loads(output); assert retry['catalog']['idempotent']
assert (root/'inference-calls').read_text().splitlines()==['convert']*4
print(json.dumps({'prepared':prepared['created'],'registered':registered['registered'],
 'verified':verified['command']=='verify','retry':retry['catalog']['idempotent'],
 'records':[r['id'] for r in c.read_records(root,receipt='D2',limit=20)['records']]}))
'''
        process = subprocess.run([sys.executable, "-c", script], cwd=self.kit,
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(all(result[key] for key in ("prepared", "registered", "verified", "retry")))
        self.assertTrue(any(value.startswith("A-docling-") for value in result["records"]))


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest = self.root / "config/selection.json"
        self.manifest.parent.mkdir()
        self.config = {"schema_version": 1, "audience": policy.SETTINGS["audience"],
                       "documents": [], "context_rules": {}, "renditions": []}
        self.db = self.root / "tools/context_index/.local/index.sqlite3"
        self.save_config()

    def save_config(self):
        self.manifest.write_text(json.dumps(self.config))

    def build(self):
        self.save_config()
        config, checksum = index.load_manifest(self.manifest)
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
            "handling": [policy.SETTINGS["handling"]],
            "provenance": {"origin": "agent_review", "record": None,
                           "mapping_version": None, "unmapped_fields": []},
            "relationships": [], "supersedes": [], "correction_reason": None,
        }



class WorkspaceTests(WorkspaceCase):
    def test_empty_initialization(self):
        self.assertEqual(catalog.initialize(self.root)["records"], 0)
        self.assertEqual(events.initialize(self.root)["events"], 0)
        self.assertEqual(self.build()["documents"], 0)
        with closing(index.readonly(self.db)) as db:
            self.assertEqual(index.search(db, "documentation", 5, None)["hits"], [])
        self.assertEqual(event_reader.read_snapshot(self.root).query()["records"], [])

    def test_exact_packets_and_compaction(self):
        self.docs()
        with closing(index.readonly(self.db)) as db:
            packet = index.packet(db, self.config, "documents/README.md#interfaces", 60000)
            self.assertIn("documents/AGENTS.md", {p["path"] for p in packet["pieces"]})
            for piece in packet["pieces"]:
                raw = (self.root / piece["path"]).read_bytes()
                lines = raw.decode().splitlines(keepends=True)
                self.assertEqual(piece["source_hash"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(piece["text"], "".join(lines[piece["line_start"]-1:piece["line_end"]]))
            compact = index.compact_packet(packet)
            self.assertEqual([p["text"] for p in compact["pieces"]], [p["text"] for p in packet["pieces"]])
            many = index.packet_many(db, self.config,
                                     ["documents/README.md#interfaces", "documents/AGENTS.md"], 60000)
            self.assertEqual(len(many["requests"]), 2)
            with self.assertRaises(index.IndexError_):
                index.packet(db, self.config, "documents/README.md", 1)
            with self.assertRaises(index.IndexError_):
                index.packet_many(db, self.config, [], 60000)

    def test_missing_context_refuses_build(self):
        self.docs()
        before = self.db.read_bytes()
        self.config["context_rules"]["documents/README.md"] = ["documents/AGENTS.md#not-present"]
        with self.assertRaises(index.IndexError_):
            self.build()
        self.assertEqual(self.db.read_bytes(), before)

    def test_refusing_publication_guard_preserves_prior_index(self):
        self.docs()
        before = self.db.read_bytes()
        config, checksum = index.load_manifest(self.manifest)
        def refuse():
            raise index.IndexError_("Caller refused publication")
        with index.publication_lock(self.db), self.assertRaises(index.IndexError_):
            index.build(self.root, self.manifest, self.db, config, checksum,
                        publication_guard=refuse)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(list(self.db.parent.glob(f".{self.db.name}.build-*")))

    def test_stale_content_detected(self):
        self.docs()
        (self.root / "documents/AGENTS.md").write_bytes((ROOT / "README.md").read_bytes())
        config, digest = index.load_manifest(self.manifest)
        with closing(index.readonly(self.db)) as db:
            self.assertTrue(index.freshness(db, self.root, self.manifest, self.db, config, digest))

    def test_paths_and_external_dictionary(self):
        self.assertFalse(index.eligible("../README.md"))
        self.assertFalse(index.eligible(".local/database.md"))
        self.assertFalse(policy.allowed("sources/.git/config", "original_roots"))
        with patch.dict(policy.SETTINGS, {"excluded_name_patterns": [r"^README\.md$"]}):
            self.assertFalse(index.eligible("documents/README.md"))
        (self.root / "documents").mkdir()
        (self.root / "documents/README.md").symlink_to(ROOT / "README.md")
        with self.assertRaises(index.IndexError_):
            index.source_path(self.root, "documents/README.md")

    def test_catalog_retry_state_and_backup(self):
        records, result = self.registration()
        retry = catalog.submit(self.root, records, actor="test-runner", request_id="register-input",
                               expected_state="outdated", reason="Structural contract test")
        self.assertTrue(retry["idempotent"])
        with self.assertRaises(catalog.CatalogError):
            catalog.submit(self.root, records, actor="test-runner", request_id="different",
                           expected_state="outdated", reason="Structural contract test")
        packet = catalog.read_records(self.root, receipt="D1")
        self.assertEqual(compact_records.expand_catalog(compact_records.compact_catalog(packet)), packet)
        snapshot = Path(result["backup"]["snapshot"])
        self.assertEqual(catalog.verify_backup(snapshot), result["state"])
        restored = self.root / "restored"
        restored.mkdir()
        catalog.restore(restored, snapshot)
        self.assertEqual(catalog.status(restored)["state_token"], result["state_token"])

    def test_native_review_correction_retry_and_restore(self):
        self.registration()
        state = events.initialize(self.root)
        event = self.pending()
        result = events.append(self.root, events.bundle([event]), "test-runner", "req-pending",
                               0, state["state_token"])
        retry = events.append(self.root, events.bundle([event]), "test-runner", "req-pending",
                              0, state["state_token"])
        self.assertTrue(retry["idempotent"])
        updated = deepcopy(event)
        updated.update(event_id="rev-correction", supersedes=[event["event_id"]],
                       correction_reason="Clarify pending reason without asserting review.",
                       reason="This remains pending after the structural test.",
                       recorded_at=(datetime.fromisoformat(event["recorded_at"]) + timedelta(seconds=1)).isoformat())
        result = events.append(self.root, events.bundle([updated]), "test-runner", "req-correction",
                               1, result["state_token"])
        snapshot = event_reader.read_snapshot(self.root)
        self.assertEqual(snapshot.query()["records"][0]["event"]["event_id"], "rev-correction")
        self.assertEqual(snapshot.query(include_history=True)["count"], 2)
        with self.assertRaises(contract.ReviewError):
            snapshot.query(after=1)
        with self.assertRaises(contract.ReviewError):
            snapshot.query(expected_state="0" * 64)
        with self.assertRaises(contract.ReviewError):
            snapshot.query(max_chars=1)
        with closing(events.connect(events.locations(self.root)[1], write=True)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM events")
        destination = self.root / "recovered"
        destination.mkdir()
        events.restore(destination, Path(result["backup"]["snapshot"]))
        self.assertEqual(events.status(destination)["state_token"], result["state_token"])

    def test_invalid_native_event_rejected(self):
        events.initialize(self.root)
        event = self.pending()
        event["counts"]["reviewed"] = 1
        with self.assertRaises(contract.ReviewError):
            contract.validate(events.bundle([event]), schema=events.store_schema(), stored=True)
        event = self.pending()
        event["provenance"]["origin"] = "unattributed"
        with self.assertRaises(contract.ReviewError):
            events.prepare_submission(events.bundle([event]), self.root)

    def selected_text(self, raw=b"first\r\n\xce\xb2\nlast"):
        self.registration("sources/notes.txt", raw)
        prepared = extract.run(self.root, "prepare", "D1", "text")
        extract.run(self.root, "verify", "D1", "text")
        extract.run(self.root, "register", "D1", "text", "test-runner",
                    catalog.status(self.root)["state_token"])
        child = next(record for record in catalog.read_records(self.root, receipt="D1")["records"]
                     if record["kind"] == "artifact" and record["data"]["role"] == "generated-rendition")
        child["data"]["coverage"] = "indexed"
        catalog.submit(self.root, [child], actor="test-runner", request_id="select-text-rendition",
                       expected_state=catalog.status(self.root)["state_token"],
                       reason="Explicit synthetic text selection")
        self.docs()
        self.config["renditions"] = [{"path": prepared["rendition"],
            "sha256": prepared["rendition_sha256"], "receipt_id": "D1", "format": "text",
            "context": ["documents/AGENTS.md"]}]
        self.build()
        return prepared

    def test_selected_text_round_trips_exact_lines_and_byte_locations(self):
        prepared = self.selected_text()
        with closing(index.readonly(self.db)) as db:
            rows = db.execute("SELECT locator, payload FROM rendition_locations "
                              "ORDER BY locator").fetchall()
            self.assertEqual([row[0].rsplit("#", 1)[1] for row in rows],
                             ["line-1", "line-2", "line-3"])
            self.assertEqual(json.loads(rows[0][1]), {
                "json_pointer": "/payload/lines/0", "original_line": 1,
                "byte_start": 0, "byte_end": 7, "terminator": "crlf",
            })
            packet = index.packet(db, self.config, prepared["rendition"] + "#line-2", 60000)
            piece = next(piece for piece in packet["pieces"] if piece["locator"].endswith("#line-2"))
            self.assertIn('"text": "β\\n"', piece["text"])
            metadata = packet["renditions"][prepared["rendition"]]
            self.assertEqual(metadata["format"], "text")
            self.assertEqual(metadata["bom_bytes"], 0)
            found = index.search(db, "β", 10, None)
            self.assertEqual(found["hits"][0]["locator"], prepared["rendition"] + "#line-2")
            self.assertIsInstance(found["hits"][0]["line_start"], int)
            self.assertGreaterEqual(found["hits"][0]["line_end"], found["hits"][0]["line_start"])
            self.assertEqual(found["hits"][0]["line_basis"],
                             "extraction-json; source_location identifies original line or XML element")

    def test_selected_text_unicode_separators_keep_physical_json_line_citations(self):
        content = "a\u0085b\u2028c\u2029d"
        prepared = self.selected_text(content.encode("utf-8"))
        locator = prepared["rendition"] + "#line-1"
        with closing(index.readonly(self.db)) as db:
            packet = index.packet(db, self.config, locator, 60000)
        piece = next(piece for piece in packet["pieces"] if piece["locator"] == locator)
        physical = (self.root / prepared["rendition"]).read_text().splitlines(keepends=True)
        # splitlines() is intentionally unsuitable here; derive physical JSON
        # lines solely from the encoder's LF bytes.
        raw_text = (self.root / prepared["rendition"]).read_text()
        physical = [part + "\n" for part in raw_text.split("\n")[:-1]]
        self.assertEqual(piece["text"], "".join(physical[piece["line_start"] - 1:piece["line_end"]]))
        self.assertIn(content, piece["text"])
        self.assertEqual(piece["line_end"] - piece["line_start"], 6)

    def test_text_rendition_refuses_inconsistent_byte_mapping(self):
        api = index.rendition_api()
        payload = extract.text(b"one\r\ntwo")
        payload["lines"][1]["byte_start"] = 4
        with self.assertRaisesRegex(index.IndexError_, "line mapping"):
            index.renditions.validate_text_mapping(payload, b"one\r\ntwo", api)

        for label, mutate in (
            ("truncated", lambda payload: payload["lines"].pop()),
            ("final-state", lambda payload: payload.update(ends_with_terminator=True)),
        ):
            payload = extract.text(b"one\ntwo")
            mutate(payload)
            with self.subTest(label=label), self.assertRaisesRegex(index.IndexError_, "mapping"):
                index.renditions.validate_text_mapping(payload, b"one\ntwo", api)

        payload = extract.text(b"one\ntwo")
        payload.update(bom_bytes=3)
        for record in payload["lines"]:
            record["byte_start"] += 3
            record["byte_end"] += 3
        with self.assertRaisesRegex(index.IndexError_, "BOM declaration"):
            index.renditions.validate_text_mapping(payload, b"one\ntwo", api)

        for label, lines in (
            ("merged-lines", [{"line": 1, "text": "a\nb\n", "terminator": "lf",
                               "byte_start": 0, "byte_end": 4}]),
            ("split-crlf", [{"line": 1, "text": "a\r", "terminator": "cr",
                             "byte_start": 0, "byte_end": 2},
                            {"line": 2, "text": "\n", "terminator": "lf",
                             "byte_start": 2, "byte_end": 3}]),
        ):
            payload = extract.text(b"a\nb\n" if label == "merged-lines" else b"a\r\n")
            payload["lines"] = lines
            with self.subTest(label=label), self.assertRaisesRegex(index.IndexError_, "physical line mapping"):
                index.renditions.validate_text_mapping(
                    payload, b"a\nb\n" if label == "merged-lines" else b"a\r\n", api)

        payload = extract.text(b"one\ntwo")
        payload["lines"][0]["text"] = "six\n"
        with self.assertRaisesRegex(index.IndexError_, "mapping"):
            index.renditions.validate_text_mapping(payload, b"one\ntwo", api)
        payload["lines"][0]["text"] = None
        with self.assertRaisesRegex(index.IndexError_, "field types"):
            index.renditions.validate_text_mapping(payload, b"one\ntwo", api)

    def docling_qualification_fixture(self, *, tables=False):
        path = "data/extractions/.local/D1/" + "a" * 64 + "/extraction.json"
        sha = "b" * 64
        selection = {"path": path, "sha256": sha, "receipt_id": "D1", "format": "docling",
                     "context": ["documents/README.md"], "review_event_ids": ["rev-exact"]}
        value = {"schema": "docling-rendition-v1", "source": {
            "receipt_id": "D1", "source_ids": ["S1"], "path": "sources/input.pdf",
            "sha256": "c" * 64}, "conversion": {
                "schema": "docling-rendition-v1", "format": "pdf",
                "document": {"texts": [], "tables": [{}] if tables else []}, "pages": []}}
        event = self.pending(path)
        event.update(event_id="rev-exact", outcome="reviewed", reviewer="test-reviewer",
                     reviewed_on="2026-10-28")
        event["source"].update(parent_locator="sources/input.pdf",
                               checksum={"sha256": sha, "basis": "reported"})
        event["scope"].update(content=["text", "tables"] if tables else ["text"],
                              description="Complete exact Docling rendition review.")
        event["evidence"] = [{"kind": "agent_statement", "pointer": None,
                              "quote": "Reviewed the complete exact retained rendition."}]
        event["uncertainties"] = []
        event["counts"].update(reviewed=1, pending=0)
        return selection, value, event

    def test_strict_docling_qualification_accepts_only_active_exact_file_review(self):
        api = index.rendition_api()
        selection, value, event = self.docling_qualification_fixture(tables=True)
        snapshot = event_reader.Snapshot({"state_token": "d" * 64}, [event], {})
        result = qualification.docling(selection, value, {}, snapshot, api)
        self.assertEqual(result["policy_version"], qualification.POLICY_VERSION)
        self.assertEqual(result["review_event_ids"], ["rev-exact"])
        self.assertEqual(result["review_state"], "d" * 64)
        self.assertEqual(result["reviewed_content"], ["tables", "text"])
        text_event = deepcopy(event)
        text_event["scope"]["content"] = ["text"]
        table_event = deepcopy(event)
        table_event["event_id"] = "rev-table"
        table_event["scope"]["content"] = ["tables"]
        split_selection = deepcopy(selection)
        split_selection["review_event_ids"] = ["rev-exact", "rev-table"]
        split = qualification.docling(split_selection, value, {}, event_reader.Snapshot(
            {"state_token": "d" * 64}, [text_event, table_event], {}), api)
        self.assertEqual(split["reviewed_content"], ["tables", "text"])

        def assert_refused(label, mutate=None, *, events_=None, successors=None):
            selected, envelope, candidate = self.docling_qualification_fixture(tables=True)
            if mutate:
                mutate(selected, envelope, candidate)
            history = [candidate] if events_ is None else events_
            with self.subTest(label=label), self.assertRaises(index.IndexError_):
                qualification.docling(selected, envelope, {}, event_reader.Snapshot(
                    {"state_token": "e" * 64}, history, successors or {}), api)

        assert_refused("original-only", lambda s, v, e: e["source"].update(
            locator=v["source"]["path"], parent_locator=None,
            checksum={"sha256": v["source"]["sha256"], "basis": "reported"}))
        assert_refused("wrong-receipt", lambda s, v, e: e["source"].update(receipt_id="D2"))
        assert_refused("wrong-source", lambda s, v, e: e["source"].update(source_id="S2"))
        assert_refused("wrong-path", lambda s, v, e: e["source"].update(locator="other.json"))
        assert_refused("wrong-hash", lambda s, v, e: e["source"].update(
            checksum={"sha256": "f" * 64, "basis": "reported"}))
        assert_refused("partial", lambda s, v, e: e.update(outcome="partial"))
        assert_refused("uncertain", lambda s, v, e: e.update(
            uncertainties=["Table review completeness is unresolved."]))
        assert_refused("ranged", lambda s, v, e: e["scope"].update(
            extent="selected_ranges", unit="page", coordinates="one_based_inclusive",
            ranges=[{"start": 1, "end": 1}]))
        assert_refused("narrow-defined-scope", lambda s, v, e: e["scope"].update(
            description="Only selected paragraphs of the exact retained rendition."))
        assert_refused("missing-table-scope", lambda s, v, e: e["scope"].update(content=["text"]))
        assert_refused("missing", events_=[])
        _, _, superseded = self.docling_qualification_fixture(tables=True)
        assert_refused("superseded", events_=[superseded], successors={"rev-exact": "rev-new"})

    def selected_docling(self, *, missing_page=False):
        raw_source = b"%PDF-synthetic-structural-test"
        records, _ = self.registration("sources/input.pdf", raw_source)
        run = "a" * 64
        path = f"data/extractions/.local/D1/{run}/extraction.json"
        source = {"receipt_id": "D1", "artifact_id": "A-input", "source_ids": ["S1"],
                  "path": "sources/input.pdf", "sha256": hashlib.sha256(raw_source).hexdigest(),
                  "bytes": len(raw_source), "audience": "source", "handling_refs": []}
        bbox = {"l": 1, "t": 2, "r": 80, "b": 40, "coord_origin": "TOPLEFT"}
        text_item = {"self_ref": "#/texts/0", "label": "text", "text": "Reviewed phrase",
                     "prov": [{"page_no": 2 if missing_page else 1, "bbox": bbox}]}
        table = {"self_ref": "#/tables/0", "label": "table",
                 "prov": [{"page_no": 1, "bbox": bbox}], "data": {
                     "table_cells": [{"text": "Cell", "start_row_offset_idx": 0,
                                      "start_col_offset_idx": 0, "end_row_offset_idx": 1,
                                      "end_col_offset_idx": 1}], "num_rows": 1, "num_cols": 1}}
        value = {"schema": "docling-rendition-v1", "run_id": run,
                 "extractor": {"schema_version": 1, "label": "synthetic-docling"},
                 "source": source, "boundary": "Machine rendition; exact review still required.",
                 "conversion": {"schema": "docling-conversion-v1", "format": "pdf",
                                "document": {"texts": [text_item], "tables": [table]},
                                "pages": [{"page_no": 1, "width": 100, "height": 100,
                                           "textless": False}],
                                "warnings": [{"code": "low_confidence", "message": "Synthetic warning"}],
                                "confidence": {"layout": 0.75},
                                "omissions": ["generated_page_images"]}}
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
               + "\n").encode()
        rendition = self.root / path
        rendition.parent.mkdir(parents=True)
        rendition.write_bytes(raw)
        rendition.chmod(0o600)
        registered_value = deepcopy(value)
        registered_value["canonical_bytes"] = raw
        catalog.submit(self.root, docling_bridge.registration_records(
            self.root, records[2], rendition, registered_value), actor="test-runner",
            request_id="register-docling", expected_state=catalog.status(self.root)["state_token"],
            reason="Synthetic Docling registration")
        child = next(record for record in catalog.read_records(self.root, receipt="D1")["records"]
                     if record["id"] == "A-docling-" + run)
        child["data"]["coverage"] = "indexed"
        catalog.submit(self.root, [child], actor="test-runner", request_id="select-docling",
                       expected_state=catalog.status(self.root)["state_token"],
                       reason="Explicit synthetic Docling selection")
        event = self.pending(path)
        event.update(event_id="rev-exact", outcome="reviewed", reviewer="test-reviewer",
                     reviewed_on=datetime.now(timezone.utc).date().isoformat())
        event["source"].update(parent_locator=source["path"],
                               checksum={"sha256": hashlib.sha256(raw).hexdigest(),
                                         "basis": "reported"})
        event["scope"].update(content=["text", "tables"],
                              description="Complete exact Docling rendition review.")
        event["evidence"] = [{"kind": "agent_statement", "pointer": None,
                              "quote": "Reviewed the complete exact retained rendition."}]
        event["uncertainties"] = []
        event["counts"].update(reviewed=1, pending=0)
        events.initialize(self.root)
        events.append(self.root, events.bundle([event]), "test-reviewer", "req-review-docling", 0,
                      events.status(self.root)["state_token"])
        self.docs()
        self.config["renditions"] = [{"path": path, "sha256": hashlib.sha256(raw).hexdigest(),
                                     "receipt_id": "D1", "format": "docling",
                                     "context": ["documents/AGENTS.md"],
                                     "review_event_ids": ["rev-exact"]}]
        return path, value

    def test_selected_docling_builds_exact_text_and_table_records(self):
        path, value = self.selected_docling()
        self.build()
        with closing(index.readonly(self.db)) as db:
            rows = db.execute("SELECT locator,payload FROM rendition_locations ORDER BY locator").fetchall()
            self.assertEqual([row[0] for row in rows], [path + "#table-1", path + "#text-1"])
            text_packet = index.packet(db, self.config, path + "#text-1", 60000)
            text_piece = next(piece for piece in text_packet["pieces"]
                              if piece["locator"] == path + "#text-1")
            self.assertEqual(json.loads(text_piece["text"]), value["conversion"]["document"]["texts"][0])
            self.assertIsNone(text_piece["line_start"])
            self.assertIsNone(text_piece["line_end"])
            rendition_raw = (self.root / path).read_bytes()
            text_location = text_piece["source_location"]
            self.assertEqual(text_piece["text"].encode(),
                             rendition_raw[text_location["byte_start"]:text_location["byte_end"]])
            table_packet = index.packet(db, self.config, path + "#table-1", 60000)
            table_piece = next(piece for piece in table_packet["pieces"]
                               if piece["locator"] == path + "#table-1")
            self.assertEqual(json.loads(table_piece["text"]), value["conversion"]["document"]["tables"][0])
            location = json.loads(dict(rows)[path + "#table-1"])
            self.assertEqual(table_piece["text"].encode(),
                             rendition_raw[location["byte_start"]:location["byte_end"]])
            self.assertIsNone(table_piece["line_start"])
            self.assertIsNone(table_piece["line_end"])
            self.assertEqual(location["page_no"], 1)
            self.assertEqual(location["json_pointer"], "/conversion/document/tables/0")
            hit = index.search(db, "Reviewed phrase", 10, None)["hits"][0]
            self.assertEqual(hit["locator"], path + "#text-1")
            self.assertIsNone(hit["line_start"])
            self.assertIsNone(hit["line_end"])
            self.assertEqual(hit["source_location"], text_location)
            self.assertEqual(hit["line_basis"], "retained-extraction-byte-range-and-json-pointer")
            self.assertEqual(text_packet["renditions"][path]["qualification"]["review_event_ids"],
                             ["rev-exact"])
            self.assertEqual(text_packet["renditions"][path]["warnings"],
                             value["conversion"]["warnings"])
            self.assertEqual(text_packet["renditions"][path]["confidence"],
                             value["conversion"]["confidence"])
            self.assertEqual(text_packet["renditions"][path]["omissions"],
                             value["conversion"]["omissions"])
            # A broad context rule attached to the table must not leak into a
            # packet for the distinct non-line-based text record.
            self.config["context_rules"] = {path + "#table-1": ["documents/README.md"]}
            isolated = index.packet(db, self.config, path + "#text-1", 60000)
            self.assertNotIn("documents/README.md", {piece["path"] for piece in isolated["pieces"]})
            self.assertIn("null", text_packet["renditions"][path]["citation_contract"])
            self.assertIn("byte", text_packet["renditions"][path]["citation_contract"])

    def test_malformed_docling_page_and_bbox_geometry_preserve_prior_index(self):
        self.selected_docling()
        rendition = next(self.root.glob("data/extractions/.local/D1/*/extraction.json"))
        base = json.loads(rendition.read_text())
        before = self.db.read_bytes()
        bbox = lambda value: value["conversion"]["document"]["texts"][0]["prov"][0]["bbox"]
        for label, mutate in (
            ("negative", lambda value: bbox(value).update(l=-1)),
            ("inverted", lambda value: bbox(value).update(l=60, r=40)),
            ("outside", lambda value: bbox(value).update(r=101)),
            ("nonfinite", lambda value: bbox(value).update(l=float("nan"))),
            ("oversized-coordinate", lambda value: bbox(value).update(r=10 ** 1000)),
            ("non-object", lambda value: value["conversion"]["document"]["texts"][0]
             ["prov"][0].update(bbox=[])),
            ("bad-page-width", lambda value: value["conversion"]["pages"][0].update(width=-1)),
            ("oversized-page-width", lambda value: value["conversion"]["pages"][0]
             .update(width=10 ** 1000)),
            ("finite-excessive-page-width", lambda value: value["conversion"]["pages"][0]
             .update(width=1e308)),
            ("excessive-page-count", lambda value: value["conversion"].update(
                pages=[{"page_no": number, "width": 1, "height": 1, "textless": True}
                       for number in range(1, 102)])),
            ("noncanonical-pages", lambda value: value["conversion"]["pages"][0].update(page_no=2)),
        ):
            value = deepcopy(base)
            mutate(value)
            raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                   + "\n").encode()
            with self.subTest(label=label), self.assertRaises(index.IndexError_):
                list(index.renditions.records(raw, value, index.rendition_api()))
        self.assertEqual(self.db.read_bytes(), before)

    def test_excessive_docling_record_count_is_refused(self):
        _, value, _ = self.docling_qualification_fixture()
        value["conversion"].update(
            schema="docling-conversion-v1", format="pdf",
            pages=[{"page_no": 1, "width": 1, "height": 1}],
            warnings=[], confidence={}, omissions=[])
        value["conversion"]["document"]["texts"] = [None] * (
            index.renditions.DOCLING_MAX_RECORDS + 1)
        with self.assertRaisesRegex(index.IndexError_, "record count"):
            list(index.renditions.records(b"{}", value, index.rendition_api()))

    def test_malformed_docling_table_geometry_and_label_preserve_prior_index(self):
        self.selected_docling()
        rendition = next(self.root.glob("data/extractions/.local/D1/*/extraction.json"))
        base = json.loads(rendition.read_text())
        before = self.db.read_bytes()
        for label, mutate in (
            ("geometry", lambda value: value["conversion"]["document"]["tables"][0]
             ["data"]["table_cells"][0].update(start_row_offset_idx=5, end_row_offset_idx=6)),
            ("label", lambda value: value["conversion"]["document"]["texts"][0].update(label={})),
        ):
            value = deepcopy(base)
            mutate(value)
            raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                   + "\n").encode()
            with self.subTest(label=label), self.assertRaises(index.IndexError_):
                list(index.renditions.records(raw, value, index.rendition_api()))
        self.assertEqual(self.db.read_bytes(), before)

    def test_unsupported_docling_content_collection_preserves_prior_index(self):
        self.selected_docling()
        rendition = next(self.root.glob("data/extractions/.local/D1/*/extraction.json"))
        value = json.loads(rendition.read_text())
        value["conversion"]["document"]["key_value_items"] = [{"self_ref": "#/key_value_items/0"}]
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
               + "\n").encode()
        before = self.db.read_bytes()
        with self.assertRaisesRegex(index.IndexError_, "unsupported content"):
            list(index.renditions.records(raw, value, index.rendition_api()))
        self.assertEqual(self.db.read_bytes(), before)

    def test_malformed_docling_warnings_preserve_prior_index(self):
        self.selected_docling()
        rendition = next(self.root.glob("data/extractions/.local/D1/*/extraction.json"))
        value = json.loads(rendition.read_text())
        value["conversion"]["warnings"] = "not-a-list"
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
               + "\n").encode()
        before = self.db.read_bytes()
        with self.assertRaisesRegex(index.IndexError_, "warnings"):
            list(index.renditions.records(raw, value, index.rendition_api()))
        self.assertEqual(self.db.read_bytes(), before)

    def test_unknown_docling_conversion_schema_preserves_prior_index(self):
        self.selected_docling()
        rendition = next(self.root.glob("data/extractions/.local/D1/*/extraction.json"))
        value = json.loads(rendition.read_text())
        value["conversion"]["schema"] = "docling-conversion-v2"
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
               + "\n").encode()
        # Exercise the parser atomically without weakening catalog/hash checks.
        before = self.db.read_bytes()
        with self.assertRaisesRegex(index.IndexError_, "conversion schema"):
            list(index.renditions.records(raw, value, index.rendition_api()))
        self.assertEqual(self.db.read_bytes(), before)

    def test_docling_missing_page_preserves_prior_index(self):
        self.selected_docling(missing_page=True)
        before = self.db.read_bytes()
        with self.assertRaisesRegex(index.IndexError_, "page provenance"):
            self.build()
        self.assertEqual(self.db.read_bytes(), before)

    def test_docling_mid_build_review_change_preserves_prior_index(self):
        # Build-start qualification sees the active event; the publication
        # recheck must observe the newer successor map and refuse replacement.
        self.selected_docling()
        before = self.db.read_bytes()
        active = event_reader.read_snapshot(self.root)
        changed = event_reader.Snapshot({**active.state, "state_token": "f" * 64},
                                        active.events, {"rev-exact": "rev-correction"})
        with patch.object(event_reader, "read_snapshot", side_effect=[active, changed]):
            with self.assertRaisesRegex(index.IndexError_, "missing or superseded"):
                self.build()
        self.assertEqual(self.db.read_bytes(), before)

    def test_qualification_code_is_bound_into_engine_fingerprint(self):
        qualification_path = ROOT / "tools/context_index/qualification.py"
        before = index.engine_fingerprint()
        paths = [Path(index.__file__), ROOT / "tools/context_index/compact_records.py",
                 ROOT / "tools/context_index/renditions.py", qualification_path,
                 Path(policy.__file__)]
        contents = {path: path.read_bytes() for path in paths}
        with patch.object(Path, "read_bytes", autospec=True,
                          side_effect=lambda path: contents[path]
                          + (b"# changed" if path == qualification_path else b"")):
            self.assertNotEqual(index.engine_fingerprint(), before)

    def test_docling_review_state_binds_freshness_and_failed_rebuild_preserves_index(self):
        selection, value, event = self.docling_qualification_fixture()
        api = index.rendition_api()
        first = qualification.docling(selection, value, {}, event_reader.Snapshot(
            {"state_token": "1" * 64}, [event], {}), api)
        second = qualification.docling(selection, value, {}, event_reader.Snapshot(
            {"state_token": "2" * 64}, [event], {}), api)
        self.assertNotEqual(
            index.metadata_expected(self.root, "manifest", rendition_identity=api.digest(
                json.dumps(first, sort_keys=True).encode())),
            index.metadata_expected(self.root, "manifest", rendition_identity=api.digest(
                json.dumps(second, sort_keys=True).encode())))

        self.docs()
        before = self.db.read_bytes()
        config, manifest_hash = index.load_manifest(self.manifest)
        with patch.object(index.renditions, "inputs", side_effect=index.IndexError_(
                "Configured Docling review event is missing or superseded")):
            with self.assertRaisesRegex(index.IndexError_, "missing or superseded"):
                index.build(self.root, self.manifest, self.db, config, manifest_hash)
        self.assertEqual(self.db.read_bytes(), before)

    def test_docling_selection_requires_explicit_unique_review_events(self):
        api = index.rendition_api()
        base = {"path": "data/extractions/.local/D1/" + "a" * 64 + "/extraction.json",
                "sha256": "b" * 64, "receipt_id": "D1", "format": "docling",
                "context": ["documents/README.md"], "review_event_ids": ["rev-exact"]}
        index.renditions.validate({"renditions": [base]}, api)
        for review_ids in ([], ["rev-exact", "rev-exact"], [1], [{}]):
            invalid = deepcopy(base)
            invalid["review_event_ids"] = review_ids
            with self.subTest(review_ids=review_ids), self.assertRaises(index.IndexError_):
                index.renditions.validate({"renditions": [invalid]}, api)
        invalid = deepcopy(base)
        invalid["format"] = "text"
        with self.assertRaises(index.IndexError_):
            index.renditions.validate({"renditions": [invalid]}, api)

    def test_selected_empty_text_is_refused_without_replacing_index(self):
        self.docs()
        preserved = self.db.read_bytes()
        self.config["renditions"] = []
        # Build the empty rendition lifecycle in a fresh workspace state would
        # duplicate the same receipt IDs, so use the current empty selection as
        # the prior index and then register the zero-record source.
        self.registration("sources/empty.txt", b"\xef\xbb\xbf")
        prepared = extract.run(self.root, "prepare", "D1", "text")
        extract.run(self.root, "register", "D1", "text", "test-runner",
                    catalog.status(self.root)["state_token"])
        child = next(record for record in catalog.read_records(self.root, receipt="D1")["records"]
                     if record["kind"] == "artifact" and record["data"]["role"] == "generated-rendition")
        child["data"]["coverage"] = "indexed"
        catalog.submit(self.root, [child], actor="test-runner", request_id="select-empty-text",
                       expected_state=catalog.status(self.root)["state_token"], reason="Synthetic empty selection")
        self.config["renditions"] = [{"path": prepared["rendition"],
            "sha256": prepared["rendition_sha256"], "receipt_id": "D1", "format": "text",
            "context": ["documents/AGENTS.md"]}]
        with self.assertRaisesRegex(index.IndexError_, "no retrievable records"):
            self.build()
        self.assertEqual(self.db.read_bytes(), preserved)

    def selected_odt(self):
        text = (ROOT / "AGENTS.md").read_text()
        content = ('<office:document-content xmlns:office="' + extract.NS["office"] +
                   '" xmlns:text="' + extract.NS["text"] +
                   '"><office:body><office:text><text:p>' + escape(text) +
                   '</text:p></office:text></office:body></office:document-content>')
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as z:
            z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
            z.writestr("content.xml", content)
            z.writestr("meta.xml", '<office:document-meta xmlns:office="' + extract.NS["office"] +
                       '"><office:meta/></office:document-meta>')
            z.writestr("META-INF/manifest.xml", "<manifest/>")
        self.registration("sources/AGENTS.odt", buffer.getvalue())
        prepared = extract.run(self.root, "prepare", "D1", "odt")
        extract.run(self.root, "verify", "D1", "odt")
        result = extract.run(self.root, "register", "D1", "odt", "test-runner",
                             catalog.status(self.root)["state_token"])
        self.assertTrue(result["registered"])
        child = next(r for r in catalog.read_records(self.root, receipt="D1")["records"]
                     if r["kind"] == "artifact" and r["data"]["role"] == "generated-rendition")
        child["data"]["coverage"] = "indexed"
        catalog.submit(self.root, [child], actor="test-runner", request_id="select-rendition",
                       expected_state=catalog.status(self.root)["state_token"], reason="Explicit test selection")
        self.docs()
        self.config["renditions"] = [{"path": prepared["rendition"],
            "sha256": prepared["rendition_sha256"], "receipt_id": "D1", "format": "odt",
            "context": ["documents/AGENTS.md"]}]
        self.build()
        return prepared

    def test_odt_registration_and_rendition_retrieval(self):
        self.selected_odt()
        with closing(index.readonly(self.db)) as db:
            row = db.execute("SELECT locator FROM rendition_locations ORDER BY locator LIMIT 1").fetchone()
            packet = index.packet(db, self.config, row[0], 60000)
            self.assertIn("renditions", packet)
            self.assertIn("documents/AGENTS.md", {p["path"] for p in packet["pieces"]})

    def test_rendition_policy_tightening_blocks_build_and_reads(self):
        prepared = self.selected_odt()
        preserved = self.db.read_bytes()
        with closing(index.readonly(self.db)) as db:
            locator = db.execute("SELECT locator FROM rendition_locations LIMIT 1").fetchone()[0]
        protected = {self.root / "sources/AGENTS.odt", self.root / prepared["rendition"]}
        original_open = Path.open

        def guard(path, *args, **kwargs):
            self.assertNotIn(path, protected, "Excluded source or rendition was opened")
            return original_open(path, *args, **kwargs)

        for override in (
            {"excluded_name_patterns": [r"^AGENTS\.odt$"]},
            {"original_roots": []},
            {"excluded_directory_names": ["sources"]},
        ):
            with self.subTest(override=override), patch.dict(policy.SETTINGS, override):
                self.assertFalse(policy.allowed("sources/AGENTS.odt", "original_roots"))
                with patch.object(Path, "open", guard):
                    with self.assertRaisesRegex(index.IndexError_, "Original source excluded by current policy"):
                        self.build()
                    for operation in (["get", locator], ["get-many", locator], ["search", "Never seed"]):
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            code = index.main([
                                "--root", str(self.root), "--manifest", str(self.manifest),
                                "--db", str(self.db), *operation,
                            ])
                        self.assertEqual(code, 1)
                        self.assertEqual(stdout.getvalue(), "")
                        self.assertIn("Original source excluded by current policy", stderr.getvalue())
                self.assertEqual(self.db.read_bytes(), preserved)
        self.build()
        with closing(index.readonly(self.db)) as db:
            self.assertTrue(index.packet(db, self.config, locator, 60000)["pieces"])

    def test_text_extraction_contract(self):
        raw = b"\xef\xbb\xbfalpha\r\n\r\n\xce\xb2\nlast\r"
        self.assertEqual(extract.text(raw), {
            "format": "text",
            "scope": "all supplied UTF-8 text, byte-mapped without line-ending normalization",
            "bom_bytes": 3,
            "ends_with_terminator": True,
            "lines": [
                {"line": 1, "text": "alpha\r\n", "terminator": "crlf",
                 "byte_start": 3, "byte_end": 10},
                {"line": 2, "text": "\r\n", "terminator": "crlf",
                 "byte_start": 10, "byte_end": 12},
                {"line": 3, "text": "β\n", "terminator": "lf",
                 "byte_start": 12, "byte_end": 15},
                {"line": 4, "text": "last\r", "terminator": "cr",
                 "byte_start": 15, "byte_end": 20},
            ],
            "warnings": ["Strict UTF-8 text decoding only; successful decoding is not review or qualification."],
        })
        self.assertEqual(extract.text(b"one\ntwo")["lines"][-1],
                         {"line": 2, "text": "two", "terminator": "none",
                          "byte_start": 4, "byte_end": 7})
        self.assertFalse(extract.text(b"one\ntwo")["ends_with_terminator"])
        unicode_separators = "a\u0085b\u2028c\u2029d".encode("utf-8")
        self.assertEqual(extract.text(unicode_separators)["lines"], [
            {"line": 1, "text": "a\u0085b\u2028c\u2029d", "terminator": "none",
             "byte_start": 0, "byte_end": len(unicode_separators)},
        ])
        self.assertEqual(extract.text(b"")["lines"], [])
        self.assertEqual(extract.text(b"\xef\xbb\xbf")["lines"], [])
        for raw in (b"\xff", b"\xc3(", b"\xff\xfeA\x00", b"\xfe\xff\x00A",
                    b"\xff\xfe\x00\x00A\x00\x00\x00", b"\x00\x00\xfe\xff\x00\x00\x00A",
                    b"valid\x00text"):
            with self.subTest(raw=raw), self.assertRaises(extract.ExtractionError):
                extract.text(raw)
        with self.assertRaisesRegex(extract.ExtractionError, "line record limit"):
            extract.text(b"x\n" * (extract.MAX_ELEMENTS + 1))
        with patch.object(extract, "MAX_EXPANDED", 100):
            with self.assertRaisesRegex(extract.ExtractionError, "rendition exceeds output limit"):
                extract.text(b"one\ntwo\n")

    def test_text_extraction_lifecycle_is_deferred_and_idempotent(self):
        raw = b"first\r\n\xce\xb2\nlast"
        self.registration("sources/notes.txt", raw)
        source_before = (self.root / "sources/notes.txt").read_bytes()
        events.initialize(self.root)
        review_before = events.status(self.root)["state_token"]
        manifest_before = self.manifest.read_bytes()

        prepared = extract.run(self.root, "prepare", "D1", "text")
        repeated = extract.run(self.root, "prepare", "D1", "text")
        self.assertTrue(prepared["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual(extract.run(self.root, "verify", "D1", "text")["run_id"],
                         prepared["run_id"])
        state = catalog.status(self.root)["state_token"]
        registered = extract.run(self.root, "register", "D1", "text", "test-runner", state)
        retry = extract.run(self.root, "register", "D1", "text", "test-runner", state)
        self.assertTrue(registered["registered"])
        self.assertTrue(retry["idempotent"])
        child = next(record for record in catalog.read_records(self.root, receipt="D1")["records"]
                     if record["kind"] == "artifact" and record["data"]["role"] == "generated-rendition")
        self.assertEqual(child["data"]["coverage"], "deferred")
        self.assertEqual(child["data"]["sha256"], prepared["rendition_sha256"])
        self.assertEqual((self.root / "sources/notes.txt").read_bytes(), source_before)
        self.assertEqual(events.status(self.root)["state_token"], review_before)
        self.assertEqual(self.manifest.read_bytes(), manifest_before)

    def test_text_lifecycle_refuses_changed_source_and_conflicting_publication(self):
        self.registration("sources/notes.txt", b"stable\n")
        prepared = extract.run(self.root, "prepare", "D1", "text")
        source = self.root / "sources/notes.txt"
        source.write_bytes(b"changed\n")
        with self.assertRaisesRegex(extract.ExtractionError, "catalog hash"):
            extract.run(self.root, "verify", "D1", "text")
        source.write_bytes(b"stable\n")
        rendition = self.root / prepared["rendition"]
        rendition.write_bytes(b"conflicting bytes")
        with self.assertRaisesRegex(extract.ExtractionError, "differs from repeat extraction"):
            extract.run(self.root, "verify", "D1", "text")
        self.assertEqual(rendition.read_bytes(), b"conflicting bytes")

    def test_transcript_dictionary_and_xml_rejection(self):
        text = (ROOT / "AGENTS.md").read_text().splitlines()[0]
        raw = ("Transcript\nReader: " + text + "\n").encode()
        parsed = extract.gemini(raw)
        self.assertEqual("".join(line["text"] for line in parsed["lines"]).encode(), raw)
        changed = dict(policy.SETTINGS["transcript"], transcript_heading="transcript section")
        with patch.dict(policy.SETTINGS, {"transcript": changed}):
            with self.assertRaises(extract.ExtractionError):
                extract.gemini(raw)
        with self.assertRaises(extract.ExtractionError):
            extract.xml(b'<!DOCTYPE x><x/>', "content.xml")

    def test_initialize_refuses_an_unmigrated_legacy_index(self):
        clone_release(self.root / "kit")
        kit = self.root / "kit"
        legacy = kit / "tools/context-index/.local/index.sqlite3"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"legacy derived index")
        result = subprocess.run(
            [sys.executable, "tools/initialize.py"], cwd=kit,
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("legacy derived index", result.stderr.lower())
        self.assertFalse((kit / "tools/context_index/.local/index.sqlite3").exists())
        self.assertFalse((kit / ".local/catalog").exists())
        self.assertFalse((kit / ".local/review-events").exists())

    def test_clean_copy_and_policy_freshness(self):
        clone_release(self.root / "kit")
        kit = self.root / "kit"
        def run(*args):
            result = subprocess.run([sys.executable, *args], cwd=kit, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        report = run("tools/initialize.py")
        self.assertEqual(report["seeded_records"], 0)
        (kit / "config/policy.local.json").write_text(json.dumps({"excluded_name_patterns": ["private"]}))
        result = subprocess.run([sys.executable, "-m", "tools.context_index.context_index", "status"],
                                cwd=kit, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale", result.stderr.lower())
        self.assertEqual(run("-m", "tools.context_index.context_index", "build")["documents"], 0)
        output = kit / "bundle.zip"
        run("tools/package.py", "--output", str(output))
        with ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), set(json.loads((kit / "release-files.json").read_text())))
            self.assertNotIn("config/policy.local.json", archive.namelist())

    def test_portable_roots_relocation_substitution_and_migration_recovery(self):
        from tools.intake import store as intake_store

        kit = self.root / "instance_a"
        clone_release(kit)
        custom_roots = {
            "catalog_home": ".local/stores/catalog",
            "review_events_home": ".local/stores/review-events",
            "context_index_db": ".local/stores/index.sqlite3",
            "intake_home": ".local/stores/intake",
            "extractions_home": "data/extractions/.local/custom",
            "diagnostics_log": ".local/stores/diagnostics.jsonl",
            "selection_manifest": "config/selection.json",
        }
        (kit / "config/policy.local.json").write_text(json.dumps({"storage_roots": custom_roots}))
        (kit / "documents").mkdir(exist_ok=True)
        (kit / "documents/guide.md").write_text("# Guide\n\nPortable instance content.\n")
        (kit / "config/selection.local.json").write_text(json.dumps({
            "schema_version": 1,
            "audience": "authorized-local",
            "documents": [{"path": "documents/guide.md", "role": "guide", "context": []}],
            "context_rules": {},
        }))

        def run_in(target, *args, check=True):
            result = subprocess.run([sys.executable, *args], cwd=target, capture_output=True, text=True)
            if check:
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            return result

        init_report = run_in(kit, "tools/initialize.py")
        self.assertEqual(init_report["storage_roots"]["catalog_home"], ".local/stores/catalog")
        self.assertTrue((kit / ".local/instance.json").is_file())
        self.assertTrue((kit / ".local/stores/catalog/catalog.sqlite3").is_file())

        cat_state = catalog.status(kit)["state_token"]
        catalog.submit(
            kit,
            [{"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}}],
            actor="operator", request_id="req-portable-s1", expected_state=cat_state,
            reason="Seed portable instance catalog",
        )
        intake_store.initialize(kit)
        request_raw = json.dumps({
            "version": 1, "correlation_id": "req-relocate-1", "correction_of": None,
            "policy": {"version": 1, "sha256": policy.for_root(kit)[1]},
            "retry": {"max_attempts": 2, "backoff_seconds": 5},
            "stages": [{"id": "delivery", "kind": "return", "operation": "await-ack",
                        "arguments": {"delivery_correlation": "delivery-one",
                                      "disposition_path": "disposition.json",
                                      "disposition_sha256": "b" * 64}}],
        }).encode()
        intake_store.stage(kit, request_raw)

        build_report = run_in(kit, "-m", "tools.context_index.context_index", "build")
        self.assertEqual(build_report["documents"], 1)
        self.assertTrue((kit / ".local/stores/index.sqlite3").is_file())

        # Move the entire instance to a new host path: index stays fresh without rebuild.
        relocated = self.root / "relocated_instance"
        shutil.move(str(kit), str(relocated))
        status_after_move = run_in(relocated, "-m", "tools.context_index.context_index", "status")
        self.assertTrue(status_after_move["fresh"])
        hits = run_in(relocated, "-m", "tools.context_index.context_index", "search", "Portable")
        self.assertEqual(len(hits["hits"]), 1)

        # Intake ledger refuses the old host path until explicitly relocated.
        with self.assertRaises(intake_store.IntakeError):
            intake_store.inspect(relocated, "req-relocate-1")
        reloc_report = run_in(relocated, "tools/initialize.py", "--relocate")
        self.assertEqual(reloc_report["intake"]["updated_requests"], 1)
        self.assertEqual(intake_store.inspect(relocated, "req-relocate-1")["correlation_id"], "req-relocate-1")

        # Substituting another instance's catalog, review-events, or index store must fail closed.
        other = self.root / "instance_b"
        clone_release(other)
        run_in(other, "tools/initialize.py")
        run_in(other, "-m", "tools.context_index.context_index", "build")

        cat_db = relocated / ".local/stores/catalog/catalog.sqlite3"
        saved_cat = cat_db.read_bytes()
        cat_db.write_bytes((other / ".local/catalog/catalog.sqlite3").read_bytes())
        try:
            with self.assertRaisesRegex(catalog.CatalogError, "Cross-instance or substituted catalog store"):
                catalog.status(relocated)
            stale_index = run_in(relocated, "-m", "tools.context_index.context_index", "status", check=False)
            self.assertNotEqual(stale_index.returncode, 0)
        finally:
            cat_db.write_bytes(saved_cat)

        idx_db = relocated / ".local/stores/index.sqlite3"
        saved_idx = idx_db.read_bytes()
        idx_db.write_bytes((other / "tools/context_index/.local/index.sqlite3").read_bytes())
        try:
            substituted_idx = run_in(relocated, "-m", "tools.context_index.context_index", "search", "Portable", check=False)
            self.assertNotEqual(substituted_idx.returncode, 0)
            self.assertIn("corpus_root", substituted_idx.stderr)
        finally:
            idx_db.write_bytes(saved_idx)

        # Changing storage_roots without --migrate-storage refuses operation; interrupted migration rolls back cleanly.
        migrated_roots = dict(custom_roots, catalog_home=".local/migrated/catalog",
                              review_events_home=".local/migrated/review-events")
        (relocated / "config/policy.local.json").write_text(json.dumps({"storage_roots": migrated_roots}))
        with self.assertRaisesRegex(catalog.CatalogError, "migrate-storage"):
            catalog.status(relocated)

        pre_cat_token = saved_cat
        with self.assertRaisesRegex(RuntimeError, "Simulated interruption"):
            policy.migrate_storage(relocated, interrupt_after=1)
        self.assertTrue((relocated / ".local/migration.json").is_file())
        with self.assertRaisesRegex(catalog.CatalogError, "recover-migration"):
            catalog.status(relocated)

        recovered = run_in(relocated, "tools/initialize.py", "--recover-migration")
        self.assertTrue(recovered["recovered"])
        self.assertFalse((relocated / ".local/migration.json").exists())
        (relocated / "config/policy.local.json").write_text(json.dumps({"storage_roots": custom_roots}))
        self.assertEqual(cat_db.read_bytes(), pre_cat_token)

        # Complete migration moves stores and preserves state tokens.
        (relocated / "config/policy.local.json").write_text(json.dumps({"storage_roots": migrated_roots}))
        before_token = policy.read_instance_binding(relocated)["catalog_store_id"]
        mig_report = run_in(relocated, "tools/initialize.py", "--migrate-storage")
        self.assertTrue(mig_report["migrated"])
        self.assertEqual(catalog.status(relocated)["store_id"], before_token)
        self.assertTrue((relocated / ".local/migrated/catalog/catalog.sqlite3").is_file())

    def test_portable_roots_edge_cases_and_instance_policy_propagation(self):
        from tools.intake import adapters as intake_adapters, store as intake_store
        from tools.extraction import docling_adapter, docling_bridge

        kit = self.root / "instance_custom"
        clone_release(kit)
        custom_roots = {
            "catalog_home": ".local/custom/catalog",
            "review_events_home": ".local/custom/review-events",
            "context_index_db": ".local/custom/index.sqlite3",
            "intake_home": ".local/queues/recovery/intake",
            "extractions_home": "data/extractions/.local/custom-runs",
            "diagnostics_log": ".local/custom/diagnostics.jsonl",
            "selection_manifest": "config/custom-selection.json",
        }
        (kit / "config/policy.local.json").write_text(json.dumps({
            "audience": "custom-instance-audience",
            "handling": "Custom instance handling notice.",
            "markdown_roots": ["documents", "custom_docs"],
            "original_roots": ["sources", "custom_sources"],
            "storage_roots": custom_roots,
        }))
        (kit / "custom_docs").mkdir(exist_ok=True)
        (kit / "custom_docs/AGENTS.md").write_text("# Custom Context\n\nCompanion context rules.\n")
        (kit / "custom_docs/guide.md").write_text("# Custom Guide\n\nUniqueCustomToken content.\n")
        (kit / "config/custom-selection.json").write_text(json.dumps({
            "schema_version": 1,
            "audience": "custom-instance-audience",
            "documents": [
                {
                    "path": "custom_docs/AGENTS.md",
                    "role": "rules",
                    "context": [],
                },
                {
                    "path": "custom_docs/guide.md",
                    "role": "guide",
                    "context": ["custom_docs/AGENTS.md"],
                },
            ],
            "context_rules": {},
        }))
        # Keep shipped default config/selection.json template alongside custom-selection.json
        self.assertTrue((kit / "config/selection.json").is_file())

        def run_in(target, *args, check=True):
            result = subprocess.run([sys.executable, *args], cwd=target, capture_output=True, text=True)
            if check:
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            return result

        # 1. Initialize instance + deeply nested intake_home (.local/queues/recovery/intake)
        run_in(kit, "tools/initialize.py")
        intake_init = intake_store.initialize(kit)
        self.assertTrue((kit / ".local/queues/recovery/intake/intake.sqlite3").is_file())
        self.assertEqual(stat.S_IMODE((kit / ".local/queues/recovery").stat().st_mode), 0o700)

        # 2. Search envelope (hit and zero-result) reflects custom instance audience and handling
        stage_spec = {
            "id": "build-idx",
            "kind": "publication",
            "operation": "index-build",
            "arguments": {
                "manifest": "config/custom-selection.json",
                "db": ".local/custom/index.sqlite3",
            },
        }
        intent = intake_adapters.prepare(kit, stage_spec)
        self.assertTrue(intent["selection_validated"])
        ev = intake_adapters.execute(kit, stage_spec, intent)
        self.assertEqual(ev["boundary"], "publication-confirmed")
        self.assertTrue(intake_adapters.availability(kit, stage_spec, ev)["available"])

        hit_res = run_in(kit, "-m", "tools.context_index.context_index", "search", "UniqueCustomToken")
        self.assertEqual(hit_res["audience"], "custom-instance-audience")
        self.assertEqual(hit_res["handling"], "Custom instance handling notice.")
        empty_res = run_in(kit, "-m", "tools.context_index.context_index", "search", "AbsentTokenXYZ")
        self.assertEqual(empty_res["audience"], "custom-instance-audience")
        self.assertEqual(empty_res["handling"], "Custom instance handling notice.")
        self.assertIn("absence", empty_res)

        # 3. Missing authority DB restore works on an initialized instance and enforces store_id lineage
        cat_backup = catalog.backup(kit)
        rev_backup = events.backup(kit)
        cat_db = kit / ".local/custom/catalog/catalog.sqlite3"
        rev_db = kit / ".local/custom/review-events/events.sqlite3"
        cat_db.unlink()
        rev_db.unlink()
        restored_cat = catalog.restore(kit, Path(cat_backup["snapshot"]))
        self.assertEqual(restored_cat["command"], "restore")
        self.assertTrue(cat_db.is_file())
        restored_rev = events.restore(kit, Path(rev_backup["snapshot"]))
        self.assertEqual(restored_rev["command"], "restore")
        self.assertTrue(rev_db.is_file())

        # 4. rendition_api(kit) binds validate_context to kit (custom_docs/AGENTS.md)
        api = index.rendition_api(kit)
        api.validate_context(["custom_docs/AGENTS.md"])

        # 5. Interruption before journal update rolls back planned moves cleanly
        diagnostics.Log(kit, custom_roots["diagnostics_log"]).record_read(
            {"command": "get"},
            started_at="2026-10-22T12:00:00+00:00",
            completed_at="2026-10-22T12:00:01+00:00",
            duration_ms=10,
        )
        next_roots = dict(
            custom_roots,
            catalog_home=".local/moved/catalog",
            review_events_home=".local/moved/review-events",
            diagnostics_log=".local/moved/diagnostics.jsonl",
            selection_manifest="config/moved-selection.json",
        )
        (kit / "config/policy.local.json").write_text(json.dumps({
            "audience": "custom-instance-audience",
            "handling": "Custom instance handling notice.",
            "markdown_roots": ["documents", "custom_docs"],
            "original_roots": ["sources", "custom_sources"],
            "storage_roots": next_roots,
        }))
        with self.assertRaisesRegex(RuntimeError, "before journal update"):
            policy.migrate_storage(kit, interrupt_before_journal=1)
        self.assertFalse(cat_db.exists())
        self.assertTrue((kit / ".local/moved/catalog/catalog.sqlite3").is_file())
        recovered = policy.recover_migration(kit)
        self.assertTrue(recovered["recovered"])
        self.assertIn("catalog_home", recovered["rolled_back"])
        self.assertTrue(cat_db.is_file())
        self.assertFalse((kit / ".local/migration.json").exists())

        # Complete migration moves diagnostics_log and selection_manifest too
        completed = policy.migrate_storage(kit)
        self.assertTrue(completed["migrated"])
        self.assertTrue((kit / ".local/moved/diagnostics.jsonl").is_file())
        self.assertTrue((kit / "config/moved-selection.json").is_file())

        # 6. Docling prepare/verify/register succeeds on an instance with custom policy and extractions_home
        (kit / "config/policy.local.json").write_text(json.dumps({
            "audience": "custom-instance-audience",
            "handling": "Custom instance handling notice.",
            "markdown_roots": ["documents", "custom_docs"],
            "original_roots": ["sources", "custom_sources"],
            "storage_roots": next_roots,
        }))
        self.assertEqual(docling_bridge.binding(kit)["policy_sha256"], policy.for_root(kit)[1])
        self.assertEqual(docling_adapter._extractions_home(kit), Path("data/extractions/.local/custom-runs"))
        raw_pdf = b"%PDF-1.7\ncustom instance docling input"
        (kit / "custom_sources").mkdir(exist_ok=True)
        (kit / "custom_sources/input.pdf").write_bytes(raw_pdf)
        cat_state = catalog.status(kit)["state_token"]
        catalog.submit(
            kit,
            [
                {"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}},
                {"id": "D1", "kind": "receipt", "data": {
                    "source_ids": ["S1"], "artifact_id": "A-pdf", "provenance": []}},
                {"id": "A-pdf", "kind": "artifact", "data": {
                    "path": "custom_sources/input.pdf", "parent_id": None, "receipt_id": "D1",
                    "role": "original", "audience": "source", "coverage": "pending-extraction",
                    "reason": "Custom instance Docling test.", "presence": "present",
                    "sha256": hashlib.sha256(raw_pdf).hexdigest(), "hash_basis": "observed",
                    "bytes": len(raw_pdf), "extraction": "pending", "handling_refs": [],
                    "provenance": [],
                }},
            ],
            actor="operator", request_id="seed-custom-docling", expected_state=cat_state,
            reason="Seed custom instance Docling receipt",
        )
        stub_report = {
            "conversion": {"schema": "docling-rendition-v1", "format": "pdf",
                           "document": {"texts": [], "tables": []}, "pages": [],
                           "confidence": {}, "warnings": [], "omissions": []},
            "runtime": {"docling": "test"},
            "profile": {"version": 2, "limits": {}},
            "assets": {"schema": "docling-assets-v1", "files": [], "sha256": "a" * 64},
        }
        worker_sha = hashlib.sha256(docling_adapter.WORKER.read_bytes()).hexdigest()
        with patch.object(
            docling_adapter,
            "_convert_with_identity",
            side_effect=lambda *args: (deepcopy(stub_report), worker_sha),
        ):
            prep = docling_adapter.run(
                kit, "prepare", "D1", "pdf", Path("/unused/models"), Path("/unused/python")
            )
            self.assertTrue(prep["rendition"].startswith("data/extractions/.local/custom-runs/D1/"))
            docling_adapter.run(
                kit, "verify", "D1", "pdf", Path("/unused/models"), Path("/unused/python")
            )
            reg = docling_adapter.run(
                kit, "register", "D1", "pdf", Path("/unused/models"), Path("/unused/python"),
                actor="operator", expected_state=catalog.status(kit)["state_token"],
            )
            self.assertTrue(reg["registered"])

        # 7. Populated extractions_home refuses auto-migration before moving anything
        bad_roots = dict(next_roots, extractions_home="data/extractions/.local/other-runs")
        (kit / "config/policy.local.json").write_text(json.dumps({
            "audience": "custom-instance-audience",
            "handling": "Custom instance handling notice.",
            "markdown_roots": ["documents", "custom_docs"],
            "original_roots": ["sources", "custom_sources"],
            "storage_roots": bad_roots,
        }))
        with self.assertRaisesRegex(ValueError, "populated extractions_home"):
            policy.migrate_storage(kit)
        self.assertFalse((kit / ".local/migration.json").exists())


class AbsenceClassificationTests(WorkspaceCase):
    """A zero-result search must explain itself without quoting the corpus."""

    def corpus(self, documents):
        (self.root / "documents").mkdir(exist_ok=True)
        for name, text in documents.items():
            (self.root / "documents" / name).write_text(text)
        self.config["documents"] = [
            {"path": f"documents/{name}", "role": "toolkit-documentation", "context": []}
            for name in documents
        ]
        self.build()

    def absence(self, query, path=None):
        with closing(index.readonly(self.db)) as db:
            result = index.search(db, query, 5, path)
            self.assertEqual(result["hits"], [])
            return result["absence"]

    def test_absent_corpus_distinguishes_collection_gaps(self):
        self.corpus({"a.md": "# Alpha\n\nalphaonly material.\n"})
        absence = self.absence("zzgapone zzgaptwo")
        self.assertEqual(absence["classification"], "absent-corpus")
        self.assertEqual((absence["terms"], absence["absent_terms"]), (2, 2))
        self.assertEqual(absence["sections_matching_any"], 0)
        self.assertIn("not evidence that the subject does not exist", absence["caveat"])

    def test_partial_and_uncombined_are_separate_signals(self):
        self.corpus({"a.md": "# Alpha\n\nalphaonly material.\n",
                     "b.md": "# Beta\n\nbetaonly material.\n"})
        partial = self.absence("alphaonly zzgapone")
        self.assertEqual(partial["classification"], "absent-partial")
        self.assertEqual((partial["terms"], partial["absent_terms"]), (2, 1))
        uncombined = self.absence("alphaonly betaonly")
        self.assertEqual(uncombined["classification"], "present-uncombined")
        self.assertEqual(uncombined["absent_terms"], 0)
        self.assertEqual(uncombined["sections_matching_any"], 2)
        self.assertIn("retrieval or selection limit", uncombined["caveat"])

    def test_sections_matching_any_counts_distinct_sections_when_terms_overlap(self):
        self.corpus({"a.md": "# Alpha\n\nalphaonly betaonly material.\n"})
        partial = self.absence("alphaonly betaonly zzgapone")
        self.assertEqual(partial["classification"], "absent-partial")
        self.assertEqual((partial["terms"], partial["absent_terms"]), (3, 1))
        self.assertEqual(partial["sections_matching_any"], 1)

    def test_classification_respects_the_requested_path_scope(self):
        self.corpus({"a.md": "# Alpha\n\nalphaonly material.\n",
                     "b.md": "# Beta\n\nbetaonly material.\n"})
        absence = self.absence("alphaonly", "documents/b.md")
        self.assertEqual(absence["classification"], "absent-corpus")
        self.assertEqual(absence["scope"], "path")

    def test_absence_never_echoes_query_terms_or_source_text(self):
        self.corpus({"a.md": "# Alpha\n\nalphaonly confidential material.\n"})
        absence = self.absence("alphaonly zzsecretterm")
        serialized = json.dumps(absence)
        for leaked in ("zzsecretterm", "alphaonly", "confidential"):
            self.assertNotIn(leaked, serialized)
        self.assertEqual(set(absence), {"classification", "terms", "absent_terms",
                                        "sections_matching_any", "scope", "caveat"})

    def test_successful_search_carries_no_absence_report(self):
        self.corpus({"a.md": "# Alpha\n\nalphaonly material.\n"})
        with closing(index.readonly(self.db)) as db:
            result = index.search(db, "alphaonly", 5, None)
        self.assertTrue(result["hits"])
        self.assertNotIn("absence", result)


class RetrievalDiagnostics(unittest.TestCase):
    """The diagnostics log must refuse anything that is not a bounded, body-free count."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.log = diagnostics.Log(self.root, ".local/diagnostics/retrieval.jsonl")

    def stamps(self, minutes=0):
        completed = datetime(2026, 10, 22, 12, minutes, tzinfo=timezone.utc)
        return {"started_at": (completed - timedelta(milliseconds=5)).isoformat(),
                "completed_at": completed.isoformat(), "duration_ms": 5}

    def search_payload(self, hits, absence=None):
        payload = {"command": "search", "hits": [{"locator": "documents/a.md#x"}] * hits}
        if absence is not None:
            payload["absence"] = absence
        return payload

    def test_records_a_classified_empty_search(self):
        record = self.log.record_search(self.search_payload(0, {
            "classification": "absent-corpus", "terms": 2, "absent_terms": 2,
            "sections_matching_any": 0, "scope": "selection", "caveat": "ignored"}), **self.stamps())
        self.assertEqual(record["outcome"], "empty")
        self.assertEqual(record["classification"], "absent-corpus")
        self.assertNotIn("caveat", record)

    def test_refuses_query_text_and_unknown_fields(self):
        for extra in ({"query": "secret"}, {"locator": "documents/a.md#x"},
                      {"excerpt": "source text"}, {"terms": "two"}):
            with self.subTest(extra=extra), self.assertRaises(diagnostics.DiagnosticsError):
                self.log.write(run_id=str(uuid.uuid4()), operation="search",
                               outcome="empty", **self.stamps(), **extra)

    def test_refuses_contradictory_classifications(self):
        contradictions = [
            {"classification": "absent-corpus", "terms": 2, "absent_terms": 1, "scope": "selection"},
            {"classification": "present-uncombined", "terms": 2, "absent_terms": 2, "scope": "selection"},
            {"classification": "absent-partial", "terms": 2, "absent_terms": 2, "scope": "selection"},
            {"classification": "absent-corpus", "terms": 1, "absent_terms": 2, "scope": "selection"},
        ]
        for fields in contradictions:
            with self.subTest(fields=fields), self.assertRaises(diagnostics.DiagnosticsError):
                self.log.write(run_id=str(uuid.uuid4()), operation="search",
                               outcome="empty", **self.stamps(), **fields)

    def test_refuses_outcome_and_count_disagreement(self):
        with self.assertRaises(diagnostics.DiagnosticsError):
            self.log.write(run_id=str(uuid.uuid4()), operation="search", outcome="hit",
                           hits=0, **self.stamps())
        with self.assertRaises(diagnostics.DiagnosticsError):
            self.log.write(run_id=str(uuid.uuid4()), operation="search", outcome="empty",
                           hits=3, **self.stamps())

    def test_error_records_keep_class_names_not_messages(self):
        record = self.log.record_error("ToolError", **self.stamps())
        self.assertEqual(record["error_type"], "ToolError")
        with self.assertRaises(diagnostics.DiagnosticsError):
            self.log.record_error("Reader failed: /private/path", **self.stamps())

    def test_log_is_private_and_appended(self):
        self.log.record_search(self.search_payload(2), **self.stamps())
        self.log.record_search(self.search_payload(1), **self.stamps(1))
        self.assertEqual(stat.S_IMODE(self.log.path.stat().st_mode), 0o600)
        self.assertEqual(len(diagnostics.read_records(self.log.path)), 2)

    def test_report_separates_collection_from_findability(self):
        self.log.record_search(self.search_payload(0, {
            "classification": "absent-corpus", "terms": 2, "absent_terms": 2,
            "sections_matching_any": 0, "scope": "selection"}), **self.stamps())
        self.log.record_search(self.search_payload(0, {
            "classification": "present-uncombined", "terms": 2, "absent_terms": 0,
            "sections_matching_any": 4, "scope": "selection"}), **self.stamps(1))
        self.log.record_search(self.search_payload(3), **self.stamps(2))
        summary = diagnostics.report(self.log.path)
        self.assertEqual(summary["searches"], 3)
        self.assertEqual(summary["outcomes"], {"empty": 2, "hit": 1})
        self.assertEqual(summary["empty_classifications"],
                         {"absent-corpus": 1, "present-uncombined": 1})
        self.assertAlmostEqual(summary["collection_gap_rate"], 1 / 3, places=4)
        self.assertAlmostEqual(summary["findability_gap_rate"], 1 / 3, places=4)
        self.assertTrue(any("not store" in limit or "not stored" in limit
                            for limit in summary["limits"]))

    def test_pruning_is_explicit_and_reports_both_counts(self):
        for minute in range(3):
            self.log.record_search(self.search_payload(1), **self.stamps(minute))
        result = diagnostics.prune(self.log.path, 1)
        self.assertEqual((result["before"], result["after"], result["removed"]), (3, 1, 2))
        self.assertEqual(len(diagnostics.read_records(self.log.path)), 1)

    def test_prune_and_write_serialize_across_shared_lock_file(self):
        for minute in range(3):
            self.log.record_search(self.search_payload(1), **self.stamps(minute))
        entered_read = threading.Event()
        release_read = threading.Event()
        original_read_records = diagnostics.read_records

        def paused_read_records(path):
            records = original_read_records(path)
            entered_read.set()
            release_read.wait(timeout=5)
            return records

        writer_result = {}

        def concurrent_writer():
            entered_read.wait(timeout=5)
            writer_result["record"] = self.log.record_search(self.search_payload(2), **self.stamps(10))

        writer = threading.Thread(target=concurrent_writer)
        writer.start()
        with patch.object(diagnostics, "read_records", side_effect=paused_read_records):
            threading.Timer(0.05, release_read.set).start()
            result = diagnostics.prune(self.log.path, 1)
        writer.join(timeout=5)
        self.assertEqual((result["before"], result["after"], result["removed"]), (3, 1, 2))
        records = diagnostics.read_records(self.log.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1]["run_id"], writer_result["record"]["run_id"])

    def test_reads_are_counted_but_never_attributed(self):
        """A read is counted as a bare event, carrying no locator, excerpt or count."""
        record = self.log.record_read({"command": "get", "pieces": [
            {"locator": "documents/a.md#x", "text": "secret source text"}]}, **self.stamps())
        self.assertEqual((record["operation"], record["outcome"]), ("read", "hit"))
        self.assertEqual(set(record) - diagnostics.BASE, set())
        serialized = json.dumps(record)
        for leaked in ("documents/a.md", "secret source text", "locator", "pieces"):
            self.assertNotIn(leaked, serialized)

    def test_read_records_refuse_search_only_fields(self):
        """Term counts and absence classification describe a search; a read cannot carry them."""
        for extra in ({"hits": 1}, {"terms": 2}, {"scope": "selection"},
                      {"classification": "absent-corpus", "terms": 1,
                       "absent_terms": 1, "scope": "selection"}):
            with self.subTest(extra=extra), self.assertRaises(diagnostics.DiagnosticsError):
                self.log.write(run_id=str(uuid.uuid4()), operation="read",
                               outcome="hit", **self.stamps(), **extra)
        with self.assertRaises(diagnostics.DiagnosticsError):
            self.log.write(run_id=str(uuid.uuid4()), operation="read",
                           outcome="empty", **self.stamps())

    def test_report_counts_reads_without_deriving_a_ratio(self):
        """Raw search and read counts are reported; no quality ratio is computed from them."""
        self.log.record_search(self.search_payload(3), **self.stamps())
        self.log.record_search(self.search_payload(0, {
            "classification": "absent-corpus", "terms": 1, "absent_terms": 1,
            "sections_matching_any": 0, "scope": "selection"}), **self.stamps(1))
        self.log.record_read({"command": "get"}, **self.stamps(2))
        self.log.record_read({"command": "get-many"}, **self.stamps(3))
        summary = diagnostics.report(self.log.path)
        self.assertEqual((summary["searches"], summary["reads"]), (2, 2))
        self.assertEqual(summary["outcomes"], {"hit": 1, "empty": 1})
        self.assertNotIn("read_per_answered_search", summary)
        self.assertNotIn("read_per_answered_search", summary["interpretation"])
        self.assertTrue(any("No search-to-read ratio" in limit for limit in summary["limits"]))
        self.assertAlmostEqual(summary["collection_gap_rate"], 0.5, places=4)
        self.assertTrue(any("not a basis for pruning" in limit for limit in summary["limits"]))

    def test_rejects_unsafe_log_paths(self):
        for value in ("../escape.jsonl", "retrieval.txt"):
            with self.subTest(value=value), self.assertRaises(diagnostics.DiagnosticsError):
                diagnostics.log_path(self.root, value)


class ScriptConventionTests(unittest.TestCase):
    MODULES = (
        "tools.initialize", "tools.package", "tools.schemas.export_schemas",
        "tools.catalog.inventory", "tools.extraction.docling_extract",
    )

    def test_commands_expose_strict_parser_and_injectable_main(self):
        for name in self.MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                self.assertTrue(callable(module.parser))
                with self.assertRaises(SystemExit):
                    module.parser().parse_args(["--roo", "/tmp"])
                with self.assertRaises(SystemExit) as stopped:
                    module.main(["--help"])
                self.assertEqual(stopped.exception.code, 0)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "file.txt").write_text("release")

    def manifest(self, value):
        (self.root / "release-files.json").write_text(json.dumps(value))

    def test_manifest_rejects_invalid_entries_before_filesystem_access(self):
        for value, message in (
            (["file.txt", "file.txt"], "unique"),
            (["file.txt", 7], "entry 2"),
            (["../file.txt"], "unsafe"),
            (["store.sqlite3"], "unsafe"),
        ):
            with self.subTest(value=value):
                self.manifest(value)
                with self.assertRaisesRegex((TypeError, ValueError), message):
                    package.load_release_manifest(self.root)

    def test_release_paths_rejects_missing_and_symlinked_files(self):
        self.manifest(["missing.txt"])
        with self.assertRaisesRegex(ValueError, "Missing"):
            package.release_paths(self.root)
        (self.root / "target.txt").write_text("target")
        (self.root / "link.txt").symlink_to("target.txt")
        self.manifest(["link.txt"])
        with self.assertRaisesRegex(ValueError, "symlinked"):
            package.release_paths(self.root)

    def test_package_rejects_symlinked_output_parent(self):
        real = self.root / "real"
        real.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            package.validate_output_path(linked / "release.zip")


class Documentation(unittest.TestCase):
    def test_component_map_paths_and_documented_module_commands_are_current(self):
        readme = (ROOT / "README.md").read_text()
        manifest = dict(package.release_paths(ROOT))
        for path in re.findall(r"`((?:tools|tests|docs)/[^` ]+/?)`", readme):
            if ".local" not in path:
                with self.subTest(path=path):
                    prefix = path.rstrip("/") + "/"
                    self.assertTrue(path in manifest or any(item.startswith(prefix) for item in manifest), path)
        modules = set(re.findall(
            r"(?:\.venv/bin/)?python\d* -m ([a-zA-Z0-9_.]+)", readme
        ))
        self.assertIn("tools.catalog.catalog_store", modules)
        for module in modules - {"pip", "venv"}:
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-m", module, "--help"], cwd=ROOT,
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_librarian_identity_and_role_documents_are_packaged(self):
        paths = dict(package.release_paths(ROOT))
        self.assertEqual((ROOT / "README.md").read_text().splitlines()[0], "# The Librarian")
        self.assertTrue({
            "ROADMAP.md", "agents/librarian.md", "agents/contributions.md",
            "agents/refiner.md", "agents/coder.md",
        }.issubset(paths))

    def test_retired_execution_documents_are_not_packaged(self):
        paths = dict(package.release_paths(ROOT))
        for name in (
            "docs/superpowers/plans/2026-10-20-docling-ingestion.md",
            "docs/superpowers/plans/2026-10-22-ingestion-completion.md",
            "docs/superpowers/specs/2026-10-20-docling-ingestion-design.md",
            "docs/superpowers/specs/2026-10-22-ingestion-completion-design.md",
        ):
            self.assertNotIn(name, paths)

    def test_relative_document_links_are_packaged(self):
        paths = dict(package.release_paths(ROOT))
        for name, source in paths.items():
            if source.suffix != ".md":
                continue
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", source.read_text()):
                if "://" in target or target.startswith("#"):
                    continue
                linked = (source.parent / target.split("#", 1)[0]).resolve()
                with self.subTest(document=name, target=target):
                    self.assertIn(linked.relative_to(ROOT).as_posix(), paths)


if __name__ == "__main__":
    unittest.main()
