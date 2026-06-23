import base64
import json
import urllib.request

from core.llm_gateway import gateway, Priority

MODEL = "qwen3-vl:4b"

SYSTEM_PROMPT = """You analyze screenshots from a personal computer activity monitor.

Given one or more screenshots near a flagged event, perform:
1. OCR — extract the most relevant visible text (errors, URLs, titles, code snippets).
2. Reasoning — infer what the user is doing on screen.
3. Next action — suggest one concrete next step if obvious, otherwise null.
4. Verdict — classify the event as interesting or not_interesting.

interesting: notable, novel, worth remembering (deep work, debugging, learning, anomaly).
not_interesting: routine noise (idle desktop, generic browsing, trivial UI).

Score: 0-10 confidence the event IS interesting.
reason: one short sentence summarizing the verdict."""

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["interesting", "not_interesting"],
        },
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "reason": {"type": "string"},
        "ocr_text": {"type": "string"},
        "user_activity": {"type": "string"},
        "suggested_action": {"type": ["string", "null"]},
    },
    "required": [
        "verdict",
        "score",
        "reason",
        "ocr_text",
        "user_activity",
        "suggested_action",
    ],
}


def _encode_image(path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def classify_with_vision(event: dict, screenshot_paths: list) -> dict:
    ctx = event["window_context"]
    ctx_str = (
        f"{ctx.get('process_name', '')} — {ctx.get('current_window_title', '')}"
    )
    if ctx.get("active_url"):
        ctx_str += f" ({ctx['active_url']})"

    user_content = (
        f"Event type: {event['event_type']}\n"
        f"Window context: {ctx_str}\n"
        f"Event summary: {event['summary']}\n"
        f"Number of screenshots: {len(screenshot_paths)} "
        "(ordered closest to event time first)"
    )

    images = [_encode_image(screenshot_paths[0])]


    body = gateway.chat(
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content, "images": images},
    ],
    model=MODEL, format=VERDICT_SCHEMA, think=False,
    options={"temperature": 0}, priority=Priority.FOREGROUND)

    content = body["message"]["content"]
    verdict = json.loads(content) if isinstance(content, str) else content
    return {
        "verdict": verdict.get("verdict") or "not_interesting",
        "score": verdict.get("score") or 0,
        "reason": verdict.get("reason") or "No reason provided",
        "ocr_text": verdict.get("ocr_text") or "",
        "user_activity": verdict.get("user_activity") or "",
        "suggested_action": verdict.get("suggested_action"),
    }