import os
import sys
import logging
from contextlib import redirect_stdout

# MCP clients treat stderr noise as errors; keep protocol logs quiet.
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("mcp.server").setLevel(logging.WARNING)

# Make sure both the project root and core/ are importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "core"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent"))

from mcp.server.fastmcp import FastMCP

with redirect_stdout(sys.stderr):
    from agent.memory import (
        delete_note,
        fetch_cluster,
        recall_memory,
        save_identity,
        save_note,
    )
    from agent.mcp_query import (
        activity_coverage,
        app_time_summary,
        dumps,
        get_screenshot,
        list_screenshots,
        list_sessions_range,
        list_urls,
        search_with_bounds,
    )
    from agent.retrieval import search_events, search_sessions
    from agent.tools import find_files, list_workspace_roots

mcp = FastMCP("Clippy-Vision MCP")


def _call_tool(function, *args, **kwargs):
    with redirect_stdout(sys.stderr):
        return function(*args, **kwargs)


@mcp.tool()
def search_sessions_tool(question: str) -> str:
    """Search session summaries in the activity database.
    Use for: broad time windows (yesterday, this week), daily/weekly overviews,
    what-did-I-work-on questions, project topics, task recaps.
    Returns paragraph summaries — NOT granular event detail.
    If the result says the info isn't there, call search_events_tool next.
    For exact date ranges prefer list_sessions_tool or search_bounded_tool."""
    return _call_tool(search_sessions, question)


@mcp.tool()
def search_events_tool(question: str) -> str:
    """Search individual events in the activity database.
    Use for: specific messages, OCR screen text, exact URLs, clipboard content,
    app usage, WhatsApp/email content, fine-grained timestamps, copy-paste history.
    Returns raw event rows with screen/OCR data.
    If the result says the info isn't there, call search_sessions_tool next.
    For exact date ranges prefer search_bounded_tool."""
    return _call_tool(search_events, question)


@mcp.tool()
def search_bounded_tool(
    question: str,
    start: str = "",
    end: str = "",
    table: str = "events",
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Keyword search with explicit time bounds (no LLM date guessing).
    start/end: epoch seconds or YYYY-MM-DD (local). table: 'events' or 'sessions'.
    Use when you know the calendar window and need pagination via limit/offset."""
    return _call_tool(
        lambda: dumps(
            search_with_bounds(
                question,
                start=start or None,
                end=end or None,
                limit=limit,
                offset=offset,
                table=table if table in ("events", "sessions") else "events",
            )
        )
    )


@mcp.tool()
def list_sessions_tool(
    start: str = "",
    end: str = "",
    limit: int = 40,
    offset: int = 0,
) -> str:
    """Chronological deduped session list for a time window.
    Prefer this over search_sessions_tool for day/week timelines.
    start/end: epoch or YYYY-MM-DD."""
    return _call_tool(
        lambda: dumps(
            list_sessions_range(
                start or None, end or None, limit=limit, offset=offset
            )
        )
    )


@mcp.tool()
def activity_coverage_tool(
    start: str,
    end: str = "",
    bucket_hours: float = 1.0,
) -> str:
    """Hourly (or custom) event-count buckets so capture gaps are honest.
    Empty buckets mean no events that hour — often capture was off.
    start required (epoch or YYYY-MM-DD); end defaults to now."""
    return _call_tool(
        lambda: dumps(
            activity_coverage(start, end or None, bucket_hours=bucket_hours)
        )
    )


@mcp.tool()
def app_time_summary_tool(start: str, end: str = "") -> str:
    """Approximate per-app foreground time from event gaps in a window.
    start/end: epoch or YYYY-MM-DD. Durations are approximate (gaps capped at 5 min)."""
    return _call_tool(
        lambda: dumps(app_time_summary(start, end or None))
    )


@mcp.tool()
def list_urls_tool(
    pattern: str = "",
    start: str = "",
    end: str = "",
    limit: int = 40,
) -> str:
    """Distinct URLs seen in active_url / OCR within a time window.
    pattern filters substring (e.g. 'greenhouse'). Deduped with counts."""
    return _call_tool(
        lambda: dumps(
            list_urls(
                pattern=pattern,
                start=start or None,
                end=end or None,
                limit=limit,
            )
        )
    )


@mcp.tool()
def list_screenshots_tool(start: str = "", end: str = "", limit: int = 30) -> str:
    """List screenshots (or OCR-backed stubs) in a time window.
    image_available=false means the JPEG expired; OCR preview may still be present."""
    return _call_tool(
        lambda: dumps(
            list_screenshots(start or None, end or None, limit=limit)
        )
    )


@mcp.tool()
def get_screenshot_tool(filename: str = "", timestamp: float = 0.0) -> str:
    """Fetch one screenshot by filename or unix timestamp.
    Returns local path when the image still exists; otherwise OCR/event text backup.
    Screenshots use adaptive TTL (important frames kept longer, capped)."""
    ts = timestamp if timestamp and timestamp > 0 else None
    return _call_tool(
        lambda: dumps(get_screenshot(filename=filename, timestamp=ts))
    )


@mcp.tool()
def recall_memory_tool(
    query: str = "",
    include_stale: bool = False,
    min_freshness: float = 0.2,
) -> str:
    """List long-term memory clusters ranked by freshness score.
    Pass query to run semantic recall instead of listing.
    Stale/recovered clusters are hidden unless include_stale=true."""
    return _call_tool(
        recall_memory,
        query=query,
        include_stale=include_stale,
        min_freshness=min_freshness,
    )


@mcp.tool()
def fetch_cluster_tool(label: str) -> str:
    """Get all facts stored in a named memory cluster (with freshness annotations).
    Use after recall_memory_tool to get the full content of a specific topic.
    Pass the cluster label exactly as returned by recall_memory_tool."""
    return _call_tool(fetch_cluster, label)


@mcp.tool()
def save_identity_tool(field: str, op: str, value: str = "", items: list[str] = None) -> str:
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


@mcp.tool()
def find_files_tool(name: str, under: str = "", limit: int = 15) -> str:
    """Search trusted workspace roots for a filename or substring."""
    return _call_tool(find_files, name, under=under, limit=limit)


@mcp.tool()
def list_workspace_roots_tool() -> str:
    """List folders Clippy may search with find_files_tool."""
    return _call_tool(list_workspace_roots)


if __name__ == "__main__":
    mcp.run(transport="stdio")
