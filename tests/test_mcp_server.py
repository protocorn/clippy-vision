from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None


@unittest.skipIf(ClientSession is None, "mcp package is not installed")
class McpServerSmokeTests(unittest.IsolatedAsyncioTestCase):
    """PowerShell: `$env:PYTHONPATH=(Get-Location).Path; python -m unittest tests.test_mcp_server`."""

    async def test_stdio_server_lists_and_calls_read_tools(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="clippy-mcp-") as data_dir:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root)
            env["CLIPPY_DATA_DIR"] = data_dir
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[str(root / "mcp_server.py")],
                env=env,
            )

            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    tool_names = {tool.name for tool in listed.tools}
                    self.assertEqual(
                        {
                            "search_sessions_tool",
                            "search_events_tool",
                            "recall_memory_tool",
                            "fetch_cluster_tool",
                            "save_identity_tool",
                            "save_note_tool",
                            "delete_note_tool",
                        },
                        tool_names,
                    )

                    search = await session.call_tool(
                        "search_sessions_tool",
                        {"question": "What did I work on today?"},
                    )
                    self.assertFalse(search.isError, search.content)
                    self.assertTrue(search.content)
                    self.assertIn("search_sessions:", search.content[0].text)

                    recall = await session.call_tool("recall_memory_tool", {})
                    self.assertFalse(recall.isError, recall.content)
                    self.assertEqual(recall.content[0].text, "No memory clusters yet.")
