"""Persist and apply best-effort content crops for screenshot OCR."""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

_CROP_VERSION = 1
_MIN_CROP_WIDTH = 160
_MIN_CROP_HEIGHT = 100


def crop_metadata_path(screenshot_path: Path) -> Path:
    return screenshot_path.with_suffix(".ocr-crop.json")


def _clamp_box(
    box: tuple[int, int, int, int], image_width: int, image_height: int
) -> tuple[int, int, int, int] | None:
    left, top, right, bottom = box
    left = max(0, min(image_width, int(left)))
    top = max(0, min(image_height, int(top)))
    right = max(0, min(image_width, int(right)))
    bottom = max(0, min(image_height, int(bottom)))
    if right - left < _MIN_CROP_WIDTH or bottom - top < _MIN_CROP_HEIGHT:
        return None
    return left, top, right, bottom


def heuristic_content_crop(image_width: int, image_height: int) -> tuple[int, int, int, int]:
    """Conservative fallback: remove title/status bars and a likely left rail."""
    return (
        round(image_width * 0.14),
        round(image_height * 0.08),
        round(image_width * 0.98),
        round(image_height * 0.94),
    )


def normalize_a11y_bounds(
    bounds: tuple[int, int, int, int] | None,
    *,
    monitor: dict,
    image_width: int,
    image_height: int,
) -> tuple[int, int, int, int] | None:
    """Map screen-space UIA bounds into pixels of this captured monitor image."""
    if bounds is None:
        return None
    monitor_width = float(monitor.get("width") or image_width)
    monitor_height = float(monitor.get("height") or image_height)
    if monitor_width <= 0 or monitor_height <= 0:
        return None
    monitor_left = float(monitor.get("left", 0))
    monitor_top = float(monitor.get("top", 0))
    left, top, right, bottom = bounds
    return _clamp_box(
        (
            round((left - monitor_left) * image_width / monitor_width),
            round((top - monitor_top) * image_height / monitor_height),
            round((right - monitor_left) * image_width / monitor_width),
            round((bottom - monitor_top) * image_height / monitor_height),
        ),
        image_width,
        image_height,
    )


def save_crop_metadata(
    screenshot_path: Path,
    *,
    image_width: int,
    image_height: int,
    monitor: dict,
    a11y_bounds: tuple[int, int, int, int] | None,
) -> dict:
    """
    Store a crop alongside the screenshot. Geometry is captured while the
    original foreground UIA tree still exists; OCR may run later in another
    process after focus has changed.
    """
    box = normalize_a11y_bounds(
        a11y_bounds,
        monitor=monitor,
        image_width=image_width,
        image_height=image_height,
    )
    source = "a11y-region" if box else "heuristic"
    if box is None:
        box = heuristic_content_crop(image_width, image_height)
    box = _clamp_box(box, image_width, image_height)
    if box is None:
        return {}

    payload = {
        "version": _CROP_VERSION,
        "source": source,
        "box": list(box),
        "image_size": [image_width, image_height],
    }
    try:
        crop_metadata_path(screenshot_path).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return {}
    return payload


def load_crop_metadata(screenshot_path: Path) -> dict:
    try:
        data = json.loads(crop_metadata_path(screenshot_path).read_text(encoding="utf-8"))
        if data.get("version") != _CROP_VERSION or len(data.get("box") or []) != 4:
            return {}
        return data
    except (OSError, ValueError, TypeError):
        return {}


def crop_screenshot_for_ocr(screenshot_path: Path, target_path: Path) -> dict:
    """Write the saved crop to target_path; return metadata or an empty dict."""
    metadata = load_crop_metadata(screenshot_path)
    if not metadata:
        return {}
    try:
        with Image.open(screenshot_path) as image:
            box = _clamp_box(tuple(metadata["box"]), image.width, image.height)
            if box is None:
                return {}
            image.crop(box).save(target_path, format="JPEG", quality=90)
            return {**metadata, "box": list(box)}
    except (OSError, ValueError):
        return {}
