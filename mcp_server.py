"""Clippy Vision MCP server (stdio transport).

Spawned by MCP clients (Claude Desktop, Cursor) with no Clippy environment, so it
resolves import roots and the data directory itself before importing anything that
opens the database at module scope.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _default_data_dir() -> Path:
    """Mirror the userData/data location Electron writes to when packaged."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "Clippy Vision" / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Clippy Vision" / "data"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "clippy-vision" / "data"


def _bootstrap() -> None:
    for path in (_ROOT, _ROOT / "core", _ROOT / "agent"):
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)

    if not (os.environ.get("CLIPPY_DATA_DIR") or "").strip():
        data_dir = _default_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["CLIPPY_DATA_DIR"] = str(data_dir)

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")

    # Those env vars only affect child processes; this interpreter already picked its
    # console encoding, and the retrieval stack logs non-ASCII characters.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True)

    sys.stdout = _ProtocolStdout(sys.stdout, sys.stderr)


class _ProtocolStdout:
    """Sends stray print() output to stderr while the transport keeps real stdout.

    Under the stdio transport, stdout carries JSON-RPC frames, but the capture,
    router, and retrieval modules all log progress with print() — at import time as
    well as during a call. The MCP SDK builds its writer from `sys.stdout.buffer`,
    so exposing the real buffer here keeps the protocol intact while every text
    write is diverted.
    """

    def __init__(self, real, err):
        self.buffer = real.buffer
        self._err = err

    def write(self, data):
        return self._err.write(data)

    def flush(self):
        return self._err.flush()

    def __getattr__(self, name):
        return getattr(self._err, name)


_bootstrap()

from mcp.server.fastmcp import FastMCP  # noqa: E402

from agent.memory import delete_note, fetch_cluster, recall_memory, save_identity, save_note  # noqa: E402
from agent.prefetch.pipeline import routed_context  # noqa: E402
from agent.retrieval import search_events, search_sessions  # noqa: E402
from agent.router import load_classifier  # noqa: E402

mcp = FastMCP("Clippy-Vision MCP")

_NO_ROUTE = (
    "Router unavailable. Fall back to search_sessions_tool for overviews and "
    "search_events_tool for URLs, OCR text or clipboard detail."
)
_NOT_PERSONAL = (
    "Router classified this as a general question with no personal-activity intent, "
    "so nothing was retrieved. Answer it directly, or call search_sessions_tool / "
    "search_events_tool if the user insists their own history is involved."
)
_EMPTY = (
    "Prefetch ran for this route but found nothing. Call search_sessions_tool, then "
    "search_events_tool if the answer needs granular detail."
)

# Client-supplied history is unbounded, and this text is embedded and time-parsed,
# so keep it near the 3 turns the in-app agent uses rather than accepting a transcript.
_MAX_PRIOR_TURNS = 6
_MAX_TURN_CHARS = 500


def _clean_turns(turns: list[str] | None) -> list[str]:
    if not turns:
        return []
    return [
        turn.strip()[:_MAX_TURN_CHARS]
        for turn in turns[-_MAX_PRIOR_TURNS:]
        if isinstance(turn, str) and turn.strip()
    ]


@mcp.tool()
def query_activity(question: str, recent_turns: list[str] | None = None) -> str:
    """Answer questions about the user's own computer activity and remembered facts.

    PREFER THIS over search_sessions_tool / search_events_tool for anything like
    "what did I work on", "when did I", "which paper was I reading", "what do you
    know about me". It runs Clippy's query router and pulls context from the right
    source (time window, topic, specific artifact, or long-term memory) in one call.
    Answer from what it returns; only fall back to the search tools if it comes back
    empty or too thin.

    Pass the last few messages of your conversation in recent_turns, oldest first,
    each prefixed with the speaker, e.g.
    ["User: what did I do yesterday", "Assistant: you debugged the capture loop"].
    Follow-ups like "what about the day before?" resolve against those turns, so
    supply them whenever the question depends on what was already said. Still phrase
    `question` as fully as you can — routing reads it on its own."""
    result = routed_context(question, _clean_turns(recent_turns))
    if not result.routed:
        return _NO_ROUTE

    header = f"[route={result.route} confidence={result.confidence:.2f}"
    if result.secondary:
        header += f" secondary={','.join(result.secondary)}"
    header += "]"

    if result.route == "casual":
        return f"{header}\n{_NOT_PERSONAL}"
    return f"{header}\n\n{result.context}" if result.prefetched else f"{header}\n{_EMPTY}"


