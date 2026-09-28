import io
import re
import threading
import time
from pathlib import Path
from typing import Optional

import imagehash
import mss
from PIL import Image, ImageDraw
from core.performance_metrics import increment, timed

# Screenshot scheduling and privacy redaction. Text extraction is
# performed by screenshot_processor after a frame has been written.
try:
    from core.paths import get_screenshots_dir
except ImportError:
    from paths import get_screenshots_dir

try:
    from core.platform_support import (
        IS_MACOS,
        IS_WINDOWS,
        get_foreground_window_bounds,
        get_idle_seconds,
        get_window_metadata,
        window_key,
    )
except ImportError:
    from platform_support import (
        IS_MACOS,
        IS_WINDOWS,
        get_foreground_window_bounds,
        get_idle_seconds,
        get_window_metadata,
        window_key,
    )

_SCREENSHORT_DIR = get_screenshots_dir()

MIN_GAP_SECONDS = 8

BACKGROUND_INTERVALS_SECS = 60
# Retain only a bounded local window of visual context.
SCREENSHOT_TTL_MS = 24 * 60 * 60 * 1000  # 24 hours
JPEG_QUALITY = 75

# Coalesce typing, paste, and context-change notifications into one capture.
ACTIVITY_DEBOUNCE_SECONDS = 2.0

# HID idle stretches *background* cadence only — never hard-skips. Automated
# work (builds, logs, progress UIs) can still change the screen while the user
# is away; phash dedup drops truly static frames. Short polls so returning
# from idle resets within ~BACKGROUND_POLL_SECS, not after a long sleep.
IDLE_STRETCH_AFTER_SECS = 120.0
IDLE_BACKGROUND_GAP_SECS = 300.0
BACKGROUND_POLL_SECS = 10.0
PURGE_EVERY_SECS = 60.0


try:
    from core.privacy_settings import is_clippy_window, should_redact_window
except ImportError:
    # Redaction rules (Clippy window + user privacy toggles) live in privacy_settings.
    from privacy_settings import is_clippy_window, should_redact_window
try:
    from core.secret_fields import paint_screen_rects
    from core.secret_patterns import auth_page_label, paint_secret_text
except ImportError:
    from secret_fields import paint_screen_rects
    from secret_patterns import auth_page_label, paint_secret_text
try:
    from core.accessibility_text import collect_redaction_safe, extract_accessibility_text
    from core.app_settings import get_capture_settings
    from core.ocr_crop import save_crop_metadata
    from core.uia_worker import submit_uia_job
except ImportError:
    from accessibility_text import collect_redaction_safe, extract_accessibility_text
    from app_settings import get_capture_settings
    from ocr_crop import save_crop_metadata
    from uia_worker import submit_uia_job

_lock = threading.Lock()
_typing_lock = threading.Lock()
_last_capture_ms = 0
_last_capture_hash = None
_activity_timer: threading.Timer | None = None
_typing_active = False
_typing_frames: list[dict] = []
_TITLE_NOISE = re.compile(r"(\s+[-–—*].*|\s+\*)$")


def _title_stem(title: str) -> str:
    return _TITLE_NOISE.sub("", (title or "").casefold()).strip()


def _surface_parts(key: str) -> tuple[str, str, str]:
    parts = (key or "").split("\x1f")
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def same_typing_surface(earlier_key: str, later_key: str) -> bool:
    """Same app and tab. A browser tab is its URL; a desktop doc is its title."""
    if not earlier_key or not later_key or earlier_key == "unknown" or later_key == "unknown":
        return False
    process_a, title_a, url_a = _surface_parts(earlier_key)
    process_b, title_b, url_b = _surface_parts(later_key)
    if process_a != process_b or url_a != url_b:
        return False
    if url_a:
        return True
    stem_a = _title_stem(title_a)
    stem_b = _title_stem(title_b)
    return bool(stem_a) and (stem_a == stem_b or stem_a.startswith(stem_b) or stem_b.startswith(stem_a))


