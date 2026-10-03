"""Exercise actual stdio MCP against an empty independent copy."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from test_kit import clone


class MCP(unittest.IsolatedAsyncioTestCase):
    async def test_empty_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            clone(root)
            setup = subprocess.run([sys.executable, str(root / "tools/initialize.py")],
                                   capture_output=True, text=True)
            self.assertEqual(setup.returncode, 0, setup.stderr)
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "qualified_context_mcp.server", "--root", str(root),
                      "--reader", str(root / "tools/context-index/context_index.py")],
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


if __name__ == "__main__":
    unittest.main()
