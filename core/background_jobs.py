"""App-lifetime background jobs that must not depend on live screen capture.

Capture may be paused as a privacy switch, but events and screenshots already
stored from an allowed window should still be summarized and distilled.
These workers live in the API process, which stays up while the app is open.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_started = False


def start_background_jobs() -> None:
    """Idempotent: safe if Electron/API reloads or capture also used to start these."""
    global _started
    with _lock:
        if _started:
            return
        _started = True

    from classifier.worker import start_catch_up_worker
    from core.distil import distil, should_distil
    from core.screenshot_processor import start_screenshot_processor
    from core.summarizer import start_summarizer

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
    print("[background] Summarizer, screenshot, and classification catch-up workers started")
