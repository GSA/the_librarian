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
from tools.diagnostics import retrieval_log as diagnostics
from tools.extraction import extract
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

    def test_clean_copy_contains_installable_review_gate(self):
        required = {".githooks/pre-commit", "tools/review/roborev_precommit.py",
                    "tools/review/install_hooks.py", "tools/review/README.md",
                    "tools/review/test_precommit.py"}
        self.assertTrue(required <= {name for name, _ in package.release_paths(ROOT)})
        for name in required:
            self.assertEqual((self.kit / name).read_bytes(), (ROOT / name).read_bytes())
        # Source archives do not promise executable-bit preservation; the
        # documented activation prerequisite must be applied to a copied hook.
        (self.kit / ".githooks/pre-commit").chmod(0o755)
        launcher = self.root / "scoped-wrapper"
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        subprocess.run(["git", "init", "-q", str(self.kit)], env=env, check=True)
        result = subprocess.run([sys.executable, "tools/review/install_hooks.py",
                                 "--launcher", str(launcher)], cwd=self.kit, env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        configured = subprocess.check_output(["git", "-C", str(self.kit), "config",
                                               "--local", "--get", "core.hooksPath"], env=env)
        self.assertEqual(configured.strip(), b".githooks")

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
