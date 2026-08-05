from pathlib import Path
import io
import imagehash
import mss
from PIL import Image, ImageDraw
import threading
import time
from typing import Optional

try:
    from core.paths import get_screenshots_dir
except ImportError:
    from paths import get_screenshots_dir

try:
    from core.platform_support import (
        IS_MACOS,
        IS_WINDOWS,
        get_foreground_window_bounds,
        get_window_metadata,
    )
except ImportError:
    from platform_support import (
        IS_MACOS,
        IS_WINDOWS,
        get_foreground_window_bounds,
        get_window_metadata,
    )

_SCREENSHORT_DIR = get_screenshots_dir()

MIN_GAP_SECONDS = 8

BACKGROUND_INTERVALS_SECS = 60
SCREENSHOT_TTL_MS = 24 * 60 * 60 * 1000
JPEG_QUALITY = 75


ACTIVITY_DEBOUNCE_SECONDS = 2.0


try:
    from core.privacy_settings import is_clippy_window, should_redact_window
except ImportError:
    from privacy_settings import is_clippy_window, should_redact_window
try:
    from core.app_settings import get_capture_settings
except ImportError:
    from app_settings import get_capture_settings

_lock = threading.Lock()
_last_capture_ms = 0
_last_capture_hash = None
_activity_timer: Optional[threading.Timer] = None


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


def capture_screenshot(timestamp_ms: int) -> Optional[Path]:
    settings = get_capture_settings()
    if not settings["capture_screenshots"]:
        return None
    try:
        with mss.mss() as sct:
            settings = get_capture_settings()
            monitor = sct.monitors[0] if settings["capture_all_monitors"] else (sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0])
            screenshot = sct.grab(monitor)
            img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)

        _redact_clippy_windows(img, monitor)

        digest = imagehash.phash(img)
        global _last_capture_hash
        with _lock:
            if _last_capture_hash is not None and (digest - _last_capture_hash) <= 2:
                return None

        path = _SCREENSHORT_DIR / f"{timestamp_ms}.jpg"
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        path.write_bytes(buf.getvalue())
        with _lock:
            _last_capture_hash = digest
        return path
    except Exception as e:
        print(f"Error capturing screenshot: {e}")
        return None


def _capture_if_not_recent() -> None:
    global _last_capture_ms
    settings = get_capture_settings()
    if not settings["capture_screenshots"]:
        return
    with _lock:
        now_ms = int(time.time() * 1000)
        if now_ms - _last_capture_ms < settings["min_gap_seconds"] * 1000:
            return
        _last_capture_ms = now_ms


    capture_screenshot(now_ms)

def purge_expired_screenshots() -> None:
    retention_days = get_capture_settings()["screenshot_retention_days"]
    cutoff_ms = int(time.time() * 1000) - int(retention_days * 86400 * 1000)
    for path in _SCREENSHORT_DIR.glob("*.jpg"):
        try:
            ts_part = path.stem.split("_")[0]
            if int(ts_part) < cutoff_ms:
                path.unlink()
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


        offset = ts_ms - target_ms
        if -window_ms <= offset <= 10_000:
            candidates.append((abs(offset), path))
    candidates.sort(key=lambda x: x[0])
    return [path for _, path in candidates[:max_count]]

def start_background_capture() -> None:
    while True:
        _capture_if_not_recent()
        purge_expired_screenshots()
        time.sleep(get_capture_settings()["background_interval_seconds"])

def start_vision_daemon() -> threading.Thread:
    t = threading.Thread(target=start_background_capture, daemon=True)
    t.start()
    print("Vision daemon started")
    return t
