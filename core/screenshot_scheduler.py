import io
import re
import threading
import time
from pathlib import Path
from typing import Optional

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
# Retention is measured in days. Scanning every image and its event once a
# minute stalls the capture thread for work that can wait an hour.
PURGE_EVERY_SECS = 3600.0


try:
    from core.privacy_settings import dev_electron_is_clippy, hides_screen_text, is_clippy_window, should_redact_window
except ImportError:
    # Redaction rules (Clippy window + user privacy toggles) live in privacy_settings.
    from privacy_settings import dev_electron_is_clippy, hides_screen_text, is_clippy_window, should_redact_window
try:
    from core.secret_fields import paint_screen_rects
    from core.secret_patterns import auth_page_label, paint_secret_text
except ImportError:
    from secret_fields import paint_screen_rects
    from secret_patterns import auth_page_label, paint_secret_text
try:
    from core.accessibility_text import collect_redaction, extract_accessibility_text
    from core.app_settings import get_capture_settings, should_watch_process
    from core.ocr_crop import save_crop_metadata
except ImportError:
    from accessibility_text import collect_redaction, extract_accessibility_text
    from app_settings import get_capture_settings, should_watch_process
    from ocr_crop import save_crop_metadata

_lock = threading.Lock()
_walk_lock = threading.Lock()
_last_capture_ms = 0
_last_capture_path: Path | None = None
_activity_timer: threading.Timer | None = None
# Frames whose text has been stored. A covered frame is marked, then deleted
# on a later sweep, so a walk still in flight is not removed out from under itself.
_kept_frames: list[dict] = []
_FRAME_MEMORY = 200
DISCARD_AFTER_SECONDS = 90.0
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
    """True when the later screen text still contains the earlier draft.

    An empty earlier read is not a draft. Covering it would delete a frame
    whose walk failed.
    """
    old = " ".join((earlier or "").split()).casefold()
    new = " ".join((later or "").split()).casefold()
    if not old or not new:
        return False
    if old in new:
        return True
    limit = min(len(old), len(new))
    shared = 0
    while shared < limit and old[shared] == new[shared]:
        shared += 1
    return shared >= max(12, int(len(old) * 0.8))


def text_covers(earlier: str, later: str) -> bool:
    """True when every line of the earlier text is still in the later text.

    Order can change when a later walk puts the edit first. A line that
    scrolled off the screen is not covered, so that frame stays.
    """
    if is_text_continuation(earlier, later):
        return True
    old = [line.casefold() for line in (earlier or "").splitlines() if line.strip()]
    new = "\n".join(line.casefold() for line in (later or "").splitlines() if line.strip())
    if not old or not new:
        return False
    return all(line in new for line in old)


def superseded_typing_paths(frames: list[dict]) -> list[Path]:
    """Earlier frames fully covered by the latest shot on the same tab.

    This only names them. Capture marks those frames and deletes them later.
    """
    if len(frames) < 2:
        return []
    latest = frames[-1]
    if not str(latest.get("text") or "").strip():
        return []
    drop: list[Path] = []
    for earlier in frames[:-1]:
        if same_typing_surface(str(earlier.get("window_key") or ""), str(latest.get("window_key") or "")) and text_covers(
            str(earlier.get("text") or ""), str(latest.get("text") or "")
        ):
            path = earlier.get("path")
            if path is not None:
                drop.append(Path(path))
    return drop


def _note_frame(path: Path, key: str, text: str, *, pending: bool) -> None:
    with _lock:
        found = None
        for frame in _kept_frames:
            if Path(frame.get("path")) == path:
                found = frame
                break
        if found is None:
            _kept_frames.append(
                {
                    "path": path,
                    "window_key": key,
                    "text": text,
                    "pending": pending,
                    "marked_at": None,
                }
            )
        else:
            found["window_key"] = key or found.get("window_key") or ""
            if text.strip():
                found["text"] = text
            found["pending"] = pending
        while len(_kept_frames) > _FRAME_MEMORY:
            drop_at = next(
                (index for index, frame in enumerate(_kept_frames) if not frame.get("marked_at")),
                None,
            )
            if drop_at is None:
                break
            _kept_frames.pop(drop_at)


