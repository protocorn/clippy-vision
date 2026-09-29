from __future__ import annotations

import json
import time
from typing import Any

from core.storage import conn

_META_KEY = "settings.capture"
_IMAGE_MODES = {"auto", "cached", "fallback", "off"}
_DEFAULTS: dict[str, Any] = {
    "capture_screenshots": True,
    "capture_all_monitors": False,
    "capture_clipboard": True,
    "ocr_enabled": True,
    # PARKED — contributor feature; default off. Ask the contributor whether to
    # keep CLIP image embeddings and why. Clippy's spine is text (a11y/OCR +
    # session search and memory); CLIP only helps "search by look" and costs RAM/Torch.
    "image_embeddings_enabled": False,
    # PARKED — contributor feature; default off. Ask the contributor whether to
    # keep event-level RAG and why. Session search and memory already handle most chat
    # recall; this only adds MiniLM vectors on raw events for fuzzy
    # search_events when the SQL path is not enough.
    "rag_enabled": False,
    "min_gap_seconds": 8.0,
    "background_interval_seconds": 60.0,
    "activity_debounce_seconds": 2.0,
    "raw_retention_days": 7,
    "screenshot_retention_days": 1,
    # Adaptive TTL cap: high-signal frames may live up to this many days
    # (never longer than raw_retention_days). See core/screenshot_ttl.py.
    "screenshot_retention_max_days": 7,
    "summary_retention_days": 90,
    # "all" records every app and uses the privacy blackout list.
    # "selected" records only the process names in watch_apps.
    "watch_mode": "all",
    "watch_apps": [],
    "launch_at_login": False,
    # Hard ceiling for a single UIA bounds/text query on the async worker.
    # UIA COM calls can hang against certain apps; this bounds the damage.
    "uia_timeout_seconds": 1.5,
}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_capture_settings(values: dict[str, Any] | None = None) -> dict[str, Any]:
    source = dict(_DEFAULTS)
    if values:
        source.update(values)
    base_days = _as_int(source.get("screenshot_retention_days"), 1, 1, 7)
    max_days = _as_int(source.get("screenshot_retention_max_days"), 7, 1, 14)
    if max_days < base_days:
        max_days = base_days
    mode = str(source.get("watch_mode") or "all").strip().lower()
    if mode not in {"all", "selected"}:
        mode = "all"
    apps: list[str] = []
    raw_apps = source.get("watch_apps") or []
    if isinstance(raw_apps, str):
        raw_apps = [part.strip() for part in raw_apps.split(",")]
    if isinstance(raw_apps, list):
        for item in raw_apps:
            token = str(item or "").strip()
            if token and token not in apps and len(token) <= 120:
                apps.append(token)
            if len(apps) >= 40:
                break
    return {
        "capture_screenshots": _as_bool(source.get("capture_screenshots"), True),
        "capture_all_monitors": _as_bool(source.get("capture_all_monitors"), False),
        "capture_clipboard": _as_bool(source.get("capture_clipboard"), True),
        "ocr_enabled": _as_bool(source.get("ocr_enabled"), True),
        "image_embeddings_enabled": _as_bool(source.get("image_embeddings_enabled"), False),
        "rag_enabled": _as_bool(source.get("rag_enabled"), False),
        "min_gap_seconds": _as_float(source.get("min_gap_seconds"), 8.0, 2.0, 120.0),
        "background_interval_seconds": _as_float(source.get("background_interval_seconds"), 60.0, 15.0, 3600.0),
        "activity_debounce_seconds": _as_float(source.get("activity_debounce_seconds"), 2.0, 0.5, 15.0),
        "raw_retention_days": _as_int(source.get("raw_retention_days"), 7, 1, 30),
        "screenshot_retention_days": base_days,
        "screenshot_retention_max_days": max_days,
        "summary_retention_days": _as_int(source.get("summary_retention_days"), 90, 1, 180),
        "watch_mode": mode,
        "watch_apps": apps,
        "launch_at_login": _as_bool(source.get("launch_at_login"), False),
        "uia_timeout_seconds": _as_float(source.get("uia_timeout_seconds"), 1.5, 0.5, 5.0),
    }


def get_capture_settings() -> dict[str, Any]:
    row = conn.execute("SELECT value FROM memory_meta WHERE key = ?", (_META_KEY,)).fetchone()
    if not row:
        return normalize_capture_settings(None)
    try:
        stored = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return dict(_DEFAULTS)
    return normalize_capture_settings(stored if isinstance(stored, dict) else None)


def set_capture_settings(values: dict[str, Any] | None) -> dict[str, Any]:
    current = get_capture_settings()
    current.update(values or {})
    normalized = normalize_capture_settings(current)
    conn.execute(
        "INSERT OR REPLACE INTO memory_meta (key, value) VALUES (?, ?)",
        (_META_KEY, json.dumps(normalized)),
    )
    retention_days = normalized["raw_retention_days"]
    conn.execute(
        "UPDATE events SET expires_at = timestamp + ?",
        (retention_days * 86400,),
    )
    conn.execute(
        "DELETE FROM events WHERE timestamp < ?",
        (time.time() - retention_days * 86400,),
    )
    summary_days = normalized["summary_retention_days"]
    conn.execute(
        "UPDATE sessions SET expires_at = created_at + ?",
        (summary_days * 86400,),
    )
    conn.execute(
        "DELETE FROM sessions WHERE created_at < ?",
        (time.time() - summary_days * 86400,),
    )
    conn.commit()
    return normalized


def should_watch_process(process_name: str) -> bool:
    """Selected-apps mode records only the processes the user picked.

    ``chrome.exe`` and ``Google Chrome`` are the same app.
    """
    from core.process_names import process_key

    settings = get_capture_settings()
    if settings.get("watch_mode") != "selected":
        return True
    key = process_key(process_name)
    if not key:
        return False
    for app in settings.get("watch_apps") or []:
        if process_key(str(app)) == key:
            return True
    return False
