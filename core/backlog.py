"""Classification backlog status and automatic catch-up gates.

When capture is off, the API catch-up worker drains stranded ``pending``
(Tier-0/1) then ``deferred`` (Tier-2). When capture is on, live owns pending;
catch-up only runs Tier-2 if the deferred queue is large/old enough.
"""

from __future__ import annotations

import threading
import time

from core.capture_state import get_capture_status
from core.storage import conn

# Auto-drain while capturing once the deferred queue is large or old.
RECOMMEND_COUNT = 50
RECOMMEND_AGE_SECS = 15 * 60
CATCH_UP_COOLDOWN_SECS = 45
# LLM timeouts need a long pause — short cooldowns just re-hammer the same event.
CATCH_UP_TIMEOUT_COOLDOWN_SECS = 5 * 60
DEFERRED_EVENT_SKIP_SECS = 10 * 60

_lock = threading.Lock()
_running = False
_cooldown_until = 0.0
_last_error: str | None = None
_deferred_skip_until: dict[str, float] = {}


def catch_up_allowed() -> bool:
    """True when deferred Tier-2 may run in the background."""
    now = time.time()
    with _lock:
        cooling = now < _cooldown_until
    if cooling:
        return False
    if get_capture_status().get("active"):
        return bool(_counts()["recommend"])
    return True


def set_catch_up_running(active: bool) -> None:
    global _running
    with _lock:
        _running = bool(active)


def note_catch_up_failure(message: str, cooldown_secs: float | None = None) -> None:
    """Record a soft failure and pause catch-up. Timeouts get a longer cooldown."""
    global _cooldown_until, _last_error
    text = str(message)
    if cooldown_secs is None:
        low = text.casefold()
        if "timed out" in low or "timeout" in low:
            cooldown_secs = CATCH_UP_TIMEOUT_COOLDOWN_SECS
        else:
            cooldown_secs = CATCH_UP_COOLDOWN_SECS
    with _lock:
        _cooldown_until = time.time() + max(5.0, float(cooldown_secs))
        _last_error = text[:240]


def note_deferred_event_skip(event_id: str, skip_secs: float = DEFERRED_EVENT_SKIP_SECS) -> None:
    """Temporarily skip one deferred event so timeouts rotate instead of sticking."""
    with _lock:
        _deferred_skip_until[str(event_id)] = time.time() + max(30.0, float(skip_secs))


def deferred_event_skipped(event_id: str) -> bool:
    now = time.time()
    eid = str(event_id)
    with _lock:
        until = _deferred_skip_until.get(eid)
        if until is None:
            return False
        if now >= until:
            _deferred_skip_until.pop(eid, None)
            return False
        return True


def _counts() -> dict:
    row = conn.execute(
        """SELECT
               SUM(CASE WHEN classification_status = 'deferred' THEN 1 ELSE 0 END),
               SUM(CASE WHEN classification_status = 'pending' THEN 1 ELSE 0 END),
               MIN(CASE WHEN classification_status = 'deferred' THEN timestamp END)
           FROM events"""
    ).fetchone()
    deferred = int(row[0] or 0)
    pending = int(row[1] or 0)
    oldest = float(row[2]) if row[2] is not None else None
    oldest_age = (time.time() - oldest) if oldest is not None else 0.0
    recommend = deferred >= RECOMMEND_COUNT or (
        deferred > 0 and oldest_age >= RECOMMEND_AGE_SECS
    )
    return {
        "deferred": deferred,
        "pending": pending,
        "oldest_deferred_ts": oldest,
        "oldest_deferred_age_secs": round(oldest_age) if oldest is not None else None,
        "recommend": recommend,
    }


def get_backlog_status() -> dict:
    """Diagnostics for /health — no UI action required."""
    counts = _counts()
    now = time.time()
    with _lock:
        running = _running
        error = _last_error
        cooling = max(0, int(_cooldown_until - now))
    capture_active = bool(get_capture_status().get("active"))
    return {
        **counts,
        "capture_active": capture_active,
        "catch_up_running": running,
        "cooldown_remaining_secs": cooling,
        "last_error": error,
    }