def _mark_covered_frames() -> None:
    """Mark an earlier frame when a later one on the same tab still contains it."""
    with _lock:
        for index, earlier in enumerate(_kept_frames):
            if earlier.get("marked_at") or earlier.get("pending"):
                continue
            earlier_text = str(earlier.get("text") or "")
            if not earlier_text.strip():
                continue
            for later in _kept_frames[index + 1 :]:
                if later.get("pending"):
                    continue
                later_text = str(later.get("text") or "")
                if not later_text.strip():
                    continue
                if not same_typing_surface(str(earlier.get("window_key") or ""), str(later.get("window_key") or "")):
                    continue
                if text_covers(earlier_text, later_text):
                    earlier["marked_at"] = time.time()
                    break


def _delete_marked_frame(path: Path) -> None:
    from core.screenshot_files import capture_stem, delete_screenshot_files, image_names

    raw_name, processed_name = image_names(capture_stem(path))
    delete_screenshot_files(path, include_text=True)
    try:
        from core.storage import conn

        conn.execute(
            "UPDATE events SET screenshot_filename = NULL WHERE screenshot_filename IN (?, ?)",
            (raw_name, processed_name),
        )
        conn.commit()
    except Exception:
        pass
    increment("screenshots.typing_superseded")
    print(f"[capture] deleted covered frame {path.name}")


def sweep_discarded_frames() -> None:
    """Delete frames that were marked covered long enough ago to be finished."""
    now = time.time()
    due: list[Path] = []
    with _lock:
        kept: list[dict] = []
        for frame in _kept_frames:
            marked = frame.get("marked_at")
            path = frame.get("path")
            if (
                marked
                and not frame.get("pending")
                and path is not None
                and now - float(marked) >= DISCARD_AFTER_SECONDS
            ):
                due.append(Path(path))
                continue
            kept.append(frame)
        _kept_frames[:] = kept
    for path in due:
        _delete_marked_frame(path)


def _commit_frame_text(
    path: Path,
    key: str,
    redaction: dict,
    *,
    monitor: dict | None = None,
    image_size: tuple[int, int] | None = None,
) -> None:
    """Store this walk on the frame that started it, then mark what it covers."""
    from core.uia_worker import _write_accessibility_text

    text = str(redaction.get("redacted_text") or "")
    bounds = redaction.get("content_bounds")
    if (
        monitor
        and image_size
        and isinstance(bounds, (list, tuple))
        and len(bounds) == 4
        and path.is_file()
    ):
        save_crop_metadata(
            path,
            image_width=image_size[0],
            image_height=image_size[1],
            monitor=monitor,
            a11y_bounds=tuple(int(value) for value in bounds),
        )
    if text.strip():
        _write_accessibility_text(path, text)
    _note_frame(path, key, text, pending=False)
    _mark_covered_frames()


def _foreground_accessibility_text() -> str:
    """Read foreground text only when the same window is safe to persist."""
    metadata = get_window_metadata()
    if not metadata:
        return ""
    process_name = metadata.get("process_name", "")
    title = metadata.get("current_window_title", "")
    executable = _process_executable(_foreground_hwnd())
    if (
        is_clippy_window(process_name, title)
        or should_redact_window(process_name, title)
        or dev_electron_is_clippy(process_name, executable)
    ):
        return ""
    from core.private_windows import window_is_private

    if window_is_private(_foreground_hwnd(), process_name):
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
    if (
        is_clippy_window(current_key[0], current_key[1])
        or should_redact_window(current_key[0], current_key[1])
        or dev_electron_is_clippy(current_key[0], "")
    ):
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

            from core.private_windows import window_is_private

            private_window = window_is_private(hwnd, name)
            executable = ""
            from core.process_names import process_key

            if process_key(name) == "electron":
                try:
                    executable = psutil.Process(pid).exe() or ""
                except Exception:
                    executable = ""
            if is_clippy_window(name, title) or dev_electron_is_clippy(name, executable):
                # Only obscure Clippy when the user is actually looking at it.
                # A hidden or minimized window does not occupy the saved pixels.
                if hwnd != foreground_hwnd:
                    return
            elif not private_window and not should_redact_window(name, title):
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
        # Same rule as Windows: privacy-listed windows are painted wherever
        # they are visible. Clippy is painted only while it is in front.
        # If Accessibility cannot be queried, hide the whole frame.
        try:
            from core.mac_ui import list_visible_windows
        except ImportError:
            from mac_ui import list_visible_windows

        windows = list_visible_windows()
        if windows is None:
            draw.rectangle([0, 0, img.width, img.height], fill=(0, 0, 0))
            return
        from core.private_windows import bounds_overlap, mac_private_bounds
        from core.process_names import process_key

        foreground = get_window_metadata() or {}
        front_process = str(foreground.get("process_name") or "")
        private_rects: dict[str, list] = {}
        for item in windows:
            process_name = str(item.get("process_name") or "")
            title = str(item.get("title") or "")
            bounds = item.get("bounds")
            key = process_key(process_name)
            if key not in private_rects and key in {"chrome", "edge", "brave"}:
                private_rects[key] = mac_private_bounds(process_name)
            private_hit = bool(bounds) and any(
                bounds_overlap(tuple(bounds), rect) for rect in private_rects.get(key, [])
            )
            if is_clippy_window(process_name, title):
                if process_name.casefold() != front_process.casefold():
                    continue
            elif not private_hit and not should_redact_window(process_name, title):
                continue
            if not bounds:
                continue
            paint_screen_rects(img, monitor, [tuple(bounds)], allow_large=True)


