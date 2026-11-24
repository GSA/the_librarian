"""Start the packaged MCP server over stdio and call status and search on the empty instance."""
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(root):
    parameters = StdioServerParameters(command=sys.executable,
                                       args=["-m", "the_librarian_mcp.server", "--root", root])
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            status = await session.call_tool("status", {})
            assert not status.is_error, status
            assert json.loads(status.content[0].text)["documents"] == 0, status
            search = await session.call_tool("search", {"query": "documentation"})
            assert not search.is_error, search
            assert json.loads(search.content[0].text)["hits"] == [], search


asyncio.run(main(sys.argv[1]))
