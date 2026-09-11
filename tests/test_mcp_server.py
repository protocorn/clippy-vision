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

EXPECTED_TOOLS = {
    "search_sessions_tool",
    "search_events_tool",
    "recall_memory_tool",
    "fetch_cluster_tool",
    "save_identity_tool",
    "save_note_tool",
    "delete_note_tool",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _launcher_path() -> Path:
    root = _repo_root()
    if sys.platform == "win32":
        return root / "scripts" / "clippy-mcp.cmd"
    return root / "scripts" / "clippy-mcp"


@unittest.skipIf(ClientSession is None, "mcp package is not installed")
class McpServerSmokeTests(unittest.IsolatedAsyncioTestCase):
    """PowerShell: `$env:PYTHONPATH=(Get-Location).Path; python -m unittest tests.test_mcp_server`."""

    async def _assert_read_tools(self, parameters: StdioServerParameters) -> None:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                tool_names = {tool.name for tool in listed.tools}
                self.assertEqual(EXPECTED_TOOLS, tool_names)

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

    async def test_stdio_server_lists_and_calls_read_tools(self) -> None:
        root = _repo_root()
        with tempfile.TemporaryDirectory(prefix="clippy-mcp-") as data_dir:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root)
            env["CLIPPY_DATA_DIR"] = data_dir
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[str(root / "mcp_server.py")],
                env=env,
            )
            await self._assert_read_tools(parameters)

    async def test_launcher_stdio_lists_and_calls_read_tools(self) -> None:
        """Same smoke path, but through scripts/clippy-mcp — how MCP clients will spawn us."""
        launcher = _launcher_path()
        self.assertTrue(launcher.is_file(), f"missing launcher: {launcher}")

        with tempfile.TemporaryDirectory(prefix="clippy-mcp-launcher-") as data_dir:
            env = os.environ.copy()
            env["CLIPPY_DATA_DIR"] = data_dir
            env["CLIPPY_PYTHON"] = sys.executable
            # Intentionally do NOT set PYTHONPATH — the launcher must set it.
            env.pop("PYTHONPATH", None)
            parameters = StdioServerParameters(
                command=str(launcher),
                args=[],
                env=env,
            )
            await self._assert_read_tools(parameters)
