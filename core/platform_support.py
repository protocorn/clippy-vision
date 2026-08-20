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

# AppleScript and Accessibility queries are relatively expensive, so the
# capture loop shares a short-lived front-window snapshot between callers.
_MAC_CACHE_TTL_SECONDS = 0.75
_mac_cache_lock = threading.Lock()
_mac_cache_at = 0.0
_mac_cache: tuple[WindowMetadata | None, tuple[int, int, int, int] | None] = (
    None,
    None,
)


def platform_label() -> str:
    if IS_MACOS:
        return "macOS"
    if IS_WINDOWS:
        return "Windows"
    return "Linux"


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
    try:
        import psutil
        import uiautomation as auto
        import win32gui
        import win32process

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        class_name = win32gui.GetClassName(hwnd)
        active_url = _windows_browser_url(auto.WindowControl(Handle=hwnd), class_name)
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process_name = psutil.Process(pid).name()
        except Exception:
            process_name = "unknown"
        return WindowMetadata(
            timestamp=time.time(),
            current_window_title=win32gui.GetWindowText(hwnd) or "",
            active_url=active_url,
            process_name=process_name,
        )
    except Exception:
        return None


def _windows_browser_url(window: Any, class_name: str) -> str | None:
    """Read the address bar without making UI Automation a hard dependency elsewhere."""
    try:
        address = None
        if class_name == "Chrome_WidgetWin_1":
            address = window.EditControl(AutomationId="addressEditBox", searchDepth=15)
            if not address.Exists(0):
                address = window.EditControl(
                    Name="Address and search bar", searchDepth=15
                )
        elif class_name == "MozillaWindowClass":
            address = window.EditControl(
                SubName="Search with Google or enter address", searchDepth=15
            )
            if not address.Exists(0):
                address = window.EditControl(
                    SubName="Search or enter address", searchDepth=15
                )
        if address and address.Exists(0.35):
            return address.GetValuePattern().Value
    except Exception:
        pass
    return None


# macOS does not expose a Win32-style foreground-window handle. System Events
# provides the frontmost process, title, and logical bounds through AppleScript.
_MAC_FRONTMOST_SCRIPT = r"""
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
"""


_MAC_BROWSER_SCRIPTS = {
    "Google Chrome": 'tell application "Google Chrome" to URL of active tab of front window',
    "Chromium": 'tell application "Chromium" to URL of active tab of front window',
    "Brave Browser": 'tell application "Brave Browser" to URL of active tab of front window',
    "Microsoft Edge": 'tell application "Microsoft Edge" to URL of active tab of front window',
    "Arc": 'tell application "Arc" to URL of active tab of front window',
    "Safari": 'tell application "Safari" to URL of current tab of front window',
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

    active_url = None
    browser_script = _MAC_BROWSER_SCRIPTS.get(process_name)
    if browser_script:
        active_url = (
            _run_command(["osascript", "-e", browser_script], timeout=1.5) or None
        )

    return (
        WindowMetadata(
            timestamp=time.time(),
            current_window_title=title,
            active_url=active_url,
            process_name=process_name,
        ),
        bounds,
    )


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
    # rest of capture should continue even when a desktop lacks it.
    title = _run_command(["xdotool", "getactivewindow", "getwindowname"])
    process_name = "unknown"
    if title:
        process_name = (
            _run_command(["xdotool", "getactivewindow", "getwindowpid"]) or "unknown"
        )
    return WindowMetadata(
        timestamp=time.time(),
        current_window_title=title,
        active_url=None,
        process_name=process_name,
    )


def get_foreground_window_bounds() -> tuple[int, int, int, int] | None:
    """Return macOS front-window bounds in logical screen coordinates."""
    # The redaction layer calls this after metadata so both values come from
    # the same cached AppleScript observation.
    if not IS_MACOS:
        return None
    get_window_metadata()
    with _mac_cache_lock:
        return _mac_cache[1]


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
                if win32clipboard.IsClipboardFormatAvailable(
                    win32clipboard.CF_UNICODETEXT
                ):
                    value = win32clipboard.GetClipboardData(
                        win32clipboard.CF_UNICODETEXT
                    )
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
