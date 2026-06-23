from agent.retrieval import search_sessions, search_events
from agent.memory import recall_memory, fetch_cluster, save_identity, save_note


TOOLS = {
    "search_sessions": search_sessions,
    "search_events":   search_events,
    "recall_memory":   recall_memory,
    "fetch_cluster":   fetch_cluster,
    "save_identity":   save_identity,
    "save_note":       save_note,
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
                "If the result says the info isn't there, call search_events next."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Natural language question."}
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
                "app usage, WhatsApp/email content, fine-grained timestamps, copy-paste history. "
                "Returns raw event rows with screen/OCR data. "
                "If the result says the info isn't there, call search_sessions next."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Natural language question."}
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "List all long-term memory clusters with labels and descriptions. Use when the user asks what you know about them, or before fetching a specific cluster.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_cluster",
            "description": "Get all facts stored in a named memory cluster. Use after recall_memory to get the full content of a specific topic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "The cluster label, e.g. 'clippy_vision', 'employment'"}
                },
                "required": ["label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_identity",
            "description": "Save a personal fact about the user: name, location, job, education, preferences, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "Field name, e.g. 'name', 'location', 'job'"},
                    "value": {"type": "string", "description": "The value to store."}
                },
                "required": ["field", "value"],
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
                    "note": {"type": "string", "description": "The note or reminder text to store."}
                },
                "required": ["note"],
            },
        },
    },
]
