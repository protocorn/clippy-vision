import hashlib
import json
import threading
import time
import uuid

try:
    from core.distil import distil, should_distil
    from core.events import get_session_id
    from core.storage import (
        get_events_for_window,
        get_sessions_needing_refresh,
        get_unsummarized_events,
        mark_session_vision_enriched,
        store_summary,
    )
except ImportError:
    from distil import distil, should_distil
    from events import get_session_id
    from storage import (
        get_events_for_window,
        get_sessions_needing_refresh,
        get_unsummarized_events,
        mark_session_vision_enriched,
        store_summary,
    )
from core.accessibility_text import is_useful_accessibility_text, strip_ui_chrome
from core.llm_gateway import Priority, gateway
from core.local_embeddings import embed_text
from core.model_residency import can_load_text, ensure_text_model

MODEL = "qwen3:8b"
INTERVAL_SEC = 60
MIN_EVENTS = 3  # don't summarize if fewer than 3 interesting events
RAW_LOOKBACK_SECONDS = 7 * 24 * 60 * 60
MAX_SESSION_GROUPS_PER_TICK = 3
MAX_VISION_REFRESHES_PER_TICK = 1
MAX_EVENTS_PER_WINDOW = 25
MAX_PROMPT_CHARS = 7000
PER_EVENT_SCREEN_CHARS = 500
# Vision-refresh hot-loop guard: timeouts must not re-queue the same session every tick.
REFRESH_FAIL_MARK_AFTER = 3
REFRESH_BACKOFF_BASE_SECS = 120
REFRESH_BACKOFF_MAX_SECS = 30 * 60
_refresh_failures: dict[str, dict] = {}  # summary_id -> {count, retry_after}
_WINDOW_TITLE_SUFFIXES = (
    " - cursor",
    " - google chrome",
    " - microsoft edge",
    " - firefox",
    " - clippy vision",
    " - visual studio code",
    " - code",
)
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


def _is_window_title_line(line: str) -> bool:
    low = line.casefold()
    if any(low.endswith(suffix) for suffix in _WINDOW_TITLE_SUFFIXES):
        return True
    # Screenshot-tab titles like "1786722150920_processed.jpg - Clippy_Vision - Cursor"
    if "_processed.jpg" in low or "_processed.png" in low:
        return True
    return False


