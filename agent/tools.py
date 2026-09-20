import json

from agent.memory import delete_note, save_identity, save_note
from agent.retrieval import search_events, search_sessions
from core.fs_search import find_files_json
from core.workspace_roots import (
    format_roots_for_prompt,
    list_roots,
    remember_root,
)


def find_files(name: str, under: str = "", limit: int = 15) -> str:
    """Search trusted folders on disk for a filename / substring."""
    return find_files_json(name, under=(under or None), limit=limit or 15)


def remember_workspace_root(path: str, label: str = "") -> str:
    """Trust a folder for future find_files searches."""
    path = (path or "").strip()
    if not path:
        return json.dumps({"ok": False, "error": "empty path"})
    try:
        root = remember_root(path, label=label or None, source="user")
        return json.dumps({"ok": True, "root": root}, default=str)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def list_workspace_roots() -> str:
    """List trusted folders Clippy may search."""
    roots = list_roots(enabled_only=False)
    return json.dumps(
        {
            "ok": True,
            "roots": roots,
            "prompt": format_roots_for_prompt(),
        },
        default=str,
    )


TOOLS = {
    "search_sessions": search_sessions,
    "search_events": search_events,
    "save_identity": save_identity,
    "save_note": save_note,
    "delete_note": delete_note,
    "find_files": find_files,
    "remember_workspace_root": remember_workspace_root,
    "list_workspace_roots": list_workspace_roots,
}


def build_prefetch_tool_schema(
    *,
    routes: list[str],
    char_count: int,
    synopsis: str,
    empty: bool,
) -> dict:
    """Dynamic schema for get_prefetched_context — only registered when a bundle exists."""
    route_label = ", ".join(routes) if routes else "unknown"
    if empty:
        when = (
            "Bundle reports little/no matching activity for the routed window. "
            "Call once to confirm emptiness, then use search_sessions or search_events "
            "if the user still needs a broader look."
        )
    else:
        when = (
            "START HERE for activity/memory recall questions that match this bundle "
            "(yesterday / this week / topic / artifact). Call before search_sessions "
            "or search_events. Skip this tool for open-file/open-url actions, "
            "casual chat, or when the synopsis clearly does not match the question."
        )
    description = (
        f"Return the pre-retrieved activity/memory bundle for THIS turn only. "
        f"Routes: {route_label}. Size: ~{char_count} characters. "
        f"Synopsis: {synopsis} "
        f"{when} "
        f"Takes no arguments. Do not invent what is inside — call the tool to read it."
    )
    return {
        "type": "function",
        "function": {
            "name": "get_prefetched_context",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_sessions",
            "description": (
                "LIVE DB search over session SUMMARIES (paragraph overviews of work stretches). "
                "ONLY for: yesterday/this week/today overviews, 'what did I work on', topic recaps. "
                "NOT for: exact URLs, clipboard/paste text, OCR, file paths, or single messages "
                "(use search_events). "
                "NOT for: finding files on disk (use find_files). "
                "If get_prefetched_context is available and its synopsis matches the question, "
                "call that FIRST; use search_sessions only when the bundle is missing, empty, "
                "or insufficient. "
                "Result header 'X of Y total' means partial — refine or call search_events."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Natural language question with any time window stated clearly.",
                    }
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_events",
            "description": (
                "LIVE DB search over individual EVENTS (URLs, clipboard/paste, OCR, window titles, "
                "app switches, fine timestamps). "
                "ONLY for: specific artifacts and granular activity detail. "
                "NOT for: day/week summaries (use search_sessions or get_prefetched_context). "
                "NOT for: locating a file on disk by name (use find_files). "
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Natural language question naming the artifact or detail.",
                    }
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": (
                "Search the REAL filesystem under trusted workspace folders for a file/folder name. "
                "USE THIS when the user asks to find a file by name (e.g. screenshot_processor.py) "
                "and you do not already have an absolute path. Returns absolute paths for the host "
                "agent to open if needed — Clippy does not open files/URLs itself. "
                "NOT a substitute for search_events (activity history). "
                "If no trusted folders exist, call remember_workspace_root with a path the user gave "
                "(e.g. C:\\Users\\proto or a project folder), then find_files again. "
                "Optional under= restricts to a root label or path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Filename or substring to find (e.g. screenshot_processor.py).",
                    },
                    "under": {
                        "type": "string",
                        "description": (
                            "Optional trusted root label or folder path to search within. "
                            "Omit to search all trusted folders."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max matches to return (default 15).",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_workspace_root",
            "description": (
                "Trust a folder for future find_files searches. "
                "Call when the user gives a project/root path (e.g. C:\\Users\\proto\\Clippy_Vision) "
                "or says to remember where their projects live. "
                "NOT for opening files (return paths via find_files; host opens them)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute folder path to trust.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional short name (defaults to folder name).",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workspace_roots",
            "description": (
                "List trusted folders Clippy may search with find_files. "
                "Use when the user asks which folders are trusted, or before searching "
                "if you are unsure whether a root is already saved."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_identity",
            "description": (
                "Save a personal fact about the user (name, location, job, skills, etc.). "
                "ONLY when they ask you to remember something about themselves. "
                "NOT for activity recall. NOT for free-form reminders (use save_note). "
                "NOT for folder paths (use remember_workspace_root)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "Identity field name."},
                    "op": {
                        "type": "string",
                        "description": "set | add_items | override | remove_items",
                    },
                    "value": {"type": "string", "description": "Scalar value for set/override."},
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List items for add_items/remove_items.",
                    },
                },
                "required": ["field", "op"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": (
                "Save a free-form note or reminder the user wants remembered. "
                "ONLY when they ask to remember a note. NOT for identity fields (use save_identity). "
                "NOT for workspace folders (use remember_workspace_root)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "The note text to remember."},
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_note",
            "description": (
                "Delete a note or memory fact the user wants forgotten. "
                "Matches by substring."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note_text": {
                        "type": "string",
                        "description": "The text or key phrase of the note to delete.",
                    }
                },
                "required": ["note_text"],
            },
        },
    },
]
