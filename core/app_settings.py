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
    "image_embeddings_enabled": True,
    "min_gap_seconds": 8.0,
    "background_interval_seconds": 60.0,
    "activity_debounce_seconds": 2.0,
    "raw_retention_days": 7,
    "screenshot_retention_days": 1,
    "launch_at_login": False,
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
    return {
        "capture_screenshots": _as_bool(source.get("capture_screenshots"), True),
        "capture_all_monitors": _as_bool(source.get("capture_all_monitors"), False),
        "capture_clipboard": _as_bool(source.get("capture_clipboard"), True),
        "ocr_enabled": _as_bool(source.get("ocr_enabled"), True),
        "image_embeddings_enabled": _as_bool(source.get("image_embeddings_enabled"), True),
        "min_gap_seconds": _as_float(source.get("min_gap_seconds"), 8.0, 2.0, 120.0),
        "background_interval_seconds": _as_float(source.get("background_interval_seconds"), 60.0, 15.0, 3600.0),
        "activity_debounce_seconds": _as_float(source.get("activity_debounce_seconds"), 2.0, 0.5, 15.0),
        "raw_retention_days": _as_int(source.get("raw_retention_days"), 7, 1, 90),
        "screenshot_retention_days": _as_int(source.get("screenshot_retention_days"), 1, 1, 30),
        "launch_at_login": _as_bool(source.get("launch_at_login"), False),
    }


def get_capture_settings() -> dict[str, Any]:
    row = conn.execute("SELECT value FROM memory_meta WHERE key = ?", (_META_KEY,)).fetchone()
    if not row:
        return dict(_DEFAULTS)
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
    conn.commit()
    return normalized
