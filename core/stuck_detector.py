from __future__ import annotations

import json
import threading
import time

from core.capture_state import get_capture_status
from core.performance_metrics import load_backoff_multiplier
from core.storage import conn

POLL_SECS = 60
WINDOW_SECS = 30 * 60

STUCK_EVENT_TYPES = (
    "context_change",
    "clipboard_change",
    "paste",
    "typing_burst",
    "deviation",
    "screenshot_analysis",
)

def fetch_stuck_window(now: float | None = None) -> list[dict]:
    """Returns events in the last WINDOW_SECS for stuck scoring
    
    Why we are not using get_events_for_window():
    - This function does NOT filter out intersting events because all events are needed fot stuck scoring.
    """

    now = time.time() if now is None else now
    cutoff = now - WINDOW_SECS
    if cutoff <= 0:
        return []
    
    rows = conn.execute(
        """SELECT event_id, timestamp, event_type,
                  process_name, current_window_title, active_url,
                  previous_process_name, previous_window_title,
                  summary, payload, vision_ocr_text, vision_activity
           FROM events
           WHERE timestamp >= ?
             AND timestamp <= ?
             AND event_type IN ({placeholders})
           ORDER BY timestamp ASC""".format(
            placeholders=",".join("?" * len(STUCK_EVENT_TYPES))
        ),
        (cutoff, now, *STUCK_EVENT_TYPES)
    ).fetchall()

    events = []
    for r in rows:
        payload = r[9]
        if isinstance(payload, str) and payload:
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        elif not isinstance(payload, dict):
            payload = {}
        events.append(
            {
                "event_id": r[0],
                "timestamp": r[1],
                "event_type": r[2],
                "process_name": r[3] or "",
                "current_window_title": r[4] or "",
                "active_url": r[5] or "",
                "previous_process_name": r[6] or "",
                "previous_window_title": r[7] or "",
                "summary": r[8] or "",
                "payload": payload,
                "vision_ocr_text": r[10] or "",
                "vision_activity": r[11] or "",
            }
        )
    return events


def stuck_detector_tick() -> None:
    if not get_capture_status().get("active"):
        print("[stuck_detector] capture inactive — skip")
        return

    events = fetch_stuck_window()
    by_type: dict[str, int] = {}
    for e in events:
        by_type[e["event_type"]] = by_type.get(e["event_type"], 0) + 1

    type_str = ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())) or "none"
    print(f"[stuck_detector] window={WINDOW_SECS}s events={len(events)} ({type_str})")


def stuck_detector_loop() -> None:
    print("[stuck_detector] Phase 0 silent loop started")
    while True:
        try:
            stuck_detector_tick()
        except Exception as exc:
            print(f"[stuck_detector] tick failed: {exc}")
        time.sleep(POLL_SECS * load_backoff_multiplier())


def start_stuck_detector() -> threading.Thread:
    t = threading.Thread(
        target=stuck_detector_loop,
        daemon=True,
        name="stuck-detector",
    )
    t.start()
    return t