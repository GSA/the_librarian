"""Full-text MCP audit log: what each agent asked and exactly what it was given."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/context-mcp/src"))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.support import clone_release
from the_librarian_mcp import audit
from the_librarian_mcp.config import Config, parser


def stamps():
    return {"started_at": "2026-11-02T00:00:00+00:00",
            "completed_at": "2026-11-02T00:00:01+00:00", "duration_ms": 1000.0}


class AuditLog(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()

    def tearDown(self):
        self.directory.cleanup()

    def log(self, **options):
        return audit.Log(self.root, Path(".local/audit/pi-mcp-access.jsonl"), "pi", **options)

    def test_record_keeps_full_text_arguments_hash_and_references(self):
        text = json.dumps({"command": "get", "pieces": [
            {"locator": "documents/a.md#x", "source_hash": "h1", "text": "secret body"}]})
        record = self.log().write(tool="get", arguments={"locator": "documents/a.md#x"},
                                  outcome="ok", text=text, **stamps())
        path = self.root / ".local/audit/pi-mcp-access.jsonl"
        [stored] = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(stored, record)
        self.assertEqual(stored["schema"], audit.SCHEMA)
        self.assertEqual(stored["agent_label"], "pi")
        self.assertEqual(stored["returned_text"], text)
        self.assertEqual(stored["returned_sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(stored["returned_chars"], len(text))
        self.assertEqual(stored["arguments"], {"locator": "documents/a.md#x"})
        self.assertEqual(stored["references"], {
            "locators": ["documents/a.md#x"], "source_hashes": ["h1"], "record_ids": []})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_references_cover_get_many_catalog_and_review_events(self):
        payload = {"requests": [{"input_locator": "documents/b.md"}],
                   "documents": {"d1": {"source_hash": "h2"}},
                   "records": [{"id": "D1", "kind": "receipt"}],
                   "events": [{"event": {"event_id": "E1"}}]}
        references = audit.references(json.dumps(payload))
        self.assertEqual(references, {"locators": ["documents/b.md"],
                                      "source_hashes": ["h2"], "record_ids": ["D1", "E1"]})
        self.assertEqual(audit.references("not json"),
                         {"locators": [], "source_hashes": [], "record_ids": []})

    def test_error_outcome_records_the_returned_message_and_type(self):
        record = self.log().write(tool="get", arguments={}, outcome="error",
                                  text="Reader failed", error_type="ToolError", **stamps())
        self.assertEqual((record["outcome"], record["error_type"]), ("error", "ToolError"))
        self.assertEqual(record["returned_text"], "Reader failed")

    def test_paths_outside_local_symlinks_and_unsafe_modes_are_refused(self):
        for value in ("../out.jsonl", "documents/audit.jsonl", ".local/audit/a.txt", "/tmp/a.jsonl"):
            with self.subTest(value=value), self.assertRaises(audit.AuditError):
                audit.Log(self.root, Path(value), "pi")
        (self.root / ".local").mkdir()
        outside = self.root / "elsewhere"
        outside.mkdir()
        (self.root / ".local/audit").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(audit.AuditError):
            self.log()
        (self.root / ".local/audit").unlink()
        log = self.log()
        log.write(tool="status", arguments={}, outcome="ok", text="{}", **stamps())
        log.path.chmod(0o644)
        with self.assertRaisesRegex(audit.AuditError, "private"):
            log.write(tool="status", arguments={}, outcome="ok", text="{}", **stamps())
        log.path.chmod(0o600)
        log.path.parent.chmod(0o755)
        with self.assertRaisesRegex(audit.AuditError, "private"):
            log.write(tool="status", arguments={}, outcome="ok", text="{}", **stamps())

    def test_report_reads_a_custom_log_and_its_rotations(self):
        log = audit.Log(self.root, Path(".local/private/pi.jsonl"), "pi",
                        max_bytes=audit.MIN_FILE_BYTES)
        for _ in range(4):
            log.write(tool="search", arguments={}, outcome="ok",
                      text="x" * (audit.MIN_FILE_BYTES // 3), **stamps())
        self.assertEqual(audit.report(self.root)["records"], 0)
        summary = audit.report(self.root, Path(".local/private/pi.jsonl"))
        self.assertEqual((summary["records"], summary["files"]), (4, 2))
        with self.assertRaises(audit.AuditError):
            audit.report(self.root, Path("documents/pi.jsonl"))

    def test_concurrent_bindings_share_an_existing_private_directory(self):
        first = audit.Log(self.root, Path(".local/audit/pi-mcp-access.jsonl"), "pi")
        second = audit.Log(self.root, Path(".local/audit/copilot-mcp-access.jsonl"), "copilot")
        self.assertEqual(first.path.parent, second.path.parent)
        (self.root / ".local/shared").mkdir(mode=0o755, parents=True)
        (self.root / ".local/shared").chmod(0o755)
        with self.assertRaisesRegex(audit.AuditError, "private"):
            audit.Log(self.root, Path(".local/shared/pi.jsonl"), "pi")
        self.assertEqual(stat.S_IMODE((self.root / ".local/shared").stat().st_mode), 0o755)

    def test_labels_and_outcomes_are_closed(self):
        for label in ("", "Pi", "a b", "x" * 65, "../pi"):
            with self.subTest(label=label), self.assertRaises(audit.AuditError):
                audit.Log(self.root, Path(".local/audit/a.jsonl"), label)
        with self.assertRaises(audit.AuditError):
            self.log().write(tool="status", arguments={}, outcome="maybe", text="", **stamps())

    def test_rotation_preserves_every_record_without_overwriting(self):
        log = self.log(max_bytes=audit.MIN_FILE_BYTES)
        body = "x" * (audit.MIN_FILE_BYTES // 3)
        for index in range(7):
            log.write(tool="search", arguments={"query": str(index)}, outcome="ok",
                      text=body, **stamps())
        files = audit.log_files(log.path)
        self.assertGreater(len(files), 1)
        self.assertEqual(files[0], log.path.with_name("pi-mcp-access.1.jsonl"))
        queries = [json.loads(line)["arguments"]["query"]
                   for path in files for line in path.read_text().splitlines()]
        self.assertEqual(sorted(queries), [str(index) for index in range(7)])
        for path in files:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertLessEqual(path.stat().st_size, audit.MIN_FILE_BYTES)

    def test_report_is_body_free_and_verifies_hashes(self):
        log = self.log()
        log.write(tool="search", arguments={"query": "zzsecret"}, outcome="ok",
                  text="zzbody", **stamps())
        log.write(tool="get", arguments={}, outcome="error", text="nope",
                  error_type="ToolError", **stamps())
        report = audit.report(self.root)
        raw = json.dumps(report)
        self.assertNotIn("zzsecret", raw)
        self.assertNotIn("zzbody", raw)
        self.assertEqual(report["records"], 2)
        self.assertEqual(report["by_tool"], {"get": 1, "search": 1})
        self.assertEqual(report["by_agent"], {"pi": 2})
        self.assertEqual(report["by_outcome"], {"error": 1, "ok": 1})
        self.assertEqual(report["returned_chars"], 10)
        self.assertEqual(report["hash_mismatches"], 0)
        lines = log.path.read_text().splitlines()
        tampered = json.loads(lines[0])
        tampered["returned_text"] = "edited"
        log.path.write_text(json.dumps(tampered) + "\n" + lines[1] + "\n")
        self.assertEqual(audit.report(self.root)["hash_mismatches"], 1)
        output = subprocess.run(
            [sys.executable, "-m", "the_librarian_mcp.audit", "--root", str(self.root), "report"],
            capture_output=True, text=True, check=True,
            env=dict(os.environ, PYTHONPATH=str(ROOT / "tools/context-mcp/src"))).stdout
        self.assertEqual(json.loads(output)["records"], 2)
        self.assertNotIn("zzsecret", output)


class AuditConfig(unittest.TestCase):
    def test_audit_is_off_by_default_and_derives_its_path_from_the_label(self):
        arguments = parser("test")
        config = Config(**vars(arguments.parse_args(["--root", str(ROOT)])))
        self.assertIsNone(config.audit_log)
        config = Config(**vars(arguments.parse_args(
            ["--root", str(ROOT), "--audit-log", "--agent-label", "copilot"])))
        self.assertEqual(config.audit_log, Path(".local/audit/copilot-mcp-access.jsonl"))
        for argv in (["--audit-log", "documents/x.jsonl", "--agent-label", "pi"],
                     ["--audit-log", "--agent-label", "A B"], ["--audit-log"],
                     ["--agent-label", "pi"],
                     ["--audit-log", ".local/diagnostics/retrieval.jsonl", "--agent-label", "pi",
                      "--diagnostics-log", ".local/diagnostics/retrieval.jsonl"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                Config(**vars(arguments.parse_args(["--root", str(ROOT), *argv])))


class AuditedServer(unittest.IsolatedAsyncioTestCase):
    def kit(self, directory):
        root = Path(directory).resolve() / "kit"
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
        return root

    def parameters(self, root, *extra):
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "the_librarian_mcp.server", "--root", str(root), *extra],
            env=dict(os.environ, PYTHONPATH=str(root / "tools/context-mcp/src")))

    async def test_every_call_records_exactly_what_the_client_received(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.kit(directory)
            calls = [("status", {}), ("search", {"query": "zzuniquesubject"}),
                     ("catalog", {}), ("review_events", {"scope": "documentation"}),
                     ("get", {"locator": "documents/missing.md"}),
                     ("search", {"query": "x", "limit": 0}),
                     ("review_events", {})]
            responses = []
            async with stdio_client(self.parameters(
                    root, "--audit-log", "--agent-label", "pi")) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for tool, arguments in calls:
                        responses.append(await session.call_tool(tool, arguments))
                    locator = json.loads(responses[1].content[0].text)["hits"][0]["locator"]
                    for tool, arguments in (("get", {"locator": locator}),
                                            ("get_many", {"locators": [locator]})):
                        calls.append((tool, arguments))
                        responses.append(await session.call_tool(tool, arguments))
            log = root / ".local/audit/pi-mcp-access.jsonl"
            records = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(records), len(calls))
            for record, (tool, arguments), response in zip(records, calls, responses):
                with self.subTest(tool=tool, arguments=arguments):
                    self.assertEqual(record["tool"], tool)
                    self.assertEqual(record["arguments"], arguments)
                    self.assertEqual(record["agent_label"], "pi")
                    self.assertEqual(record["returned_text"], response.content[0].text)
                    self.assertEqual(record["returned_sha256"], hashlib.sha256(
                        response.content[0].text.encode()).hexdigest())
                    self.assertEqual(record["outcome"], "error" if response.is_error else "ok")
            self.assertEqual([r["outcome"] for r in records],
                             ["ok", "ok", "ok", "ok", "error", "error", "error", "ok", "ok"])
            self.assertIn("zzuniquesubject", records[7]["returned_text"])
            self.assertIn(locator, records[7]["references"]["locators"])
            self.assertTrue(records[7]["references"]["source_hashes"])

    async def test_audit_failure_withholds_the_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.kit(directory)
            async with stdio_client(self.parameters(
                    root, "--audit-log", "--agent-label", "pi")) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    first = await session.call_tool("search", {"query": "zzuniquesubject"})
                    self.assertFalse(first.is_error, first)
                    (root / ".local/audit/pi-mcp-access.jsonl").chmod(0o644)
                    refused = await session.call_tool("search", {"query": "zzuniquesubject"})
            self.assertTrue(refused.is_error)
            self.assertIn("Audit log could not be recorded", refused.content[0].text)
            self.assertNotIn("zzuniquesubject", refused.content[0].text)

    async def test_audit_stays_off_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.kit(directory)
            async with stdio_client(self.parameters(root)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.call_tool("search", {"query": "zzuniquesubject"})
            self.assertFalse((root / ".local/audit").exists())


if __name__ == "__main__":
    unittest.main()