def is_text_continuation(earlier: str, later: str) -> bool:
    """True when the later screen text still contains the earlier draft."""
    old = " ".join((earlier or "").split()).casefold()
    new = " ".join((later or "").split()).casefold()
    if not old:
        return True
    if not new:
        return False
    if old in new:
        return True
    limit = min(len(old), len(new))
    shared = 0
    while shared < limit and old[shared] == new[shared]:
        shared += 1
    return shared >= max(12, int(len(old) * 0.8))


def superseded_typing_paths(frames: list[dict]) -> list[Path]:
    """Earlier frames fully covered by the latest shot on the same tab."""
    if len(frames) < 2:
        return []
    latest = frames[-1]
    if not str(latest.get("text") or "").strip():
        return []
    drop: list[Path] = []
    for earlier in frames[:-1]:
        if same_typing_surface(str(earlier.get("window_key") or ""), str(latest.get("window_key") or "")) and is_text_continuation(
            str(earlier.get("text") or ""), str(latest.get("text") or "")
        ):
            path = earlier.get("path")
            if path is not None:
                drop.append(Path(path))
    return drop


def _delete_screenshot(path: Path) -> None:
    path.unlink(missing_ok=True)
    path.with_name(path.stem + "_processed.jpg").unlink(missing_ok=True)
    path.with_suffix(".a11y.txt").unlink(missing_ok=True)
    path.with_suffix(".ocr-crop.json").unlink(missing_ok=True)
    path.with_suffix(".tmp").unlink(missing_ok=True)


def _discard_superseded_typing_frames() -> None:
    drop = superseded_typing_paths(_typing_frames)
    for path in drop:
        _delete_screenshot(path)
        increment("screenshots.typing_superseded")
        print(f"[capture] discarded superseded typing frame {path.name}")


def _remember_typing_frame(path: Path, key: str, text: str) -> None:
    if not _typing_active:
        return
    _typing_frames.append({"path": path, "window_key": key, "text": text})


def _foreground_accessibility_text() -> str:
    """Read foreground text only when the same window is safe to persist."""
    metadata = get_window_metadata()
    if not metadata:
        return ""
    process_name = metadata.get("process_name", "")
    title = metadata.get("current_window_title", "")
    if is_clippy_window(process_name, title) or should_redact_window(process_name, title):
        return ""
    captured_text = extract_accessibility_text()
    current = get_window_metadata()
    if not current:
        return ""
    original_key = (process_name, title, metadata.get("active_url"))
    current_key = (
        current.get("process_name", ""),
        current.get("current_window_title", ""),
        current.get("active_url"),
    )
    if current_key != original_key:
        return ""
    if is_clippy_window(current_key[0], current_key[1]) or should_redact_window(current_key[0], current_key[1]):
        return ""
    return captured_text


def _redact_clippy_windows(img: Image.Image, monitor: dict) -> None:
    """Paint a black rectangle over windows that should be hidden.

    Clippy Vision is only redacted when it is the foreground window — if it is
    minimized or behind another app, its rect no longer matches on-screen
    pixels, so we must not black that region out.
    User privacy targets (WhatsApp, etc.) are still redacted whenever visible.
    """
    draw = ImageDraw.Draw(img)

    if IS_WINDOWS:
        import psutil
        import win32api
        import win32gui
        import win32process

        scale = img.width / (win32api.GetSystemMetrics(0) or img.width)
        try:
            foreground_hwnd = win32gui.GetForegroundWindow()
        except Exception:
            foreground_hwnd = 0

        def _visit(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                name = psutil.Process(pid).name().lower()
                title = win32gui.GetWindowText(hwnd) or ""
            except Exception:
                return

            if is_clippy_window(name, title):
                # Only obscure Clippy when the user is actually looking at it
                # A hidden or minimized window does not occupy the saved pixels.
                if hwnd != foreground_hwnd:
                    return
            elif not should_redact_window(name, title):
                return

            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            x0 = max(0, int(left * scale))
            y0 = max(0, int(top * scale))
            x1 = min(img.width, int(right * scale))
            y1 = min(img.height, int(bottom * scale))
            if x1 > x0 and y1 > y0:
                draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0))

        win32gui.EnumWindows(_visit, None)
        return

    if IS_MACOS:
        # Accessibility permission is required for precise bounds. If it is
        # unavailable, fail closed and redact the whole frame.
        metadata = get_window_metadata()
        if not metadata:
            draw.rectangle([0, 0, img.width, img.height], fill=(0, 0, 0))
            return
        process_name = metadata.get("process_name", "")
        title = metadata.get("current_window_title", "")
        if not (is_clippy_window(process_name, title) or should_redact_window(process_name, title)):
            return

        bounds = get_foreground_window_bounds()
        if not bounds:
            draw.rectangle([0, 0, img.width, img.height], fill=(0, 0, 0))
            return

        monitor_left = float(monitor.get("left", 0))
        monitor_top = float(monitor.get("top", 0))
        monitor_width = float(monitor.get("width") or img.width)
        monitor_height = float(monitor.get("height") or img.height)
        left, top, right, bottom = bounds
        x0 = max(0, int((left - monitor_left) * img.width / monitor_width))
        y0 = max(0, int((top - monitor_top) * img.height / monitor_height))
        x1 = min(img.width, int((right - monitor_left) * img.width / monitor_width))
        y1 = min(img.height, int((bottom - monitor_top) * img.height / monitor_height))
        if x1 > x0 and y1 > y0:
            draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0))


