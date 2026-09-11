"""Background worker that runs UIA bounds/text extraction off the capture hot path.
capture_screenshot() used to call foreground_content_bounds() and
extract_accessibility_text() synchronously — both walk a live UIA tree and can
take anywhere from tens of milliseconds to multiple seconds depending on the
foreground app. This module moves that work onto a dedicated background
thread so a slow UIA call never delays the next screenshot.
Jobs carry the *hwnd* that was foreground at capture time (a cheap
win32gui.GetForegroundWindow() call, not a UIA walk) so the worker still
targets the right window even if focus has since moved on.
Results are persisted to disk, not just an in-memory cache, because the API
process (which runs screenshot_processor's backlog enrichment) is a
*different OS process* than the one that captures screenshots — an
in-memory cache would never be visible there.
"""

from __future__ import annotations


import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from core.accessibility_text import extract_accessibility_text_safe, foreground_content_bounds_safe
from core.app_settings import get_capture_settings
from core.ocr_crop import save_crop_metadata
from core.performance_metrics import increment, set_gauge, timed
from core.platform_support import get_window_metadata, window_key
from core.privacy_settings import is_clippy_window, should_redact_window

_MAX_QUEUE = 64


@dataclass
class UiaJob:
    screenshot_path: Path
    hwnd: int | None
    expected_window_key: str
    image_width: int
    image_height: int
    monitor: dict

_queue: "queue.Queue[UiaJob]" = queue.Queue(maxsize=_MAX_QUEUE)
_started = False
_started_lock = threading.Lock()


def a11y_text_path(screenshot_path: Path) -> Path:
    """Sidecar file the worker writes accessibility text to."""
    return screenshot_path.with_suffix(".a11y.txt")


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
        pass


def submit_uia_job(
    screenshot_path: Path,
    *,
    hwnd: int | None,
    expected_window_key: str,
    image_width: int,
    image_height: int,
    monitor: dict,
) -> None:
    """Enqueue UIA work for a just-captured screenshot. Never blocks the caller.
    ``expected_window_key`` should be window_key(metadata) for the window
    that was foreground *at capture time* — the worker uses it to confirm
    the target hasn't changed to something else (or something sensitive)
    before it runs, on every platform.
    """

    job = UiaJob(screenshot_path, hwnd, expected_window_key, image_width, image_height, monitor)

    try:
        _queue.put_nowait(job)
    except queue.Full:
        # Backlog is already worse than any single skipped frame's UIA data —
        # the heuristic crop and empty a11y text saved at capture time stand.
        increment("uia_worker.dropped")
    set_gauge("uia_worker.queue_depth", _queue.qsize())


def _target_still_safe(job: UiaJob) -> bool:
    """
    True only if the window that was foreground at capture time is still
    foreground, with the same title/URL, and isn't something privacy rules
    would redact right now.
    This is platform-neutral by design: window_key()/get_window_metadata()
    already abstract Windows (win32) vs. macOS (AppleScript) vs. Linux
    (xdotool). It intentionally does NOT try to use hwnd to look up a
    non-foreground window's identity on Windows — macOS has no equivalent,
    and keeping the check identical on both platforms means the privacy
    guarantee doesn't quietly get weaker depending on OS.
    """
    metadata = get_window_metadata()
    if not metadata:
        return False
    if window_key(metadata) != job.expected_window_key:
        return False
    process_name = metadata.get("process_name", "")
    title = metadata.get("current_window_title", "")
    return not (is_clippy_window(process_name, title) or should_redact_window(process_name, title))

def _process_job(job: UiaJob, timeout: float) -> None:
    with timed("uia_worker.job_total"):
        if not _target_still_safe(job):
            increment("uia_worker.target_changed")
            return
        with timed("uia_worker.bounds"):
            bounds = foreground_content_bounds_safe(job.hwnd, timeout=timeout)
        if bounds is not None:
            save_crop_metadata(
                job.screenshot_path,
                image_width=job.image_width,
                image_height=job.image_height,
                monitor=job.monitor,
                a11y_bounds=bounds,
            )
        else:
            increment("uia_worker.bounds_timeout_or_empty")
        with timed("uia_worker.text"):
            text = extract_accessibility_text_safe(job.hwnd, timeout=timeout)
        # Re-check after extraction too: on a slow call the user may have
        # switched windows (or navigated within the same one) while it ran.
        if not text.strip() or not _target_still_safe(job):
            increment("uia_worker.text_empty_or_target_changed")
            return
        _write_accessibility_text(job.screenshot_path, text)


def _worker_loop() -> None:
    print("[uia_worker] Started")
    while True:
        job = _queue.get()
        try:
            timeout = get_capture_settings()["uia_timeout_seconds"]
            _process_job(job, timeout)
        except Exception as exc:
            increment("uia_worker.errors")
            print(f"[uia_worker] job failed for {job.screenshot_path.name}: {exc}")
        finally:
            set_gauge("uia_worker.queue_depth", _queue.qsize())

def start_uia_worker() -> threading.Thread | None:
    global _started
    with _started_lock:
        if _started:
            return None
        _started = True
    thread = threading.Thread(target=_worker_loop, daemon=True, name="uia-worker")
    thread.start()
    return thread