def _process_executable(hwnd: int | None) -> str:
    """Path of the foreground process. Empty when it cannot be read."""
    if not hwnd or not IS_WINDOWS:
        return ""
    try:
        import psutil
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).exe() or ""
    except Exception:
        return ""


def _hwnd_title(hwnd: int) -> str:
    if not IS_WINDOWS:
        return ""
    try:
        import win32gui

        return win32gui.GetWindowText(int(hwnd)) or ""
    except Exception:
        return ""


def _walk_still_matches(job: dict) -> bool:
    """True when this walk is still the window the JPEG was taken from.

    A queued walk can run after the person has switched apps or tabs.
    That tree belongs to the new window, so it must not be stored on this frame.
    Jobs without a captured title are the older callers and still run.
    """
    if "title" not in job and "process" not in job:
        return True
    hwnd = job.get("hwnd")
    if not isinstance(hwnd, int):
        return False
    current = _foreground_hwnd()
    if isinstance(current, int) and int(current) != int(hwnd):
        return False
    title = str(job.get("title") or "")
    if not title:
        return True
    live = _hwnd_title(hwnd)
    return not live or live == title


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


def _empty_redaction(**flags) -> dict:
    found = {
        "edit_rects": [],
        "secret_rects": [],
        "redacted_text": "",
        "content_bounds": None,
    }
    found.update(flags)
    return found


def _field_redaction(hwnd: int | None, frame_window_key: str, timeout: float) -> dict:
    """Walk this window once. The text is stored on the frame that started the walk.

    A second capture does not start another walk, and it does not copy the
    previous walk's text onto a new picture. ``frame_window_key`` is recorded
    when the JPEG exists, so a slow walk cannot land on a later frame.
    """
    del frame_window_key
    if not _walk_lock.acquire(blocking=False):
        increment("screenshots.walk_busy")
        return _empty_redaction(busy=True)

    slot = {
        "result": None,
        "path": None,
        "key": "",
        "monitor": None,
        "image_size": None,
        "walked": threading.Event(),
        "saved": threading.Event(),
    }

    def _walker() -> None:
        try:
            if IS_WINDOWS:
                import uiautomation as auto

                with auto.UIAutomationInitializerInThread():
                    slot["result"] = collect_redaction(hwnd)
            else:
                slot["result"] = collect_redaction(hwnd)
        except Exception:
            slot["result"] = None
        finally:
            slot["walked"].set()
            slot["saved"].wait(30)
            path = slot.get("path")
            result = slot.get("result")
            try:
                if isinstance(path, Path) and isinstance(result, dict):
                    size = slot.get("image_size")
                    _commit_frame_text(
                        path,
                        str(slot.get("key") or ""),
                        result,
                        monitor=slot.get("monitor") if isinstance(slot.get("monitor"), dict) else None,
                        image_size=size if isinstance(size, tuple) else None,
                    )
            finally:
                _walk_lock.release()

    worker = threading.Thread(target=_walker, daemon=True, name="a11y-walk")
    try:
        worker.start()
    except Exception:
        _walk_lock.release()
        return _empty_redaction()
    if not slot["walked"].wait(timeout):
        increment("screenshots.secret_scan_timeout")
        print("[capture] field scan did not finish; keeping frame")
        return _empty_redaction(pending=True, slot=slot)
    result = slot["result"]
    if not isinstance(result, dict):
        return _empty_redaction(slot=slot)
    found = dict(result)
    found["slot"] = slot
    return found


