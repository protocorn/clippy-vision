import json
import time
import uuid
import threading
import urllib.request

from events import get_session_id
from storage import (
    store_summary, get_last_summary_time, get_unsummarized_events,
    get_events_for_window, get_sessions_needing_refresh, mark_session_vision_enriched,
)

from distil import should_distil, distil
from core.llm_gateway import gateway, Priority

MODEL        = "qwen3:8b"
INTERVAL_SEC = 300   # run every 5 minutes
MIN_EVENTS   = 3     # don't summarize if fewer than 3 interesting events
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "active_task": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "summary",
        "active_task",
        "entities"
    ],
}
SYSTEM_PROMPT = """You summarize computer work sessions from activity events.

Given a list of recent interesting events (typing bursts, pastes, context switches, vision observations),
produce a JSON object with:
- summary: 2-4 sentence plain English description of what the user was doing
- active_task: the single most likely task (e.g. "debugging code", "writing email", "reading docs")
- entities: list of specific things mentioned (file names, URLs, error messages, tool names, people)

Be specific and concrete. Use past tense. Focus on what actually happened, not generic descriptions.
Respond ONLY with valid JSON, no other text."""


def _build_prompt(events: list[dict]) -> str:
    lines = []
    for e in events:
        ts = time.strftime("%H:%M", time.localtime(e["timestamp"]))
        line = f"{e['summary']}"
        if e.get("vision_activity"):
            line += f" | vision: {e['vision_activity']}"
        lines.append(line)
    return "Events:\n" + "\n".join(lines)


def summarize_window(events: list[dict], session_id: str) -> dict | None:
    if len(events) < MIN_EVENTS:
        return None

    prompt = _build_prompt(events)

    body = gateway.chat(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt}
        ],
        model=MODEL, format=SUMMARY_SCHEMA, think=False,
        options={"temperature": 0}, priority=Priority.BACKGROUND)

    content = body["message"]["content"]
    result = json.loads(content) if isinstance(content, str) else content

    now = time.time()
    summary = {
        "summary_id":   str(uuid.uuid4()),
        "session_id":   session_id,
        "created_at":   now,
        "window_start": events[0]["timestamp"],
        "window_end":   events[-1]["timestamp"],
        "summary":      result.get("summary", ""),
        "active_task":  result.get("active_task"),
        "entities":     result.get("entities", []),
        "event_count":  len(events),
    }
    return summary

def _refresh_vision_enriched_sessions(session_id: str):
    stale = get_sessions_needing_refresh()
    if not stale:
        return
    for s in stale:
        events = get_events_for_window(s["window_start"], s["window_end"])
        if len(events) < MIN_EVENTS:
            mark_session_vision_enriched(s["summary_id"])
            continue
        print(f"  [SUMMARIZER] Re-summarizing session {s['summary_id'][:8]}... with vision data ({len(events)} events)")
        summary = summarize_window(events, session_id)
        if summary:
            summary["summary_id"] = s["summary_id"]  # overwrite in-place via INSERT OR REPLACE
            if should_distil():
                distil()
            store_summary(summary, vision_enriched=True)
            print(f"  [SUMMARIZER] Refreshed — {summary['active_task']}")
        else:
            mark_session_vision_enriched(s["summary_id"])


def summarizer_loop():
    print("[summarizer] Summarizer started")
    session_id = get_session_id()

    while True:
        tick_start = time.time()
        try:
            # Pass 1: keep summarizing until fully caught up
            # (multiple windows may be pending after a restart or long gap)
            while True:
                since  = get_last_summary_time(session_id)
                events = get_unsummarized_events(since)

                if len(events) < MIN_EVENTS:
                    if len(events) > 0:
                        print(f"  [SUMMARIZER] {len(events)} event(s) pending, need {MIN_EVENTS} to summarize — waiting for more")
                    break

                print(f"  [SUMMARIZER] Summarizing {len(events)} events since {time.strftime('%H:%M', time.localtime(since))}")
                summary = summarize_window(events, session_id)
                if summary:
                    store_summary(summary, vision_enriched=False)
                    print(f"  [SUMMARIZER] Done — {summary['active_task']}")
                    print(f"               {summary['summary'][:120]}...")
                else:
                    break  # LLM returned nothing, don't spin

            # Pass 2: re-summarize all past sessions now that vision has enriched them
            _refresh_vision_enriched_sessions(session_id)

        except Exception as e:
            print(f"  [SUMMARIZER] Error: {e}")

        # Sleep only for the time remaining in the interval so the tick cadence
        # stays fixed regardless of how long the work took.
        elapsed  = time.time() - tick_start
        sleep_for = max(0.0, INTERVAL_SEC - elapsed)
        if elapsed > 1:
            print(f"  [SUMMARIZER] Work took {elapsed:.0f}s, sleeping {sleep_for:.0f}s until next tick")
        time.sleep(sleep_for)

def start_summarizer() -> threading.Thread:
    t = threading.Thread(target=summarizer_loop, daemon=True)
    t.start()
    return t