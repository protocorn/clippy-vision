"""Deferred OCR for frames skipped by the CPU/memory pressure gate.

The gate itself stays in core.ocr / model_residency.can_run_ocr(); this
module only records the skip and retries later. A sidecar next to the JPEG
survives process restart. Drain is rate-limited so a long capture under
load cannot stampede OCR the moment CPU drops.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from core.screenshot_files import (
    capture_stem,
    image_names,
    resolve_screenshot_file,
)

OCR_RETRY_SUFFIX = ".ocr-retry.json"
# First attempt waits this long after the skip so a still-hot CPU isn't
# immediately asked again. Later failed attempts double, capped below.
OCR_RETRY_BACKOFF_SECS = 15.0
OCR_RETRY_BACKOFF_MAX_SECS = 120.0
# Processor cycle is ~10s. Two frames per cycle is ~2–10s of OCR, not 39
# back-to-back passes the moment pressure clears.
OCR_RETRY_BATCH = 2


def ocr_retry_path(screenshot_path: Path) -> Path:
    return screenshot_path.parent / f"{capture_stem(screenshot_path)}{OCR_RETRY_SUFFIX}"


def _read_payload(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_payload(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def _backoff_secs(attempts: int) -> float:
    delay = OCR_RETRY_BACKOFF_SECS * (2 ** max(0, int(attempts)))
    return min(OCR_RETRY_BACKOFF_MAX_SECS, delay)


def schedule_ocr_retry(
    screenshot_path: Path,
    reason: str | None,
    *,
    attempted: bool = False,
    now: float | None = None,
) -> dict:
    """Remember that this JPEG still needs OCR. Bounded by next_at backoff."""
    path = ocr_retry_path(screenshot_path)
    existing = _read_payload(path) if path.exists() else {}
    attempts = int(existing.get("attempts") or 0)
    if attempted:
        attempts += 1
    stamp = time.time() if now is None else now
    delay = _backoff_secs(attempts)
    payload = {
        "reason": reason or existing.get("reason") or "pressure gate",
        "attempts": attempts,
        "next_at": stamp + delay,
        "updated_at": stamp,
        "screenshot": screenshot_path.name,
    }
    _write_payload(path, payload)
    action = "re-queued" if attempted else "queued"
    print(
        f"[ocr] {action} retry for {screenshot_path.name} — {payload['reason']} "
        f"(attempt {attempts}, next in {delay:.0f}s)"
    )
    return payload


def clear_ocr_retry(screenshot_path: Path) -> None:
    try:
        ocr_retry_path(screenshot_path).unlink(missing_ok=True)
    except OSError:
        pass


def pending_ocr_retries(
    directory: Path,
    *,
    now: float | None = None,
    limit: int = OCR_RETRY_BATCH,
) -> list[Path]:
    """Due JPEG paths, oldest capture first, capped at *limit*."""
    if not directory.is_dir():
        return []
    stamp = time.time() if now is None else now
    due: list[tuple[int, Path, Path]] = []
    for sidecar in directory.glob(f"*{OCR_RETRY_SUFFIX}"):
        payload = _read_payload(sidecar)
        if float(payload.get("next_at") or 0) > stamp:
            continue
        stem = sidecar.name[: -len(OCR_RETRY_SUFFIX)]
        image = resolve_screenshot_file(directory, f"{stem}.jpg")
        if image is None:
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        capture_ms = 0
        try:
            capture_ms = int(stem.split("_", 1)[0])
        except ValueError:
            capture_ms = 0
        due.append((capture_ms, image, sidecar))
    due.sort(key=lambda item: item[0])
    return [image for _, image, _ in due[: max(0, int(limit))]]


def _event_screen_text(screenshot_path: Path) -> str:
    from core.screenshot_files import _db

    raw_name, processed_name = image_names(capture_stem(screenshot_path))
    row = _db().execute(
        """SELECT vision_ocr_text FROM events
           WHERE screenshot_filename IN (?, ?)
           LIMIT 1""",
        (raw_name, processed_name),
    ).fetchone()
    return (row[0] or "").strip() if row else ""


def retry_skipped_ocr(
    directory: Path | None = None,
    *,
    now: float | None = None,
    limit: int = OCR_RETRY_BATCH,
) -> int:
    """OCR due skipped frames once pressure has dropped. Returns fills.

    Does not run while can_run_ocr() is false, so sustained load only
    parks the queue. A due batch is still gated inside extract_text.
    """
    from core.accessibility_text import is_useful_accessibility_text
    from core.model_residency import can_run_ocr, ocr_unavailable_reason
    from core.ocr import last_gate_reason, set_ocr_log_label
    from core.paths import get_screenshots_dir
    from core.performance_metrics import increment
    from core.screenshot_enrichment import extract_screenshot_ocr
    from core.screenshot_files import record_accessibility_text

    folder = directory or get_screenshots_dir()
    if not can_run_ocr():
        return 0

    filled = 0
    stamp = time.time() if now is None else now
    for path in pending_ocr_retries(folder, now=stamp, limit=limit):
        existing = _event_screen_text(path)
        if is_useful_accessibility_text(existing):
            clear_ocr_retry(path)
            continue
        set_ocr_log_label(path.name)
        try:
            text = extract_screenshot_ocr(path)
        except Exception as exc:
            print(f"[ocr] retry failed for {path.name}: {exc}")
            schedule_ocr_retry(
                path,
                ocr_unavailable_reason() or last_gate_reason() or str(exc),
                attempted=True,
                now=stamp,
            )
            continue
        finally:
            set_ocr_log_label(None)

        if last_gate_reason():
            schedule_ocr_retry(path, last_gate_reason(), attempted=True, now=stamp)
            continue
        if str(text or "").strip():
            record_accessibility_text(path, text)
            print(f"[ocr] retry filled {path.name} ({len(text)} chars)")
            increment("ocr.retry_filled")
            filled += 1
        else:
            print(f"[ocr] retry of {path.name} found no text")
            increment("ocr.retry_empty")
        clear_ocr_retry(path)
    return filled