_WALK_QUEUE_MAX = 3
_walk_jobs: list[dict] = []
_walk_cv = threading.Condition()
_walker_thread: threading.Thread | None = None


def _ensure_walk_worker() -> None:
    global _walker_thread
    with _walk_cv:
        if _walker_thread is not None and _walker_thread.is_alive():
            return
        _walker_thread = threading.Thread(target=_walk_loop, daemon=True, name="a11y-walk")
        _walker_thread.start()


def _enqueue_walk(job: dict) -> None:
    """Queue this frame's tree walk. The JPEG is already on disk."""
    dropped: list[dict] = []
    with _walk_cv:
        while len(_walk_jobs) >= _WALK_QUEUE_MAX:
            dropped.append(_walk_jobs.pop(0))
        _walk_jobs.append(job)
        _walk_cv.notify()
    for old in dropped:
        path = old.get("path")
        if isinstance(path, Path):
            _note_frame(path, str(old.get("key") or ""), "", pending=False)
    _ensure_walk_worker()


def _walk_loop() -> None:
    while True:
        with _walk_cv:
            while not _walk_jobs:
                _walk_cv.wait()
            job = _walk_jobs.pop(0)
        try:
            _run_walk_job(job)
        except Exception as exc:
            print(f"[capture] field scan failed: {exc}")
            path = job.get("path")
            if isinstance(path, Path):
                _note_frame(path, str(job.get("key") or ""), "", pending=False)


def _run_walk_job(job: dict) -> None:
    """Walk the window that was in front when this JPEG was taken."""
    path = job.get("path")
    if not isinstance(path, Path) or not path.is_file():
        return
    if not _walk_still_matches(job):
        print(f"[capture] skipped text for {path.name}; the window changed")
        _note_frame(path, str(job.get("key") or ""), "", pending=False)
        return
    hwnd = job.get("hwnd")
    try:
        if IS_WINDOWS:
            import uiautomation as auto

            with auto.UIAutomationInitializerInThread():
                result = collect_redaction(hwnd if isinstance(hwnd, int) else None)
        else:
            result = collect_redaction(hwnd if isinstance(hwnd, int) else None)
    except Exception:
        result = None
    if not isinstance(result, dict):
        _note_frame(path, str(job.get("key") or ""), "", pending=False)
        return
    monitor = job.get("monitor")
    _paint_walk_onto_frame(path, result, monitor if isinstance(monitor, dict) else None)
    size = job.get("image_size")
    _commit_frame_text(
        path,
        str(job.get("key") or ""),
        result,
        monitor=monitor if isinstance(monitor, dict) else None,
        image_size=size if isinstance(size, tuple) else None,
    )


def _paint_walk_onto_frame(path: Path, redaction: dict, monitor: dict | None) -> None:
    """Black out secret fields once the walk knows where they are."""
    edit_rects = redaction.get("edit_rects") or []
    secret_rects = redaction.get("secret_rects") or []
    text = str(redaction.get("redacted_text") or "")
    if not edit_rects and not secret_rects and not text.strip():
        return
    if not isinstance(monitor, dict):
        return
    try:
        img = Image.open(path).convert("RGB")
        if edit_rects:
            paint_screen_rects(img, monitor, edit_rects)
        if secret_rects:
            paint_screen_rects(img, monitor, secret_rects, allow_large=True)
        if text.strip():
            paint_secret_text(img, text)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        path.write_bytes(buf.getvalue())
    except Exception as exc:
        print(f"[capture] could not paint fields on {path.name}: {exc}")


def _release_walk_slot(slot: dict | None, path: Path | None, key: str, monitor: dict, image_size: tuple[int, int]) -> None:
    """Hand the finished JPEG to the walk, or let the walk exit if there is no file."""
    if not slot:
        return
    if path is not None:
        slot["path"] = path
        slot["key"] = key
        slot["monitor"] = monitor
        slot["image_size"] = image_size
    slot["saved"].set()


