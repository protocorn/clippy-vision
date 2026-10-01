"""Accessibility text saved next to a screenshot.

Capture walks the tree once, on the frame that asked for it, and writes the
result here. A second walk in another thread used to race that one and store
whichever window happened to be in front when it woke up.
"""

from __future__ import annotations

from pathlib import Path


def a11y_text_path(screenshot_path: Path) -> Path:
    """Sidecar file accessibility text is written to.

    Named from the capture timestamp, including after the JPEG is renamed
    to ``*_processed.jpg``.
    """
    from core.screenshot_files import capture_stem

    return screenshot_path.with_name(capture_stem(screenshot_path) + ".a11y.txt")


def read_persisted_accessibility_text(screenshot_path: Path) -> str:
    try:
        return a11y_text_path(screenshot_path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_accessibility_text(screenshot_path: Path, text: str) -> None:
    target = a11y_text_path(screenshot_path)
    temporary = target.with_suffix(".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(target)
    except OSError:
        return
    from core.screenshot_files import record_accessibility_text

    record_accessibility_text(screenshot_path, text)