def _foreground_hwnd() -> int | None:
    """Cheap window handle for the UIA worker to target later — no UIA involved.

    Windows-only concept: macOS has no handle to hand off, so the worker
    there always re-derives "what's frontmost" itself via get_window_metadata().
    """
    if not IS_WINDOWS:
        return None
    try:
        import win32gui

        return win32gui.GetForegroundWindow() or None
    except Exception:
        return None


def capture_screenshot(timestamp_ms: int, *, ignore_dedup: bool = False) -> Path | None:
    settings = get_capture_settings()
    if not settings["capture_screenshots"]:
        return None
    try:
        with timed("screenshot.total"):
            # monitor 0 is the virtual desktop; monitor 1 is the primary display.
            # The setting keeps the default capture cost low for multi-monitor Macs.
            with timed("screenshot.grab"):
                with mss.mss() as sct:
                    settings = get_capture_settings()
                    monitor = sct.monitors[0] if settings["capture_all_monitors"] else (sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0])
                    screenshot = sct.grab(monitor)
                    img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)

            hwnd = _foreground_hwnd()
            metadata = get_window_metadata()
            page_label = auth_page_label((metadata or {}).get("active_url") or "")
            redacted_text = ""
            with timed("screenshot.redaction"):
                _redact_clippy_windows(img, monitor)
                if page_label:
                    # The address is the auth page. Black that window from
                    # Win32 bounds. Query strings are not consulted. If the
                    # window rectangle is unknown, drop the frame.
                    rect = None
                    if hwnd and IS_WINDOWS:
                        import win32gui

                        try:
                            rect = win32gui.GetWindowRect(hwnd)
                        except Exception:
                            rect = None
                    if not rect:
                        increment("screenshots.secret_scan_timeout")
                        print("[capture] skipped frame; auth window bounds unknown")
                        return None
                    paint_screen_rects(img, monitor, [rect], allow_large=True)
                    redacted_text = page_label
                else:
                    with timed("screenshot.secret_fields"):
                        redaction = collect_redaction_safe(
                            hwnd, timeout=settings["uia_timeout_seconds"]
                        )
                    if redaction is None:
                        increment("screenshots.secret_scan_timeout")
                        print("[capture] skipped frame; field scan did not finish")
                        return None
                    paint_screen_rects(img, monitor, redaction.get("edit_rects") or [])
                    paint_screen_rects(
                        img,
                        monitor,
                        redaction.get("secret_rects") or [],
                        allow_large=True,
                    )
                    redacted_text = redaction.get("redacted_text") or ""
                with timed("screenshot.secret_text"):
                    paint_secret_text(img)

            # Hash after redaction so privacy changes are reflected in the
            # duplicate-frame decision. Near-identical frames are not persisted.
            with timed("screenshot.hash"):
                digest = imagehash.phash(img)
            global _last_capture_hash
            with _lock:
                if (
                    not ignore_dedup
                    and _last_capture_hash is not None
                    and (digest - _last_capture_hash) <= 2
                ):
                    increment("screenshots.deduplicated")
                    return None

            path = _SCREENSHORT_DIR / f"{timestamp_ms}.jpg"
            with timed("screenshot.jpeg_write"):
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
                path.write_bytes(buf.getvalue())
            # Secret text was already removed above. The worker only refreshes
            # the crop rectangle, then stores the scrubbed buffer from this frame.
            with timed("screenshot.crop_metadata"):
                save_crop_metadata(
                    path,
                    image_width=img.width,
                    image_height=img.height,
                    monitor=monitor,
                    a11y_bounds=None,
                )
            if metadata:
                process_name = metadata.get("process_name", "")
                title = metadata.get("current_window_title", "")
                if not (is_clippy_window(process_name, title) or should_redact_window(process_name, title)):
                    with timed("screenshot.uia_submit"):
                        submit_uia_job(
                            path,
                            hwnd=hwnd,
                            expected_window_key=window_key(metadata),
                            image_width=img.width,
                            image_height=img.height,
                            monitor=monitor,
                            redacted_text=redacted_text,
                        )
            with _lock:
                _last_capture_hash = digest
                _remember_typing_frame(path, window_key(metadata) if metadata else "unknown", redacted_text)
            increment("screenshots.captured")
            return path
    except Exception as e:
        increment("screenshots.errors")
        print(f"Error capturing screenshot: {e}")
        return None


