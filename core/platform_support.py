"""Small platform adapters used by the capture process.

The original capture loop was tightly coupled to Win32 APIs.  Keeping the
platform-specific work here lets the event pipeline stay the same on Windows,
macOS, and Linux while still allowing optional integrations where the host
doesn't provide them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Optional

try:
    from core.events import WindowMetadata
except ImportError:
    from events import WindowMetadata


PLATFORM = sys.platform
IS_WINDOWS = PLATFORM == "win32"
IS_MACOS = PLATFORM == "darwin"
IS_LINUX = PLATFORM.startswith("linux")

# AppleScript and Accessibility queries are relatively expensive, so the
# capture loop shares a short-lived front-window snapshot between callers.
_MAC_CACHE_TTL_SECONDS = 0.75
_mac_cache_lock = threading.Lock()
_mac_cache_at = 0.0
_mac_cache: tuple[WindowMetadata | None, tuple[int, int, int, int] | None] = (None, None)

# _windows_browser_url() walks the browser's UIA tree looking for the address
# bar — measured at multiple seconds per call on some machines/browsers. It
# used to only run once per capture; now capture_screenshot() *and* the async
# uia_worker's before/after identity checks all call get_window_metadata(),
# so an uncached call here is paid 3+ times per screenshot. Same short-lived
# cache strategy as macOS above.
_WIN_CACHE_TTL_SECONDS = 0.75
_win_cache_lock = threading.Lock()
_win_cache_at = 0.0
_win_cache: WindowMetadata | None = None

# History/AT-SPI URL lookup is cheap when cached and relatively expensive
# when Chrome's SQLite file has to be copied. Same short snapshot as Windows.
_LINUX_CACHE_TTL_SECONDS = 0.75
_linux_cache_lock = threading.Lock()
_linux_cache_at = 0.0
_linux_cache: WindowMetadata | None = None


def platform_label() -> str:
    if IS_MACOS:
        return "macOS"
    if IS_WINDOWS:
        return "Windows"
    return "Linux"


def lower_process_priority(*, background: bool = False) -> None:
    """Best-effort: yield OS CPU scheduling priority to the user's foreground apps.

    Capture and the API process do real background work (screenshots, OCR,
    UIA, LLM calls) that should not compete evenly with whatever the user is
    actively using, especially on a machine that's already under memory/CPU
    pressure. This only affects scheduling priority, not niceness toward
    memory — it won't fix RAM pressure, but it keeps foreground apps snappier
    while Clippy's own work still completes, just with lower scheduling
    priority.

    ``background=True`` asks for a further step down (idle-class), intended
    for a process that does no live/interactive work at all (e.g. batch OCR).
    Failures here are silently ignored — worst case we just run at the
    default priority, which is exactly today's behavior.
    """
    try:
        import psutil

        process = psutil.Process()
        if IS_WINDOWS:
            process.nice(
                psutil.IDLE_PRIORITY_CLASS if background else psutil.BELOW_NORMAL_PRIORITY_CLASS
            )
        else:
            # POSIX nice: higher value = lower priority (range roughly -20..19).
            # 15 is a firm but not maximal de-prioritization for background work;
            # 8 is a mild step down for the still-somewhat-live capture process.
            process.nice(15 if background else 8)
    except Exception:
        pass


def _run_command(args: list[str], timeout: float = 1.5) -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _windows_metadata() -> WindowMetadata | None:
    global _win_cache_at, _win_cache
    now = time.monotonic()
    with _win_cache_lock:
        if _win_cache is not None and now - _win_cache_at < _WIN_CACHE_TTL_SECONDS:
            return _win_cache

    result: WindowMetadata | None
    try:
        import psutil
        import uiautomation as auto
        import win32gui
        import win32process

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            result = None
        else:
            class_name = win32gui.GetClassName(hwnd)
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                process_name = psutil.Process(pid).name()
            except Exception:
                process_name = "unknown"
            from core.private_windows import window_is_private

            if window_is_private(hwnd, process_name):
                # The page title and address are the private session. Keep a
                # fixed label so events do not store what was on screen.
                title = "Private window"
                active_url = None
            else:
                title = win32gui.GetWindowText(hwnd) or ""
                active_url = _windows_browser_url(auto.WindowControl(Handle=hwnd), class_name)
            result = WindowMetadata(
                timestamp=time.time(),
                current_window_title=title,
                active_url=active_url,
                process_name=process_name,
            )
    except Exception:
        result = None

    with _win_cache_lock:
        _win_cache = result
        _win_cache_at = now
    return result


def get_idle_seconds() -> float | None:
    """Seconds since last keyboard/mouse input, or None if unavailable.

    Uses the OS last-input clock (HID), not Clippy's own event stream —
    so scrolling/mouse-only activity still counts even before we add a
    pynput mouse listener.
    """
    if IS_WINDOWS:
        return _windows_idle_seconds()
    if IS_MACOS:
        return _macos_idle_seconds()
    if IS_LINUX:
        return _linux_idle_seconds()
    return None


def _windows_idle_seconds() -> float | None:
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("dwTime", wintypes.DWORD),
            ]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        # GetTickCount wraps ~49 days; use tick difference carefully.
        tick = ctypes.windll.kernel32.GetTickCount()
        elapsed_ms = (tick - info.dwTime) & 0xFFFFFFFF
        return elapsed_ms / 1000.0
    except Exception:
        return None


def _macos_idle_seconds() -> float | None:
    try:
        from Quartz import (
            CGEventSourceSecondsSinceLastEventType,
            kCGAnyInputEventType,
            kCGEventSourceStateCombinedSessionState,
        )

        return float(
            CGEventSourceSecondsSinceLastEventType(
                kCGEventSourceStateCombinedSessionState,
                kCGAnyInputEventType,
            )
        )
    except Exception:
        return None


_linux_idle_warned = False


def _linux_idle_seconds() -> float | None:
    """Linux idle time via xprintidle or xdotool, returning None if unavailable."""
    global _linux_idle_warned
    try:
        # Try xprintidle first (returns milliseconds since last input)
        if shutil.which("xprintidle"):
            result = _run_command(["xprintidle"])
            if result:
                return int(result) / 1000.0
        # Fall back to xdotool
        if shutil.which("xdotool"):
            result = _run_command(["xdotool", "get_idle_time"])
            if result:
                return int(result) / 1000.0
    except (ValueError, OSError):
        pass
    if not _linux_idle_warned:
        _linux_idle_warned = True
        print("[platform_support] Linux idle detection unavailable (xprintidle or xdotool not found)", file=sys.stderr)
    return None


def _windows_browser_url(window: Any, class_name: str) -> str | None:
    """Read the address bar without making UI Automation a hard dependency elsewhere."""
    try:
        address = None
        if class_name == "Chrome_WidgetWin_1":
            address = window.EditControl(AutomationId="addressEditBox", searchDepth=15)
            if not address.Exists(0):
                address = window.EditControl(Name="Address and search bar", searchDepth=15)
        elif class_name == "MozillaWindowClass":
            address = window.EditControl(
                SubName="Search with Google or enter address", searchDepth=15
            )
            if not address.Exists(0):
                address = window.EditControl(SubName="Search or enter address", searchDepth=15)
        if address and address.Exists(0.35):
            return address.GetValuePattern().Value
    except Exception:
        pass
    return None


# macOS does not expose a Win32-style foreground-window handle. System Events
# provides the frontmost process, title, and logical bounds through AppleScript.
_MAC_FRONTMOST_SCRIPT = r'''
tell application "System Events"
    set frontProc to first application process whose frontmost is true
    set appName to name of frontProc
    set windowTitle to ""
    set boundsText to ""
    try
        set frontWindow to front window of frontProc
        set windowTitle to name of frontWindow
        set windowPosition to position of frontWindow
        set windowSize to size of frontWindow
        set boundsText to (item 1 of windowPosition as text) & "," & ¬
            (item 2 of windowPosition as text) & "," & ¬
            ((item 1 of windowPosition) + (item 1 of windowSize) as text) & "," & ¬
            ((item 2 of windowPosition) + (item 2 of windowSize) as text)
    end try
    return appName & linefeed & windowTitle & linefeed & boundsText
end tell
'''


_MAC_BROWSER_SCRIPTS = {
    "Google Chrome": 'tell application "Google Chrome" to URL of active tab of front window',
    "Chromium": 'tell application "Chromium" to URL of active tab of front window',
    "Brave Browser": 'tell application "Brave Browser" to URL of active tab of front window',
    "Microsoft Edge": 'tell application "Microsoft Edge" to URL of active tab of front window',
    "Arc": 'tell application "Arc" to URL of active tab of front window',
    "Safari": 'tell application "Safari" to URL of current tab of front window',
    # Firefox has no URL dictionary. Read the address bar the way Windows
    # reads it from UI Automation.
    "Firefox": r'''
tell application "System Events"
    tell process "Firefox"
        try
            return value of text field 1 of combo box 1 of group 1 of toolbar 1 of group 1 of front window
        end try
        try
            return value of combo box 1 of toolbar 1 of group 1 of front window
        end try
        return ""
    end tell
end tell
''',
}


def _mac_metadata() -> tuple[WindowMetadata | None, tuple[int, int, int, int] | None]:
    raw = _run_command(["osascript", "-e", _MAC_FRONTMOST_SCRIPT], timeout=2.0)
    if not raw:
        return None, None

    lines = raw.splitlines()
    process_name = (lines[0] if lines else "unknown").strip() or "unknown"
    title = (lines[1] if len(lines) > 1 else "").strip()
    bounds: tuple[int, int, int, int] | None = None
    if len(lines) > 2 and lines[2].strip():
        try:
            values = [int(float(x.strip())) for x in lines[2].split(",")]
            if len(values) == 4:
                bounds = tuple(values)
        except ValueError:
            pass

    from core.private_windows import window_is_private

    active_url = None
    if window_is_private(None, process_name):
        # The page title and address are the private session.
        title = "Private window"
    else:
        browser_script = _MAC_BROWSER_SCRIPTS.get(process_name)
        if browser_script:
            active_url = _run_command(["osascript", "-e", browser_script], timeout=1.5) or None

    return (
        WindowMetadata(
            timestamp=time.time(),
            current_window_title=title,
            active_url=active_url,
            process_name=process_name,
        ),
        bounds,
    )


_linux_xdotool_warned = False


def _linux_browser_url(process_name: str, title: str) -> str | None:
    """Read the active tab URL the way Windows reads the address bar.

    AT-SPI first (same control names as UIA), then the window title plus
    Chrome/Chromium/Firefox session or history data. Missing tools or
    profiles return None.
    """
    try:
        from core.linux_browser_url import resolve_active_url

        return resolve_active_url(process_name, title)
    except Exception:
        return None


def _linux_metadata() -> WindowMetadata | None:
    """Best-effort Linux window metadata via xdotool and psutil."""
    global _linux_xdotool_warned, _linux_cache_at, _linux_cache

    now = time.monotonic()
    with _linux_cache_lock:
        if _linux_cache is not None and now - _linux_cache_at < _LINUX_CACHE_TTL_SECONDS:
            return _linux_cache
    
    # If xdotool is not available, warn once and return empty metadata
    if not shutil.which("xdotool"):
        if not _linux_xdotool_warned:
            _linux_xdotool_warned = True
            print("[platform_support] xdotool not found; window metadata will be limited", file=sys.stderr)
        result = WindowMetadata(
            timestamp=time.time(),
            current_window_title="",
            active_url=None,
            process_name="unknown",
        )
        with _linux_cache_lock:
            _linux_cache = result
            _linux_cache_at = now
        return result
    
    title = _run_command(["xdotool", "getactivewindow", "getwindowname"])
    process_name = "unknown"
    
    # Get PID and resolve to process name
    if title:
        pid_str = _run_command(["xdotool", "getactivewindow", "getwindowpid"])
        if pid_str:
            try:
                import psutil
                pid = int(pid_str)
                process_name = psutil.Process(pid).name()
            except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
                process_name = "unknown"

    active_url = _linux_browser_url(process_name, title or "") if title else None
    result = WindowMetadata(
        timestamp=time.time(),
        current_window_title=title or "",
        active_url=active_url,
        process_name=process_name,
    )
    with _linux_cache_lock:
        _linux_cache = result
        _linux_cache_at = now
    return result


def get_window_metadata() -> WindowMetadata | None:
    """Return the frontmost app/window, with a short cache to avoid shell churn on macOS."""
    # Keep the capture pipeline platform-neutral; only this adapter knows how
    # to ask each operating system for its frontmost application.
    global _mac_cache_at, _mac_cache
    if IS_WINDOWS:
        return _windows_metadata()
    if IS_MACOS:
        now = time.monotonic()
        with _mac_cache_lock:
            if now - _mac_cache_at < _MAC_CACHE_TTL_SECONDS:
                return _mac_cache[0]
            _mac_cache = _mac_metadata()
            _mac_cache_at = now
            return _mac_cache[0]

    # Linux support is intentionally best-effort: xdotool is optional and the
    # rest of capture should continue even when a desktop lacks it. Browser
    # URLs come from AT-SPI or on-disk history when those are available.
    return _linux_metadata()


def get_foreground_window_bounds() -> tuple[int, int, int, int] | None:
    """Return macOS front-window bounds in logical screen coordinates."""
    # The redaction layer calls this after metadata so both values come from
    # the same cached AppleScript observation.
    if not IS_MACOS:
        return None
    get_window_metadata()
    with _mac_cache_lock:
        return _mac_cache[1]


def get_foreground_window_rect(hwnd: int | None = None) -> tuple[int, int, int, int] | None:
    """Cheap, UIA-free foreground window rectangle — instant fallback for OCR cropping.
    Unlike foreground_content_bounds() in accessibility_text.py, this never
    walks a UI Automation tree. It is safe to call on the capture hot path.
    """
    if IS_WINDOWS:
        try:
            import win32gui
            target = hwnd or win32gui.GetForegroundWindow()
            if not target:
                return None
            return win32gui.GetWindowRect(target)  # (left, top, right, bottom)
        except Exception:
            return None
    if IS_MACOS:
        return get_foreground_window_bounds()
    if IS_LINUX and shutil.which("xdotool"):
        raw = _run_command(
            ["xdotool", "getactivewindow", "getwindowgeometry", "--shell"],
            timeout=2.0,
        )
        if raw:
            values: dict[str, int] = {}
            for line in raw.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key in {"X", "Y", "WIDTH", "HEIGHT"}:
                    try:
                        values[key] = int(value)
                    except ValueError:
                        pass
            if all(k in values for k in ("X", "Y", "WIDTH", "HEIGHT")):
                left = values["X"]
                top = values["Y"]
                right = left + values["WIDTH"]
                bottom = top + values["HEIGHT"]
                if right > left and bottom > top:
                    return left, top, right, bottom
    return None


def window_key(metadata: WindowMetadata | None) -> str:
    if not metadata:
        return "unknown"
    return "\x1f".join(
        str(metadata.get(field) or "")
        for field in ("process_name", "current_window_title", "active_url")
    )


def get_clipboard_text() -> str | None:
    """Read the native clipboard, returning a bounded text payload."""
    try:
        if IS_WINDOWS:
            import win32clipboard

            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    value = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                    return str(value)[:2000]
                return None
            finally:
                win32clipboard.CloseClipboard()

        if IS_MACOS:
            command = ["pbpaste"]
        elif shutil.which("wl-paste"):
            command = ["wl-paste", "--no-newline"]
        elif shutil.which("xclip"):
            command = ["xclip", "-selection", "clipboard", "-o"]
        elif shutil.which("xsel"):
            command = ["xsel", "--clipboard", "--output"]
        else:
            return None
        value = _run_command(command, timeout=1.0)
        return value[:2000] if value else None
    except Exception:
        return None
