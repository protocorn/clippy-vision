"""App-lifetime background jobs that must not depend on live screen capture.

Capture may be paused as a privacy switch, but events and screenshots already
stored from an allowed window should still be summarized and distilled.
These workers live in the API process, which stays up while the app is open.
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_started = False


def start_background_jobs() -> None:
    """Idempotent: safe if Electron/API reloads or capture also used to start these."""
    global _started
    with _lock:
        if _started:
            return
        _started = True

    from core.distil import distil, should_distil
    from core.screenshot_processor import start_screenshot_processor
    from core.summarizer import start_summarizer
    from classifier.worker import start_catch_up_worker
    from core.stuck_detector import start_stuck_detector

    # Distil before worker threads touch the shared sqlite connection.
    try:
        if should_distil():
            print("[background] Distillation threshold reached — running distil...")
            distil()
    except Exception as exc:
        print(f"[background] Distil check skipped: {exc}")

    start_screenshot_processor()
    start_summarizer()
    start_catch_up_worker()
    start_stuck_detector()
    _start_insight_card_worker()
    print("[background] Summarizer, screenshot, stuck detector, insight cards, and classification catch-up workers started")


def _start_insight_card_worker() -> None:
    """Best-effort Day Cards for recent capture days (home surface)."""

    def _loop():
        from agent.insight_cards import ensure_recent_day_cards

        # First pass shortly after API is up (summarizer may still be catching up).
        time.sleep(45)
        while True:
            try:
                ensure_recent_day_cards(lookback=3)
            except Exception as exc:
                print(f"[background] insight cards: {exc}")
            time.sleep(30 * 60)

    threading.Thread(target=_loop, name="insight-cards", daemon=True).start()