def _strip_ui_chrome(text: str, *, drop_window_titles: bool = False) -> str:
    """Summarizer prompt filter: shared a11y chrome + optional window-title lines."""
    filtered = strip_ui_chrome(text)
    if not drop_window_titles:
        return filtered
    lines = []
    for raw in filtered.splitlines():
        line = " ".join(raw.split()).strip()
        if not line:
            continue
        if _is_window_title_line(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def is_useful_screen_text(text: str) -> bool:
    """True when screen text has real content, not just window-manager chrome."""
    filtered = _strip_ui_chrome(text, drop_window_titles=True)
    return is_useful_accessibility_text(filtered)


def _screen_text_fingerprint(text: str) -> str:
    normalized = " ".join(_strip_ui_chrome(text).split()).casefold()
    return hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()


def _select_events_for_prompt(events: list[dict]) -> list[dict]:
    """Keep an oldest-first slice so backlog drains instead of chasing the newest tip."""
    if len(events) <= MAX_EVENTS_PER_WINDOW:
        return list(events)
    return list(events[:MAX_EVENTS_PER_WINDOW])


def _build_prompt(events: list[dict]) -> str:
    selected = _select_events_for_prompt(events)
    lines = []
    seen_screen = set()
    budget = MAX_PROMPT_CHARS - len("Events:\n")

    for event in selected:
        line = f"{event.get('summary') or ''}"
        activity = event.get("vision_activity")
        if activity:
            line += f" | vision: {activity}"

        raw_screen = event.get("vision_ocr_text") or ""
        if is_useful_screen_text(raw_screen):
            fingerprint = _screen_text_fingerprint(raw_screen)
            if fingerprint not in seen_screen:
                seen_screen.add(fingerprint)
                captured = " ".join(_strip_ui_chrome(raw_screen).split())[:PER_EVENT_SCREEN_CHARS]
                if captured:
                    line += f" | screen text: {captured}"

        # Always keep the event summary line if it fits; drop screen text first when over budget.
        if len(line) + 1 > budget and " | screen text: " in line:
            line = line.split(" | screen text: ", 1)[0]
        if len(line) + 1 > budget:
            break
        lines.append(line)
        budget -= len(line) + 1

    return "Events:\n" + "\n".join(lines)


def _window_has_useful_screen_text(events: list[dict]) -> bool:
    return any(is_useful_screen_text(event.get("vision_ocr_text") or "") for event in events)


def _refresh_in_backoff(summary_id: str) -> bool:
    state = _refresh_failures.get(summary_id)
    if not state:
        return False
    return time.time() < float(state.get("retry_after", 0))


def _note_refresh_failure(summary_id: str, err: BaseException) -> bool:
    """Record a failed vision refresh. Returns True if caller should give up (mark enriched)."""
    prev = _refresh_failures.get(summary_id) or {"count": 0, "retry_after": 0.0}
    count = int(prev["count"]) + 1
    delay = min(REFRESH_BACKOFF_MAX_SECS, REFRESH_BACKOFF_BASE_SECS * (2 ** (count - 1)))
    _refresh_failures[summary_id] = {
        "count": count,
        "retry_after": time.time() + delay,
    }
    print(
        f"  [SUMMARIZER] Refresh failed for {summary_id[:8]} "
        f"(attempt {count}/{REFRESH_FAIL_MARK_AFTER}): {err} — "
        f"backoff {delay:.0f}s"
    )
    return count >= REFRESH_FAIL_MARK_AFTER


def _clear_refresh_failure(summary_id: str) -> None:
    _refresh_failures.pop(summary_id, None)


def summarize_window(events: list[dict], session_id: str) -> dict | None:
    if len(events) < MIN_EVENTS:
        return None

    prompt = _build_prompt(events)

    body = gateway.chat(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        model=MODEL, format=SUMMARY_SCHEMA, think=False,
        options={"temperature": 0}, priority=Priority.BACKGROUND,
        timeout=120,
    )

    content = body["message"]["content"]
    result = json.loads(content) if isinstance(content, str) else content

    now = time.time()
    summary_text = result.get("summary", "")

    # Embed the summary for semantic search at query time
    embedding = None
    if summary_text:
        try:
            embedding = embed_text(summary_text)
        except Exception:
            pass  # best-effort; retrieval.py will back-fill on first query

    selected = _select_events_for_prompt(events)
    summary = {
        "summary_id": str(uuid.uuid4()),
        "session_id": session_id,
        "created_at": now,
        "window_start": selected[0]["timestamp"],
        "window_end": selected[-1]["timestamp"],
        "summary": summary_text,
        "active_task": result.get("active_task"),
        "entities": result.get("entities", []),
        "event_count": len(selected),
        "embedding": embedding,
    }
    return summary


def _refresh_vision_enriched_sessions(session_id: str):
    stale = get_sessions_needing_refresh()
    if not stale:
        return

    refreshed = 0
    for s in stale:
        summary_id = s["summary_id"]
        if _refresh_in_backoff(summary_id):
            continue

        events = get_events_for_window(s["window_start"], s["window_end"])
        if len(events) < MIN_EVENTS:
            mark_session_vision_enriched(summary_id)
            _clear_refresh_failure(summary_id)
            continue

        # Chrome-only / empty a11y must not keep sessions in the refresh queue forever.
        if not _window_has_useful_screen_text(events):
            mark_session_vision_enriched(summary_id)
            _clear_refresh_failure(summary_id)
            print(
                f"  [SUMMARIZER] Skipping refresh {summary_id[:8]} — "
                "no useful screen text (UI chrome only)"
            )
            continue

        if refreshed >= MAX_VISION_REFRESHES_PER_TICK:
            break

        selected = _select_events_for_prompt(events)
        print(
            f"  [SUMMARIZER] Re-summarizing session {summary_id[:8]}... "
            f"with vision data ({len(selected)}/{len(events)} events)"
        )
        try:
            summary = summarize_window(events, session_id)
        except Exception as e:
            if _note_refresh_failure(summary_id, e):
                mark_session_vision_enriched(summary_id)
                _clear_refresh_failure(summary_id)
                print(
                    f"  [SUMMARIZER] Giving up vision refresh for {summary_id[:8]} "
                    f"after {REFRESH_FAIL_MARK_AFTER} failures — keeping prior summary"
                )
            break  # one LLM failure per tick; leave gateway for chat / catch-up

        if summary:
            summary["summary_id"] = summary_id  # overwrite in-place via INSERT OR REPLACE
            if should_distil():
                distil()
            store_summary(summary, vision_enriched=True, embedding=summary.pop("embedding", None))
            _clear_refresh_failure(summary_id)
            print(f"  [SUMMARIZER] Refreshed — {summary['active_task']}")
            refreshed += 1
        else:
            mark_session_vision_enriched(summary_id)
            _clear_refresh_failure(summary_id)


def summarizer_loop():
    print("[summarizer] Summarizer started")
    session_id = get_session_id()

    while True:
        tick_start = time.time()
        failed = False
        try:
            if not can_load_text():
                # Don't soft-skip forever: try loading the model. A hard free-RAM
                # floor was stranding summarizer while chat (INTERACTIVE) could load.
                if ensure_text_model():
                    print("  [SUMMARIZER] text model warmed — continuing")
                else:
                    print("  [SUMMARIZER] deferring — text model unavailable (warm failed or commit pressure)")
                    time.sleep(INTERVAL_SEC)
                    continue
            events = get_unsummarized_events(time.time() - RAW_LOOKBACK_SECONDS)
            grouped: dict[str, list[dict]] = {}
            for event in events:
                grouped.setdefault(event["session_id"], []).append(event)

            ready = [items for items in grouped.values() if len(items) >= MIN_EVENTS]
            ready.sort(key=lambda items: items[0]["timestamp"])

            # Pass 1: work through pending session groups in bounded batches so
            # restarts or long gaps catch up without monopolizing a single tick.
            for session_events in ready[:MAX_SESSION_GROUPS_PER_TICK]:
                source_session_id = session_events[0]["session_id"]
                print(
                    f"  [SUMMARIZER] Summarizing {min(len(session_events), MAX_EVENTS_PER_WINDOW)}"
                    f"/{len(session_events)} events from session {source_session_id[:8]}"
                )
                summary = summarize_window(session_events, source_session_id)
                if summary:
                    store_summary(summary, vision_enriched=False, embedding=summary.pop("embedding", None))
                    print(f"  [SUMMARIZER] Done — {summary['active_task']}")
                    print(f"               {summary['summary'][:120]}...")

            pending = sum(len(items) for items in grouped.values() if len(items) < MIN_EVENTS)
            if pending:
                print(
                    f"  [SUMMARIZER] {pending} event(s) remain in short sessions "
                    f"below the {MIN_EVENTS}-event threshold"
                )

            # Pass 2: at most one vision refresh per tick so chat can interleave
            _refresh_vision_enriched_sessions(session_id)

        except Exception as e:
            print(f"  [SUMMARIZER] Error: {e}")
            failed = True

        # Sleep only for the time remaining in the interval so the tick cadence
        # stays fixed regardless of how long the work took. Failed ticks wait a
        # full interval so a dead Ollama is not retried with zero backoff.
        elapsed = time.time() - tick_start
        sleep_for = INTERVAL_SEC if failed else max(0.0, INTERVAL_SEC - elapsed)
        if elapsed > 1:
            print(f"  [SUMMARIZER] Work took {elapsed:.0f}s, sleeping {sleep_for:.0f}s until next tick")
        time.sleep(sleep_for)


def start_summarizer() -> threading.Thread:
    t = threading.Thread(target=summarizer_loop, daemon=True, name="summarizer")
    t.start()
    return t