def _capture_if_not_recent() -> None:
    global _last_capture_ms
    settings = get_capture_settings()
    if not settings["capture_screenshots"]:
        return
    # Reserve the timestamp before image work so concurrent activity callbacks
    # cannot start overlapping captures.
    with _lock:
        now_ms = int(time.time() * 1000)
        if now_ms - _last_capture_ms < settings["min_gap_seconds"] * 1000:
            return
        _last_capture_ms = now_ms


    capture_screenshot(now_ms)

def _capture_typing_frame() -> Path | None:
    """A typing frame ignores the idle gap and the near-duplicate check.

    Five new characters often do not move the image hash, and the closing
    frame has to be stored before the opening one can be judged redundant.
    """
    global _last_capture_ms
    settings = get_capture_settings()
    if not settings["capture_screenshots"]:
        return None
    with _lock:
        now_ms = int(time.time() * 1000)
        _last_capture_ms = now_ms
    return capture_screenshot(now_ms, ignore_dedup=True)


def begin_typing_capture(still_current=None) -> None:
    """Save the screen once a burst has enough typed characters to matter."""
    global _typing_active
    with _typing_lock:
        if still_current is not None and not still_current():
            return
        if _typing_active:
            return
        _typing_active = True
        _capture_typing_frame()


def finish_typing_capture() -> None:
    """Save the screen when the burst ends, then drop frames the latest one covers."""
    global _typing_active
    with _typing_lock:
        saved = _capture_typing_frame()
        if saved is not None:
            _discard_superseded_typing_frames()
        _typing_active = False
        _typing_frames.clear()


def purge_expired_screenshots() -> None:
    # Filenames begin with epoch milliseconds. Base retention is short; frames
    # linked to high-signal events get an adaptive TTL (see screenshot_ttl).
    # OCR remains on events.vision_ocr_text after the JPEG is deleted.
    from core.screenshot_ttl import should_purge_screenshot

    settings = get_capture_settings()
    now_ms = int(time.time() * 1000)
    base_days = settings["screenshot_retention_days"]
    flat_cutoff_ms = now_ms - int(base_days * 86400 * 1000)
    for path in _SCREENSHORT_DIR.glob("*.jpg"):
        try:
            ts_part = path.stem.split("_")[0]
            ts_ms = int(ts_part)
            # Still inside the base window — always keep.
            if ts_ms >= flat_cutoff_ms:
                continue
            if not should_purge_screenshot(path, settings=settings, now_ms=now_ms):
                continue
            path.unlink()
            path.with_suffix(".ocr-crop.json").unlink(missing_ok=True)
            path.with_suffix(".a11y.txt").unlink(missing_ok=True)
        except ValueError:
            continue
        except Exception as e:
            print(f"Error purging expired screenshots: {e}")