def capture_screenshot(timestamp_ms: int) -> Path | None:
    """Save the JPEG now. Walk that window afterwards, without blocking the next frame."""
    settings = get_capture_settings()
    if not settings["capture_screenshots"]:
        return None
    foreground = get_window_metadata()
    process_name = (foreground or {}).get("process_name") or ""
    if process_name and not should_watch_process(process_name):
        return None
    # Cursor and VS Code hide the open file until this setting is on.
    from core.editor_accessibility import ensure_screen_reader_support

    ensure_screen_reader_support(process_name)
    try:
        with timed("screenshot.total"):
            # monitor 0 is the virtual desktop; monitor 1 is the primary display.
            # The setting keeps the default capture cost low for multi-monitor Macs.
            hwnd_before = _foreground_hwnd()
            metadata_before = get_window_metadata()
            with timed("screenshot.grab"):
                with mss.mss() as sct:
                    settings = get_capture_settings()
                    monitor = dict(
                        sct.monitors[0]
                        if settings["capture_all_monitors"]
                        else (sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0])
                    )
                    screenshot = sct.grab(monitor)
                    img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)

            hwnd_after = _foreground_hwnd()
            window_changed = (
                isinstance(hwnd_before, int)
                and isinstance(hwnd_after, int)
                and int(hwnd_before) != int(hwnd_after)
            )
            hwnd = hwnd_before or hwnd_after
            metadata = metadata_before or get_window_metadata()
            from core.private_windows import window_is_private

            foreground_private = window_is_private(
                hwnd, (metadata or {}).get("process_name") or ""
            )
            foreground_process = (metadata or {}).get("process_name") or ""
            foreground_title = (metadata or {}).get("current_window_title") or ""
            hide_text = foreground_private or hides_screen_text(
                foreground_process,
                foreground_title,
                _process_executable(hwnd),
            )
            page_label = auth_page_label((metadata or {}).get("active_url") or "")
            redacted_text = ""
            defer_walk = False
            with timed("screenshot.redaction"):
                _redact_clippy_windows(img, monitor)
                if hide_text:
                    # The window is already black. Do not read its tree.
                    redacted_text = ""
                elif page_label:
                    # The address is the auth page. Black that window.
                    # Query strings are not consulted. If the window
                    # rectangle is unknown, drop the frame.
                    rect = None
                    if hwnd and IS_WINDOWS:
                        import win32gui

                        try:
                            rect = win32gui.GetWindowRect(hwnd)
                        except Exception:
                            rect = None
                    elif IS_MACOS:
                        rect = get_foreground_window_bounds()
                    if not rect:
                        increment("screenshots.secret_scan_timeout")
                        print("[capture] skipped frame; auth window bounds unknown")
                        return None
                    paint_screen_rects(img, monitor, [rect], allow_large=True)
                    redacted_text = page_label
                else:
                    # The pixels are the window from before the grab. A switch
                    # during the grab means the tree would describe the new one.
                    defer_walk = not window_changed
                if redacted_text and not foreground_private:
                    with timed("screenshot.secret_text"):
                        paint_secret_text(img, redacted_text)

            path = _SCREENSHORT_DIR / f"{timestamp_ms}.jpg"
            with timed("screenshot.jpeg_write"):
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
                path.write_bytes(buf.getvalue())
            with timed("screenshot.crop_metadata"):
                save_crop_metadata(
                    path,
                    image_width=img.width,
                    image_height=img.height,
                    monitor=monitor,
                    a11y_bounds=None,
                )
            frame_key = window_key(metadata) if metadata else ""
            from core.screenshot_files import write_frame_window

            write_frame_window(path, foreground_process, foreground_title)
            if defer_walk:
                _note_frame(path, frame_key, "", pending=True)
                _enqueue_walk(
                    {
                        "hwnd": hwnd,
                        "path": path,
                        "key": frame_key,
                        "monitor": monitor,
                        "image_size": (img.width, img.height),
                        "process": foreground_process,
                        "title": foreground_title,
                    }
                )
            elif str(redacted_text).strip():
                from core.uia_worker import _write_accessibility_text

                _write_accessibility_text(path, redacted_text)
                _note_frame(path, frame_key, redacted_text, pending=False)
                _mark_covered_frames()
            with _lock:
                _last_capture_path = path
            increment("screenshots.captured")
            return path
    except Exception as e:
        increment("screenshots.errors")
        print(f"Error capturing screenshot: {e}")
        return None