def _call_tool(function, *args, **kwargs):
    with redirect_stdout(sys.stderr):
        return function(*args, **kwargs)


@mcp.tool()
def search_sessions_tool(question: str) -> str:
    """Search session summaries in the activity database.
    Use for: broad time windows (yesterday, this week), daily/weekly overviews,
    what-did-I-work-on questions, project topics, task recaps.
    Returns paragraph summaries — NOT granular event detail.
    Prefer query_activity first; use search_sessions_tool when it returns nothing useful.
    If the result says the info isn't there, call search_events_tool next."""
    return _call_tool(search_sessions, question)


@mcp.tool()
def search_events_tool(question: str) -> str:
    """Search individual events in the activity database.
    Use for: specific messages, OCR screen text, exact URLs, clipboard content,
    app usage, WhatsApp/email content, fine-grained timestamps, copy-paste history.
    Returns raw event rows with screen/OCR data.
    Prefer query_activity first; use search_events_tool when it returns nothing useful.
    If the result says the info isn't there, call search_sessions_tool next."""
    return _call_tool(search_events, question)


@mcp.tool()
def recall_memory_tool() -> str:
    """List all long-term memory clusters with labels and descriptions.
    Use when the user asks what you know about them, or before fetching a specific cluster."""
    return _call_tool(recall_memory)


@mcp.tool()
def fetch_cluster_tool(label: str) -> str:
    """Get all facts stored in a named memory cluster.
    Use after recall_memory_tool to get the full content of a specific topic.
    Pass the cluster label exactly as returned by recall_memory_tool."""
    return _call_tool(fetch_cluster, label)


@mcp.tool()
def save_identity_tool(field: str, op: str, value: str = "", items: list[str] | None = None) -> str:
    """Save a personal fact about the user.
    op='set' for scalar facts (name, location, job).
    op='add_items' with items=[] for adding to a list (hobbies, skills).
    op='override' only when the user explicitly corrects a previous fact.
    op='remove_items' with items=[] to remove from a list."""
    return _call_tool(save_identity, field=field, value=value, op=op, items=items)


@mcp.tool()
def save_note_tool(note: str) -> str:
    """Save a free-form note or reminder the user wants remembered."""
    return _call_tool(save_note, note)


@mcp.tool()
def delete_note_tool(note_text: str) -> str:
    """Delete a note or memory fact the user wants forgotten.
    Use when the user says 'forget', 'delete', 'remove', or 'don't remember that'.
    Matches by substring — pass the key phrase or exact text from the note."""
    return _call_tool(delete_note, note_text)


def _run_self_check() -> int:
    """Verify imports, data dir, and tool registration without starting stdio MCP."""
    data_dir = Path(os.environ.get("CLIPPY_DATA_DIR") or _default_data_dir())
    payload: dict = {
        "ok": False,
        "tool_count": 0,
        "tools": [],
        "data_dir": str(data_dir),
    }
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".mcp_health_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)

        tools = asyncio.run(mcp.list_tools())
        names = sorted(tool.name for tool in tools)
        if not names:
            raise RuntimeError("No MCP tools registered.")
        payload["ok"] = True
        payload["tool_count"] = len(names)
        payload["tools"] = names
    except Exception as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"

    # Import noise may already be on stderr; use a tagged line Electron can find.
    stream = getattr(sys, "__stderr__", None) or sys.stderr
    stream.write("CLIPPY_MCP_HEALTH:" + json.dumps(payload) + "\n")
    stream.flush()
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        raise SystemExit(_run_self_check())

    # Pull the router model in while the client is still handshaking, so the first
    # query does not pay for loading transformers inside the request thread.
    threading.Thread(target=load_classifier, daemon=True, name="router-classifier-warmup").start()
    mcp.run(transport="stdio")