def _capture_after_activity() -> None:
    global _activity_timer
    with _lock:
        _activity_timer = None
    _capture_if_not_recent()


def on_activity_event() -> None:
    """Debounce noisy activity signals into a single screenshot request."""
    global _activity_timer
    settings = get_capture_settings()
    if not settings["capture_screenshots"]:
        return
    with _lock:
        if _activity_timer and _activity_timer.is_alive():
            return
        _activity_timer = threading.Timer(settings["activity_debounce_seconds"], _capture_after_activity)
        _activity_timer.daemon = True
        _activity_timer.start()

def get_screenshots_near(
    event_timestamp: float,
    max_count: int = 4,
    window_secs: float = 45,
) -> list[Path]:
    target_ms = int(event_timestamp * 1000)
    window_ms = int(window_secs * 1000)
    candidates: list[tuple[int, Path]] = []
    for path in _SCREENSHORT_DIR.glob("*.jpg"):
        try:
            ts_ms = int(path.stem.split("_", 1)[0])
        except ValueError:
            continue


        # Allow a small future window for camera lag, but never attach a frame
        # captured substantially after the event being explained.
        # Only consider screenshots taken up to window_secs before the event
        # or up to 10 s after (camera lag), never far-future shots.
        offset = ts_ms - target_ms
        if -window_ms <= offset <= 10_000:
            candidates.append((abs(offset), path))
    candidates.sort(key=lambda x: x[0])
    return [path for _, path in candidates[:max_count]]

def _background_gap_seconds() -> float:
    """How long between background frames given current HID idle.

    Active / lightly idle → normal ``background_interval_seconds``.
    Away (idle ≥ IDLE_STRETCH_AFTER_SECS) → longer gap, still capturing so
    on-screen automation is not missed. ``None`` idle (unsupported OS) keeps
    the normal interval.
    """
    settings = get_capture_settings()
    gap = float(settings["background_interval_seconds"])
    idle = get_idle_seconds()
    if idle is not None and idle >= IDLE_STRETCH_AFTER_SECS:
        gap = max(gap, IDLE_BACKGROUND_GAP_SECS)
    return gap


def _background_capture_due() -> bool:
    gap_ms = int(_background_gap_seconds() * 1000)
    with _lock:
        now_ms = int(time.time() * 1000)
        return (now_ms - _last_capture_ms) >= gap_ms


def start_background_capture() -> None:
    # Periodic capture preserves context when the user is reading, watching,
    # or when on-screen work continues without keyboard/mouse (builds, etc.).
    #
    # Deliberately NOT using load_backoff_multiplier() here. A skipped
    # capture is unrecoverable — there's no getting back a frame of what the
    # screen looked like a few minutes ago — unlike backlog *processing*
    # (screenshot_processor.py), where the file already sits safely on disk
    # and backing off only delays enrichment, at zero data-loss cost.
    #
    # Idle only *stretches* the gap between background frames; we poll often
    # so coming back from away resumes normal cadence within one poll tick.
    # Activity-triggered captures (on_activity_event) are unchanged and still
    # use min_gap_seconds.
    last_purge_at = 0.0
    while True:
        settings = get_capture_settings()
        if settings["capture_screenshots"] and _background_capture_due():
            idle = get_idle_seconds()
            if idle is not None and idle >= IDLE_STRETCH_AFTER_SECS:
                print(
                    f"[idle] background capture (idle={idle:.0f}s, "
                    f"gap={_background_gap_seconds():.0f}s)",
                    flush=True,
                )
            _capture_if_not_recent()

        now = time.time()
        if now - last_purge_at >= PURGE_EVERY_SECS:
            purge_expired_screenshots()
            last_purge_at = now

        time.sleep(BACKGROUND_POLL_SECS)

def start_screenshot_daemon() -> threading.Thread:
    # A daemon thread lets the desktop process exit without waiting on the
    # long-lived background loop.
    t = threading.Thread(target=start_background_capture, daemon=True)
    t.start()
    print("Screenshot scheduler started")
    return t
