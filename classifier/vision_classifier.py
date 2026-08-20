import base64
import json
import re
import urllib.request

from core.llm_gateway import Priority, gateway

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


def _parse_json_safe(text: str) -> dict:
    """Parse JSON from model output, repairing common truncation issues.

    The vision model sometimes returns a JSON object that is cut off mid-string
    (e.g. unterminated string literal).  We try three strategies in order:
      1. Direct parse — succeeds most of the time.
      2. Truncate at the last complete key-value pair before a comma or closing
         brace, then close the object.
      3. Extract individual known fields via regex and rebuild a minimal dict.
    Raises ValueError only when all strategies fail.
    """

    # Strategy 1: clean parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass



    # Strategy 2: close truncated object
    # Find the last well-formed top-level comma or the opening brace, then close.
    repaired = text.rstrip()

    # Strip any trailing partial token (open string, trailing comma)
    repaired = re.sub(r",\s*$", "", repaired)
    repaired = re.sub(r',\s*"[^"]*$', "", repaired)
    if not repaired.endswith("}"):
        repaired += "}"
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass


    # Strategy 3: regex field extraction
    def _extract(key: str, default=None):
        m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', text)
        if m:
            return m.group(1)
        m = re.search(rf'"{key}"\s*:\s*(-?\d+)', text)
        if m:
            return int(m.group(1))
        return default

    verdict = _extract("verdict", "not_interesting")
    if verdict not in ("interesting", "not_interesting"):
        verdict = "not_interesting"
    result = {
        "verdict": verdict,
        "score": _extract("score", 0),
        "reason": _extract("reason", "parse error — partial response"),
        "ocr_text": _extract("ocr_text", ""),
        "user_activity": _extract("user_activity", ""),
        "suggested_action": _extract("suggested_action"),
    }

    # Accept if we got at least a verdict
    if result["verdict"] in ("interesting", "not_interesting"):
        return result

    raise ValueError(f"Could not parse vision model output: {text[:120]!r}")


def classify_with_vision(event: dict, screenshot_paths: list) -> dict:
    ctx = event["window_context"]
    ctx_str = f"{ctx.get('process_name', '')} — {ctx.get('current_window_title', '')}"
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
        model=MODEL,
        format=VERDICT_SCHEMA,
        options={"temperature": 0},
        priority=Priority.FOREGROUND,
    )

    content = body["message"]["content"]


    # qwen3-vl with think=False routes structured output into "thinking" instead of
    # "content" — fall back to thinking field if content is empty.
    if isinstance(content, str) and not content.strip():
        content = body["message"].get("thinking", "")
    if isinstance(content, str):
        content = content.strip()
        if not content:
            raise ValueError("Vision model returned empty content")
        verdict = _parse_json_safe(content)
    else:
        verdict = content

    return {
        "verdict": verdict.get("verdict") or "not_interesting",
        "score": verdict.get("score") or 0,
        "reason": verdict.get("reason") or "No reason provided",
        "ocr_text": verdict.get("ocr_text") or "",
        "user_activity": verdict.get("user_activity") or "",
        "suggested_action": verdict.get("suggested_action"),
    }
