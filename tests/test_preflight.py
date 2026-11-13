"""Drive the real runtime probe of librarian-preflight with a stubbed environment.

The probe is a subprocess of the running interpreter. A throwaway ``sitecustomize``
on PYTHONPATH fakes only the facts under test (Python version, the installed
``mcp`` version, FTS5) in that child; the production probe code runs unchanged.
Everything happens in temporary workspaces; no real instance is opened.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/context-mcp/src"))

from tests.support import clone_release
from the_librarian_mcp import preflight
from the_librarian_mcp.config import Config

SHIM = '''
import json, os, sqlite3, sys
fake = json.loads(os.environ["LIBRARIAN_PREFLIGHT_FAKE"])
if "python" in fake:
    # Libraries imported by the probe keep seeing the real interpreter; only
    # comparisons made by the probe's own "-c" code (filename "<string>") are faked.
    real_info = sys.version_info
    def faked(name):
        def compare(self, other):
            subject = tuple(fake["python"]) if sys._getframe(1).f_code.co_filename == "<string>" else tuple(real_info)
            return getattr(tuple, name)(subject, tuple(other))
        return compare
    class FakeVersionInfo(tuple):
        major, minor, micro = real_info.major, real_info.minor, real_info.micro
        releaselevel, serial = real_info.releaselevel, real_info.serial
        __lt__, __le__, __gt__, __ge__ = (faked(n) for n in ("__lt__", "__le__", "__gt__", "__ge__"))
    sys.version_info = FakeVersionInfo(real_info)
if "mcp" in fake:
    import importlib.metadata as metadata
    real = metadata.version
    metadata.version = lambda name: fake["mcp"] if name == "mcp" else real(name)
if fake.get("no_fts5"):
    class NoFts5(sqlite3.Connection):
        def execute(self, sql, *args):
            if "fts5" in sql.lower():
                raise sqlite3.OperationalError("no such module: fts5")
            return super().execute(sql, *args)
    real_connect = sqlite3.connect
    sqlite3.connect = lambda *a, **k: real_connect(*a, factory=NoFts5, **k)
'''


class PreflightRuntimeProbe(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = Path(directory.name).resolve()
        self.root = home / "kit"
        clone_release(self.root)
        self.shim = home / "shim"
        self.shim.mkdir()
        (self.shim / "sitecustomize.py").write_text(SHIM, encoding="utf-8")
        self.config = Config(root=self.root)

    def environment(self, fake: dict) -> dict:
        existing = os.environ.get("PYTHONPATH")
        path = os.pathsep.join(filter(None, [str(self.shim), existing]))
        return {"PYTHONPATH": path, "LIBRARIAN_PREFLIGHT_FAKE": json.dumps(fake)}

    def runtime_check(self, fake: dict) -> dict:
        with mock.patch.dict(os.environ, self.environment(fake)):
            return preflight.preflight(self.config)["checks"][0]

    def assert_refused(self, fake: dict, diagnostic: str):
        check = self.runtime_check(fake)
        self.assertEqual(check["name"], "runtime")
        self.assertFalse(check["ok"])
        self.assertNotEqual(check["exit_code"], 0)
        self.assertIn(diagnostic, check["error"])
        self.assertNotIn("result", check)

    def test_python_below_3_11_is_refused(self):
        for version in ((3, 10, 14), (3, 9, 0)):
            with self.subTest(version=version):
                self.assert_refused({"python": version}, "Python 3.11+ required")

    def test_any_mcp_version_other_than_the_pin_is_refused(self):
        for version in ("2.1.9", "2.2.1", "3.0.0"):
            with self.subTest(version=version):
                self.assert_refused({"mcp": version}, "This adapter requires mcp==2.2.0")

    def test_missing_fts5_is_refused(self):
        self.assert_refused({"no_fts5": True}, "no such module: fts5")

    def test_passing_probe_reports_the_versions_in_use(self):
        check = self.runtime_check({"python": [3, 11, 0], "mcp": "2.2.0"})
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["result"], {
            "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
            "mcp": "2.2.0", "fts5": True,
        })

    def test_unstubbed_probe_passes_and_reports_this_interpreter(self):
        with mock.patch.dict(os.environ, self.environment({})):
            check = preflight.preflight(self.config)["checks"][0]
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["result"]["python"], sys.version.split()[0])
        self.assertEqual(check["result"]["mcp"], "2.2.0")
        self.assertTrue(check["result"]["fts5"])

    def test_cli_exits_non_zero_and_skips_refresh_when_the_runtime_is_refused(self):
        argv = ["--root", str(self.root), "--refresh-index"]
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.environment({"mcp": "2.1.9"})), \
                contextlib.redirect_stdout(out):
            code = preflight.main(argv)
        report = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        refresh = next(c for c in report["checks"] if c["name"] == "refresh_index")
        self.assertTrue(refresh["skipped"])
        self.assertFalse(refresh["ok"])


if __name__ == "__main__":
    unittest.main()
