"""Adaptive screenshot retention.

Default capture keeps images for ``screenshot_retention_days`` (usually 1).
Frames linked to high-signal events get a longer TTL up to
``screenshot_retention_max_days`` (capped by raw event retention).

keep_score ∈ [0, 1] from linked event signals only (no keyword lists):
  interesting flag, interest_score, active_url presence, clipboard/paste type.

ttl_days = base + (cap - base) * keep_score

OCR / vision text on events is the durable backup after the JPEG is gone.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from core.app_settings import get_capture_settings
from core.storage import conn


def retention_bounds(settings: dict[str, Any] | None = None) -> tuple[float, float]:
    """Return (base_days, cap_days). Cap never exceeds raw event retention."""
    cfg = settings or get_capture_settings()
    base = float(cfg.get("screenshot_retention_days", 1))
    raw_cap = float(cfg.get("raw_retention_days", 7))
    max_days = float(cfg.get("screenshot_retention_max_days", 7))
    cap = max(base, min(max_days, raw_cap))
    return base, cap


def keep_score_from_event(event: dict[str, Any] | None) -> float:
    """Score how long to keep a frame from classifier/event structure only."""
    if not event:
        return 0.0
    score = 0.0
    if event.get("interesting"):
        score += 0.40
    try:
        interest = float(event.get("interest_score") or 0.0)
    except (TypeError, ValueError):
        interest = 0.0
    # interest_score historically 0–10-ish; normalize softly
    score += 0.35 * max(0.0, min(1.0, interest / 10.0 if interest > 1.0 else interest))

    if (event.get("active_url") or "").strip():
        score += 0.15

    et = (event.get("event_type") or "").strip().lower()
    if et in {"paste", "clipboard_change"}:
        score += 0.10

    return max(0.0, min(1.0, score))


def ttl_days_for_score(keep: float, settings: dict[str, Any] | None = None) -> float:
    base, cap = retention_bounds(settings)
    keep = max(0.0, min(1.0, float(keep)))
    return base + (cap - base) * keep


def _event_for_screenshot(filename: str, ts_ms: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT event_type, process_name, current_window_title, active_url,
                  summary, interesting, interest_score, interest_reason,
                  vision_ocr_text, vision_activity, screenshot_filename, timestamp
           FROM events
           WHERE screenshot_filename = ?
           ORDER BY ABS(timestamp - ?) ASC
           LIMIT 1""",
        (filename, ts_ms / 1000.0),
    ).fetchone()
    if not row:
        # Nearest event within ±15s of the frame timestamp
        row = conn.execute(
            """SELECT event_type, process_name, current_window_title, active_url,
                      summary, interesting, interest_score, interest_reason,
                      vision_ocr_text, vision_activity, screenshot_filename, timestamp
               FROM events
               WHERE ABS(timestamp - ?) <= 15
               ORDER BY ABS(timestamp - ?) ASC
               LIMIT 1""",
            (ts_ms / 1000.0, ts_ms / 1000.0),
        ).fetchone()
    if not row:
        return None
    keys = (
        "event_type", "process_name", "current_window_title", "active_url",
        "summary", "interesting", "interest_score", "interest_reason",
        "vision_ocr_text", "vision_activity", "screenshot_filename", "timestamp",
    )
    return dict(zip(keys, row))


def screenshot_keep_until_ms(
    path: Path,
    *,
    settings: dict[str, Any] | None = None,
    now_ms: int | None = None,
) -> tuple[int, float, float]:
    """Return (keep_until_ms, keep_score, ttl_days) for a screenshot path."""
    try:
        ts_ms = int(path.stem.split("_", 1)[0])
    except (ValueError, IndexError) as exc:
        raise ValueError(f"unparseable screenshot name: {path.name}") from exc

    event = _event_for_screenshot(path.name, ts_ms)
    keep = keep_score_from_event(event)
    ttl = ttl_days_for_score(keep, settings)
    keep_until = ts_ms + int(ttl * 86400 * 1000)
    return keep_until, keep, ttl


