from agent.memory import delete_note, save_identity, save_note
from agent.retrieval import search_events, search_sessions

TOOLS = {
    "search_sessions": search_sessions,
    "search_events": search_events,
    "save_identity": save_identity,
    "save_note": save_note,
    "delete_note": delete_note,
}



# Used when prefetch already supplied retrieval context — model only gets
# write/delete tools so it physically cannot trigger a redundant search.
WRITE_TOOLS = {
    "save_identity": save_identity,
    "save_note": save_note,
    "delete_note": delete_note,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_sessions",
            "description": (
                "Search session summaries in the activity database. "
                "Use for: broad time windows (yesterday, this week), daily/weekly overviews, "
                "what-did-I-work-on questions, project topics, task recaps. "
                "Returns paragraph summaries — NOT granular event detail. "
                "Use when <prefetch_context> is empty, insufficient, or the user asks for more detail. "
                "The result header shows 'X of Y total' — if Y > X, the result is partial; "
                "call search_events with a more specific query to find what you're looking for. "
                "ALWAYS follow up with search_events when the user asks about: URLs, links, "
                "websites, clipboard content, specific messages, or any fine-grained detail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Natural language question.",
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
                "Search individual events in the activity database. "
                "Use for: specific messages, OCR screen text, exact URLs, clipboard content, "
                "app usage, message content, fine-grained timestamps, copy-paste history, "
                "links the user visited, articles the user read, browser activity. "
                "Returns raw event rows with screen/OCR data. "
                "Use when <prefetch_context> is empty, insufficient, or the user needs granular detail. "
                "The result header shows 'X of Y total' — if Y > X, refine the query to be more specific. "
                "If the result says the info isn't there, call search_sessions next."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Natural language question.",
                    }
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_identity",
            "description": (
                "Save a personal fact about the user. "
                "Use op='set' for scalar facts (name, location, job). "
                "Use op='add_items' with items=[] for adding to a list (hobbies, skills). "
                "Use op='override' only when the user explicitly corrects a previous fact."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "description": "Field name, e.g. 'name', 'hobbies'",
                    },
                    "value": {
                        "type": "string",
                        "description": "Value for scalar fields (set/override). Leave empty for list ops.",
                    },
                    "op": {
                        "type": "string",
                        "enum": ["set", "add_items", "remove_items", "override"],
                        "description": "Operation type. Default is 'set'.",
                    },
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of items for add_items or remove_items ops.",
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
            "description": "Save a free-form note or reminder the user wants you to remember.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "The note or reminder text to store.",
                    }
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
                "Use when the user says 'forget', 'delete', 'remove', or 'don't remember that'. "
                "Matches by substring — pass the key phrase or exact text from the note."
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


# Schemas for write-only mode (used when prefetch context is present)
WRITE_TOOL_SCHEMAS = [s for s in TOOL_SCHEMAS if s["function"]["name"] in WRITE_TOOLS]
