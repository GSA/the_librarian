"""Exercise actual stdio MCP against an empty independent copy."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/context-mcp/src"))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.support import clone_release
from the_librarian_mcp.config import Config, parser


class MCPConfig(unittest.TestCase):
    def test_paths_are_derived_from_the_toolkit_root(self):
        config = Config(root=ROOT)
        self.assertEqual(config.toolkit_root, ROOT)
        self.assertEqual(config.context_module, "tools.context_index.context_index")
        self.assertEqual(config.event_module, "tools.review_events.event_reader")
        self.assertEqual(config.diagnostics_path, ROOT / "tools/diagnostics/retrieval_log.py")

    def test_incomplete_clone_missing_new_dependencies_is_rejected(self):
        for missing in ("tools/context_index/cli.py", "tools/catalog/contracts.py"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve() / "kit"
                clone_release(root)
                (root / missing).unlink()
                with self.assertRaisesRegex(ValueError, "complete trusted toolkit clone"):
                    Config(root=root)

    def test_other_root_cannot_shadow_trusted_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "tools/context_index").mkdir(parents=True)
            (root / "tools/context_index/context_index.py").write_text("raise RuntimeError('shadow')")
            with self.assertRaisesRegex(ValueError, "complete trusted toolkit clone"):
                Config(root=root)

    def test_script_path_overrides_are_not_accepted(self):
        arguments = parser("test")
        for option in ("--reader", "--event-reader"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                arguments.parse_args(["--root", "/tmp", option, "/tmp/tool.py"])


class MCP(unittest.IsolatedAsyncioTestCase):
    async def test_empty_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            clone_release(root)
            setup = subprocess.run([sys.executable, str(root / "tools/initialize.py")],
                                   capture_output=True, text=True)
            self.assertEqual(setup.returncode, 0, setup.stderr)
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "the_librarian_mcp.server", "--root", str(root),],
                env=dict(os.environ, PYTHONPATH=str(root / "tools/context-mcp/src")),
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual({t.name for t in tools.tools},
                                     {"status", "search", "get", "get_many", "catalog", "review_events"})
                    for tool, arguments in (
                        ("status", {}), ("search", {"query": "documentation"}),
                        ("catalog", {}), ("review_events", {"scope": "documentation"}),
                    ):
                        response = await session.call_tool(tool, arguments)
                        self.assertFalse(response.is_error, response)
                        payload = json.loads(response.content[0].text)
                        if tool == "status":
                            self.assertEqual(payload["documents"], 0)
                        elif tool == "search":
                            self.assertEqual(payload["hits"], [])
                        else:
                            self.assertEqual(payload["count"], 0)
                    response = await session.call_tool("get", {"locator": "documents/missing.md"})
                    self.assertTrue(response.is_error)

    async def test_opt_in_diagnostics_record_absence_without_the_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            clone_release(root)
            setup = subprocess.run([sys.executable, str(root / "tools/initialize.py")],
                                   capture_output=True, text=True)
            self.assertEqual(setup.returncode, 0, setup.stderr)
            log = root / ".local/diagnostics/retrieval.jsonl"
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "the_librarian_mcp.server", "--root", str(root),
                      "--diagnostics-log", ".local/diagnostics/retrieval.jsonl"],
                env=dict(os.environ, PYTHONPATH=str(root / "tools/context-mcp/src")),
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    response = await session.call_tool("search", {"query": "zzsecretsubject"})
                    self.assertFalse(response.is_error, response)
                    payload = json.loads(response.content[0].text)
                    self.assertEqual(payload["hits"], [])
                    self.assertEqual(payload["absence"]["classification"], "absent-corpus")
                    failure = await session.call_tool("search", {"query": "???"})
                    self.assertTrue(failure.is_error)
                    missing = await session.call_tool("get", {"locator": "documents/missing.md"})
                    self.assertTrue(missing.is_error)
                    await session.call_tool("status", {})
                    await session.call_tool("catalog", {})
            raw = log.read_text()
            self.assertNotIn("zzsecretsubject", raw)
            self.assertNotIn("documents/missing.md", raw)
            records = [json.loads(line) for line in raw.splitlines() if line.strip()]
            self.assertEqual(len(records), 3, "only search and reads are observed")
            self.assertEqual(records[0]["outcome"], "empty")
            self.assertEqual(records[0]["classification"], "absent-corpus")
            self.assertEqual((records[1]["operation"], records[1]["outcome"]), ("search", "error"))
            self.assertEqual((records[2]["operation"], records[2]["outcome"]), ("read", "error"))
            for record in records[1:]:
                self.assertTrue(record["error_type"].isidentifier())

    async def test_diagnostics_count_reads_without_recording_what_was_read(self):
        """A successful get is counted as a bare event, with no locator in the log."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            clone_release(root)
            (root / "documents").mkdir(parents=True, exist_ok=True)
            (root / "documents/guide.md").write_text(
                "# Guide\n\n## Overview\n\nzzuniquesubject material for retrieval.\n")
            (root / "config/selection.json").write_text(json.dumps({
                "schema_version": 1, "audience": "authorized-local",
                "documents": [{"path": "documents/guide.md",
                               "role": "toolkit-documentation", "context": []}],
                "context_rules": {}, "renditions": []}))
            subprocess.run([sys.executable, str(root / "tools/initialize.py")],
                           capture_output=True, text=True, check=True)
            log = root / ".local/diagnostics/retrieval.jsonl"
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "the_librarian_mcp.server", "--root", str(root),
                      "--diagnostics-log", ".local/diagnostics/retrieval.jsonl"],
                env=dict(os.environ, PYTHONPATH=str(root / "tools/context-mcp/src")),
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    found = await session.call_tool("search", {"query": "zzuniquesubject"})
                    self.assertFalse(found.is_error, found)
                    hits = json.loads(found.content[0].text)["hits"]
                    self.assertTrue(hits)
                    fetched = await session.call_tool("get", {"locator": hits[0]["locator"]})
                    self.assertFalse(fetched.is_error, fetched)
                    self.assertIn("zzuniquesubject", fetched.content[0].text)
            raw = log.read_text()
            for leaked in ("zzuniquesubject", "documents/guide.md", "guide", "Overview"):
                self.assertNotIn(leaked, raw)
            records = [json.loads(line) for line in raw.splitlines() if line.strip()]
            self.assertEqual([(r["operation"], r["outcome"]) for r in records],
                             [("search", "hit"), ("read", "hit")])
            self.assertNotIn("hits", records[1])
            summary = subprocess.run(
                [sys.executable, "-m", "tools.diagnostics.retrieval_log",
                 "--root", str(root), "report"], cwd=root,
                capture_output=True, text=True, check=True)
            report = json.loads(summary.stdout)
            self.assertEqual((report["searches"], report["reads"]), (1, 1))
            self.assertNotIn("read_per_answered_search", report)

    async def test_diagnostics_stay_off_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            clone_release(root)
            subprocess.run([sys.executable, str(root / "tools/initialize.py")],
                           capture_output=True, text=True, check=True)
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "the_librarian_mcp.server", "--root", str(root),],
                env=dict(os.environ, PYTHONPATH=str(root / "tools/context-mcp/src")),
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.call_tool("search", {"query": "documentation"})
            self.assertFalse((root / ".local/diagnostics").exists())


if __name__ == "__main__":
    unittest.main()