def _reserve_capture(ignore_gap: bool = False) -> tuple[int, int] | None:
    """Claim a capture timestamp. Returns (now_ms, previous_ms), or None if too soon."""
    global _last_capture_ms
    settings = get_capture_settings()
    with _lock:
        now_ms = int(time.time() * 1000)
        previous = _last_capture_ms
        if not ignore_gap and now_ms - previous < settings["min_gap_seconds"] * 1000:
            return None
        _last_capture_ms = now_ms
        return now_ms, previous


def _restore_capture_clock(now_ms: int, previous: int) -> None:
    global _last_capture_ms
    with _lock:
        if _last_capture_ms == now_ms:
            _last_capture_ms = previous


def _capture_if_not_recent() -> None:
    settings = get_capture_settings()
    if not settings["capture_screenshots"]:
        return
    reserved = _reserve_capture()
    if reserved is None:
        return
    now_ms, previous = reserved
    if capture_screenshot(now_ms) is None:
        _restore_capture_clock(now_ms, previous)


def _capture_typing_frame() -> Path | None:
    """Save the screen for a typing pause or a character checkpoint."""
    settings = get_capture_settings()
    if not settings["capture_screenshots"]:
        return None
    reserved = _reserve_capture(ignore_gap=True)
    if reserved is None:
        return None
    now_ms, previous = reserved
    path = capture_screenshot(now_ms)
    if path is None:
        _restore_capture_clock(now_ms, previous)
    return path


def checkpoint_typing_capture() -> None:
    """Save the screen in the background after a long run of typing.

    The keyboard listener must not wait on the accessibility walk.
    """
    threading.Thread(target=_capture_typing_frame, daemon=True, name="typing-checkpoint").start()


def finish_typing_capture(hwnd: int | None = None) -> bool:
    """Save the screen when a burst pauses, while that window is still in front.

    A tab change has already dropped the previous document from the tree.
    The checkpoint taken while typing is what keeps that text.
    """
    current = _foreground_hwnd()
    if hwnd and current and int(hwnd) != int(current):
        return False
    return _capture_typing_frame() is not None


def purge_expired_screenshots() -> None:
    # Filenames begin with epoch milliseconds. Base retention is short; frames
    # linked to high-signal events get an adaptive TTL (see screenshot_ttl).
    # OCR remains on events.vision_ocr_text after the JPEG is deleted.
    # Expired event and session rows are deleted on this same pass. Capture
    # used to do that only once, at startup.
    from core.screenshot_ttl import should_purge_screenshot
    from core.storage import purge_expired

    try:
        purge_expired()
    except Exception as e:
        print(f"Error purging expired events: {e}")
    try:
        sweep_discarded_frames()
    except Exception as e:
        print(f"Error sweeping covered frames: {e}")

    settings = get_capture_settings()
    now_ms = int(time.time() * 1000)
    base_days = settings["screenshot_retention_days"]
    flat_cutoff_ms = now_ms - int(base_days * 86400 * 1000)
    from core.screenshot_files import capture_stem, delete_screenshot_files, sweep_screenshot_sidecars

    seen: set[str] = set()
    for path in _SCREENSHORT_DIR.glob("*.jpg"):
        try:
            stem = capture_stem(path)
            if stem in seen:
                continue
            seen.add(stem)
            ts_ms = int(stem.split("_", 1)[0])
            # Still inside the base window — always keep.
            if ts_ms >= flat_cutoff_ms:
                continue
            if not should_purge_screenshot(path, settings=settings, now_ms=now_ms):
                continue
            delete_screenshot_files(path, include_text=False)
        except ValueError:
            continue
        except Exception as e:
            print(f"Error purging expired screenshots: {e}")
    try:
        sweep_screenshot_sidecars(_SCREENSHORT_DIR)
    except Exception as e:
        print(f"Error sweeping screenshot sidecars: {e}")

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

        sweep_discarded_frames()

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