def should_purge_screenshot(
    path: Path,
    *,
    settings: dict[str, Any] | None = None,
    now_ms: int | None = None,
) -> bool:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        keep_until, _, _ = screenshot_keep_until_ms(path, settings=settings, now_ms=now)
    except ValueError:
        # Non-epoch names: fall back to base retention from mtime-less stem fail → purge by base from 0
        base, _ = retention_bounds(settings)
        return True  # unknown naming → let caller use flat cutoff; treat as expired
    return now >= keep_until


def get_screenshot_payload(
    *,
    filename: str | None = None,
    timestamp: float | None = None,
    screenshots_dir: Path | None = None,
) -> dict[str, Any]:
    """Fetch image path when present; always attach OCR/event metadata as backup."""
    from core.paths import get_screenshots_dir

    directory = screenshots_dir or get_screenshots_dir()
    event: dict[str, Any] | None = None
    path: Path | None = None

    if filename:
        cand = directory / Path(filename).name
        if cand.is_file():
            path = cand
        row = conn.execute(
            """SELECT event_id, timestamp, event_type, process_name, current_window_title,
                      active_url, summary, vision_ocr_text, vision_activity,
                      screenshot_filename, interesting, interest_score
               FROM events WHERE screenshot_filename = ?
               ORDER BY timestamp DESC LIMIT 1""",
            (Path(filename).name,),
        ).fetchone()
        if row:
            event = {
                "event_id": row[0], "timestamp": row[1], "event_type": row[2],
                "process_name": row[3], "current_window_title": row[4],
                "active_url": row[5], "summary": row[6],
                "vision_ocr_text": row[7], "vision_activity": row[8],
                "screenshot_filename": row[9], "interesting": bool(row[10]),
                "interest_score": row[11],
            }
    elif timestamp is not None:
        ts = float(timestamp)
        # Prefer linked filename near the timestamp
        row = conn.execute(
            """SELECT event_id, timestamp, event_type, process_name, current_window_title,
                      active_url, summary, vision_ocr_text, vision_activity,
                      screenshot_filename, interesting, interest_score
               FROM events
               WHERE ABS(timestamp - ?) <= 30
               ORDER BY ABS(timestamp - ?) ASC
               LIMIT 1""",
            (ts, ts),
        ).fetchone()
        if row:
            event = {
                "event_id": row[0], "timestamp": row[1], "event_type": row[2],
                "process_name": row[3], "current_window_title": row[4],
                "active_url": row[5], "summary": row[6],
                "vision_ocr_text": row[7], "vision_activity": row[8],
                "screenshot_filename": row[9], "interesting": bool(row[10]),
                "interest_score": row[11],
            }
            if event.get("screenshot_filename"):
                cand = directory / event["screenshot_filename"]
                if cand.is_file():
                    path = cand
        if path is None:
            # Disk fallback by epoch ms filename
            target_ms = int(ts * 1000)
            best: tuple[int, Path] | None = None
            for p in directory.glob("*.jpg"):
                try:
                    ms = int(p.stem.split("_", 1)[0])
                except ValueError:
                    continue
                dist = abs(ms - target_ms)
                if dist <= 30_000 and (best is None or dist < best[0]):
                    best = (dist, p)
            if best:
                path = best[1]

    image_available = path is not None and path.is_file()
    ocr = (event or {}).get("vision_ocr_text") or ""
    return {
        "ok": bool(image_available or ocr or event),
        "image_available": image_available,
        "path": str(path) if image_available else None,
        "filename": path.name if image_available else (event or {}).get("screenshot_filename"),
        "ocr_text": ocr[:4000] if ocr else "",
        "event": event,
        "note": (
            None if image_available
            else "Image expired or missing — OCR/event text is the durable backup."
        ),
    }
