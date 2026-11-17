"""Release manifest, clean-copy kit and module layout contracts."""

from contextlib import redirect_stderr, redirect_stdout
import io
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools import package
from tests.support import ROOT, clone_release


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

    def test_packaged_intake_modules_import(self):
        for name in ("tools.intake.model_review", "tools.intake.cohort"):
            with self.subTest(module=name):
                process = subprocess.run(
                    [sys.executable, "-c", f"import {name}"],
                    cwd=self.kit, capture_output=True, text=True)
                self.assertEqual(process.returncode, 0, process.stderr)

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
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    module.parser().parse_args(["--roo", "/tmp"])
                with redirect_stdout(io.StringIO()) as shown, self.assertRaises(SystemExit) as stopped:
                    module.main(["--help"])
                self.assertEqual(stopped.exception.code, 0)
                self.assertIn("usage:", shown.getvalue())


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

    # Tracked files deliberately kept out of the kit. Keys ending in "/" are prefixes.
    UNPACKAGED_TRACKED = {
        ".kata.toml": "Kata project binding for this repository's own work tracking",
        ".github/": "hosting and CI configuration for this repository, not toolkit behavior",
        "ruff.toml": "repository lint configuration; no packaged code or test reads it",
    }

    def test_manifest_covers_every_tracked_file(self):
        try:
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT,
                                 capture_output=True, text=True)
        except FileNotFoundError:
            self.skipTest("git is not installed")
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != ROOT.resolve():
            self.skipTest("not a git checkout of the toolkit (packaged kits carry no .git)")
        tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                                 text=True, check=True).stdout.split("\0")
        tracked = {name for name in tracked if name}
        manifest = package.load_release_manifest(ROOT)

        def excluded(name):
            return any(name.startswith(key) if key.endswith("/") else name == key
                       for key in self.UNPACKAGED_TRACKED)

        self.assertEqual(sorted(name for name in tracked if name not in manifest and not excluded(name)), [],
                         "add to release-files.json or to UNPACKAGED_TRACKED with a reason")
        self.assertEqual(sorted(name for name in manifest if excluded(name)), [])
        self.assertEqual(sorted(name for name in manifest
                                if name not in tracked or not (ROOT / name).is_file()), [])

    def test_package_rejects_symlinked_output_parent(self):
        real = self.root / "real"
        real.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            package.validate_output_path(linked / "release.zip")


if __name__ == "__main__":
    unittest.main()
