import uuid
from typing import TypedDict, Optional

SESSION_ID = str(uuid.uuid4())


class WindowMetadata(TypedDict):
    timestamp: float
    current_window_title: str
    #is_browser_window: bool
    active_url: Optional[str]
    process_name: str

class Event(TypedDict):
    event_id: str
    session_id: str
    timestamp: float

    event_type: str # "typing_burst", "paste", "context_change, "deviation", "mouse_burst", ...

    window_context: WindowMetadata
    previous_window_context: Optional[WindowMetadata]

    payload: dict

    # Ingestion fields (filled after capture, and not at record time)
    summary: Optional[str]
    vector_embedding: Optional[list]
    interest_score: Optional[float]
    interest_reason: Optional[str]
    interesting: Optional[bool]

def generate_summary(event: Event) -> str:

    payload = event["payload"]
    event_type = event["event_type"]
    window_context = event["window_context"]
    previous_window_context = event["previous_window_context"]

    match event_type:
        case "typing_burst":
            return (f"Typed {payload['word_count']} words at {payload['typing_speed_wpm']} WPM "
                f"in {window_context['process_name']}, revision ratio {payload['revision_ratio']}")
        case "paste":
            return f"Pasted content: {payload['pasted_content']} in {window_context['process_name']} on {window_context['active_url'] or window_context['current_window_title']}"
        case "context_change":
            return (f"Switched to {window_context['process_name']} - {window_context['current_window_title']} from {previous_window_context['process_name']} - {previous_window_context['current_window_title']}")
        case "deviation":
            return (f"Anomalous typing in {payload['context_key']}: "
                f"overall deviation {payload['overall_deviation']}σ")
        case "clipboard_change":
            content = payload['content'].replace('\n', ' ')
            preview = content[:200] + ("..." if len(content) > 200 else "")
            return f"Copied '{preview}' in {window_context['process_name']} - {window_context['current_window_title']}"
        case "mouse_burst":
            return (f"Mouse burst detected in {window_context['process_name']}")
        case _:
            return f"Unknown event: {event_type} in {window_context['process_name']} - {window_context['current_window_title']}"

def get_session_id() -> str:
    return SESSION_ID