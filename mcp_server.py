import sys
import os

# Make sure both the project root and core/ are importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "core"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent"))

from mcp.server.fastmcp import FastMCP

from agent.retrieval import search_sessions, search_events
from agent.memory import recall_memory, fetch_cluster, save_identity, save_note, delete_note

mcp = FastMCP("Clippy-Vision MCP")


@mcp.tool()
def search_sessions_tool(question: str) -> str:
    """Search session summaries in the activity database.
    Use for: broad time windows (yesterday, this week), daily/weekly overviews,
    what-did-I-work-on questions, project topics, task recaps.
    Returns paragraph summaries — NOT granular event detail.
    If the result says the info isn't there, call search_events_tool next."""
    return search_sessions(question)

@mcp.tool()
def search_events_tool(question: str) -> str:
    """Search individual events in the activity database.
    Use for: specific messages, OCR screen text, exact URLs, clipboard content,
    app usage, WhatsApp/email content, fine-grained timestamps, copy-paste history.
    Returns raw event rows with screen/OCR data.
    If the result says the info isn't there, call search_sessions_tool next."""
    return search_events(question)

@mcp.tool()
def recall_memory_tool() -> str:
    """List all long-term memory clusters with labels and descriptions.
    Use when the user asks what you know about them, or before fetching a specific cluster."""
    return recall_memory()

@mcp.tool()
def fetch_cluster_tool(label: str) -> str:
    """Get all facts stored in a named memory cluster.
    Use after recall_memory_tool to get the full content of a specific topic.
    Pass the cluster label exactly as returned by recall_memory_tool."""
    return fetch_cluster(label)

@mcp.tool()
def save_identity_tool(field: str, op: str, value: str = "", items: list[str] = None) -> str:
    """Save a personal fact about the user.
    op='set' for scalar facts (name, location, job).
    op='add_items' with items=[] for adding to a list (hobbies, skills).
    op='override' only when the user explicitly corrects a previous fact.
    op='remove_items' with items=[] to remove from a list."""
    return save_identity(field=field, value=value, op=op, items=items)

@mcp.tool()
def save_note_tool(note: str) -> str:
    """Save a free-form note or reminder the user wants remembered."""
    return save_note(note)
@mcp.tool()

def delete_note_tool(note_text: str) -> str:
    """Delete a note or memory fact the user wants forgotten.
    Use when the user says 'forget', 'delete', 'remove', or 'don't remember that'.
    Matches by substring — pass the key phrase or exact text from the note."""
    return delete_note(note_text)
    
if __name__ == "__main__":
    mcp.run(transport="stdio")