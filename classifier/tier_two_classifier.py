import json, urllib.request

from core.llm_gateway import gateway, Priority

MODEL      = "qwen3:8b"

# Identical every call — Ollama reuses KV cache for this prefix (5x speedup)
SYSTEM_PROMPT = """You label events from a personal computer activity monitor.

Verdicts:
- "interesting"      : notable, novel, worth remembering (deep work, new tool, anomaly)
- "not_interesting"  : routine noise (idle, common app switch, trivial burst)
- "needs_vision"     : text metadata is genuinely ambiguous AND a screenshot would resolve it.
                       Use sparingly — it is expensive. Prefer this when the event is not clear enough.

Score: 0-10 reflecting confidence the event IS interesting.
  0-3  = clearly not interesting
  4-6  = ambiguous
  7-10 = clearly interesting

reason: one short sentence."""

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["interesting", "not_interesting", "needs_vision"]
        },
        "score":  {"type": "integer", "minimum": 0, "maximum": 10},
        "reason": {"type": "string"}
    },
    "required": ["verdict", "score", "reason"]
}

def classify_with_llm(summary: str, event_type: str, window_context) -> dict:
    if isinstance(window_context, dict):
        ctx_str = f"{window_context.get('process_name', '')} - {window_context.get('current_window_title', '')}"
    else:
        ctx_str = str(window_context)

    body = gateway.chat(
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": f"[{event_type}] {ctx_str}: {summary}"},
    ],
    model=MODEL, format=VERDICT_SCHEMA, think=False,
    options={"temperature": 0}, priority=Priority.FOREGROUND)

    content = body["message"]["content"]
    verdict = json.loads(content) if isinstance(content, str) else content
    return {
        "verdict": verdict.get("verdict") or "needs_vision",
        "reason":  verdict.get("reason")  or "No reason provided",
        "score":   verdict.get("score")   or 0,
    }