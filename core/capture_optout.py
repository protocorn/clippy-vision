"""Detect windows that request not to be captured via OS-level signals.

This module checks for OS-provided opt-out mechanisms:
- Windows: SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE or WDA_MONITOR
- macOS: NSWindowSharingNone (kCGWindowSharingState == 0)
- Linux: no standardized per-window mechanism (documented)

Password managers often use these APIs. When detected, skip screenshot, OCR,
accessibility text, and clipboard capture for that moment.

``core.private_windows`` covers browser private-mode windows (Chrome Incognito,
Edge InPrivate, …). It is not a password-manager list, so this module keeps its
own exact-match names. User-toggled private apps live in
``core.privacy_settings`` and stay on that path.
"""

from __future__ import annotations

import sys
import threading
import time

try:
    from core.platform_support import IS_LINUX, IS_MACOS, IS_WINDOWS
except ImportError:
    from platform_support import IS_LINUX, IS_MACOS, IS_WINDOWS

# Display affinity can change at runtime (password managers toggle it on
# unlock) and HWNDs get reused, so cache entries expire quickly.
_cache_lock = threading.Lock()
_cache: dict[int, tuple[bool, float]] = {}
_CACHE_TTL_SECONDS = 2.0

WDA_NONE = 0x00
WDA_MONITOR = 0x01
WDA_EXCLUDEFROMCAPTURE = 0x11

# Exact names after _normalize_process_name (lowercase, no .exe/.app).
# Aliases map a known variant onto one of these keys.
PASSWORD_MANAGERS = frozenset(
    {
        "1password",
        "bitwarden",
        "dashlane",
        "keepass",
        "keepassxc",
        "lastpass",
        "keeper",
        "enpass",
        "nordpass",
        "roboform",
    }
)

PASSWORD_MANAGER_ALIASES = {
    "1password-browser": "1password",
    "1passwordnative": "1password",
    "1password native messaging host": "1password",
    "keepass2": "keepass",
    "keepersecurityapp": "keeper",
    "keeper password manager": "keeper",
}


def _normalize_process_name(process_name: str) -> str:
    """Lowercase executable/bundle name with .exe / .app stripped."""
    name = (process_name or "").lower().strip()
    for ext in (".exe", ".app"):
        if name.endswith(ext):
            name = name[: -len(ext)].strip()
            break
    return name


def _password_manager_key(process_name: str) -> str:
    normalized = _normalize_process_name(process_name)
    return PASSWORD_MANAGER_ALIASES.get(normalized, normalized)


def _is_private_process(process_name: str) -> bool:
    """True only for an exact password-manager name or listed alias."""
    return _password_manager_key(process_name) in PASSWORD_MANAGERS


def process_is_capture_opted_out(process_name: str) -> bool:
    """True when capture always skips this process (password managers).

    OS-level per-window opt-out cannot be reconstructed from stored events.
    This is the durable process-name signal that retrieval can still honor.
    """
    return _is_private_process(process_name)


def _read_window_display_affinity(hwnd: int) -> int | None:
    """Return GetWindowDisplayAffinity, or None when the call fails."""
    try:
        import ctypes
        from ctypes import wintypes

        affinity = wintypes.DWORD()
        result = ctypes.windll.user32.GetWindowDisplayAffinity(
            wintypes.HWND(hwnd),
            ctypes.byref(affinity),
        )
        if not result:
            return None
        return int(affinity.value)
    except Exception as exc:
        print(f"[capture_optout] Windows affinity check error: {exc}", file=sys.stderr)
        return None


def _windows_window_opted_out(hwnd: int) -> bool:
    """True when the window set WDA_EXCLUDEFROMCAPTURE or WDA_MONITOR.

    Fail-safe: return False on a missing handle or API error.
    """
    if not hwnd:
        return False
    affinity = _read_window_display_affinity(hwnd)
    if affinity is None:
        return False
    return affinity in (WDA_MONITOR, WDA_EXCLUDEFROMCAPTURE)


def _macos_copy_window_info() -> list | None:
    """Front-to-back on-screen window dicts from CGWindowList, or None."""
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
        )

        return CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
        )
    except Exception as exc:
        print(f"[capture_optout] macOS sharing state check error: {exc}", file=sys.stderr)
        return None


def _macos_window_opted_out(_bounds: tuple[int, int, int, int] | None = None) -> bool:
    """True when the foreground app's frontmost window is NSWindowSharingNone.

    Uses the first layer-0 window in the front-to-back list and then that
    window's ``kCGWindowOwnerPID``. A background layer-0 window that merely
    overlaps the front window must not trigger opt-out.
    """
    try:
        window_list = _macos_copy_window_info()
        if not window_list:
            return False

        frontmost_pid = None
        for window in window_list:
            if window.get("kCGWindowLayer", -1) == 0:
                frontmost_pid = window.get("kCGWindowOwnerPID")
                break
        if frontmost_pid is None:
            return False

        for window in window_list:
            if window.get("kCGWindowOwnerPID") != frontmost_pid:
                continue
            if window.get("kCGWindowLayer", -1) != 0:
                continue
            return window.get("kCGWindowSharingState", 1) == 0
        return False
    except Exception as exc:
        print(f"[capture_optout] macOS sharing state check error: {exc}", file=sys.stderr)
        return False


def _linux_window_opted_out() -> bool:
    """Linux has no standardized per-window capture opt-out.

    X11 has no "do not capture" property. Wayland screen-share consent is
    session-wide, not per-window. Process-name fallback covers password
    managers; this OS check always returns False.
    """
    return False


def _cached_windows_opted_out(hwnd: int) -> bool:
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(hwnd)
        if cached is not None:
            opted_out, stamped = cached
            if now - stamped < _CACHE_TTL_SECONDS:
                return opted_out

    opted_out = _windows_window_opted_out(hwnd)

    with _cache_lock:
        _cache[hwnd] = (opted_out, now)
        if len(_cache) > 256:
            cutoff = now - _CACHE_TTL_SECONDS
            for stale in [handle for handle, (_, stamped) in _cache.items() if stamped < cutoff]:
                _cache.pop(stale, None)
    return opted_out


def _mac_optout_bounds() -> tuple[int, int, int, int] | None:
    """Front-window bounds on macOS only. Never imported on the hot path elsewhere."""
    if not IS_MACOS:
        return None
    try:
        from core.platform_support import get_foreground_window_bounds
    except ImportError:
        from platform_support import get_foreground_window_bounds
    return get_foreground_window_bounds()


def window_opted_out(
    hwnd: int | None = None,
    process_name: str = "",
    bounds: tuple[int, int, int, int] | None = None,
) -> bool:
    """True when the foreground window asked not to be captured.

    Fail-safe: detection errors return False (allow capture).
    """
    if process_name and _is_private_process(process_name):
        return True

    if IS_WINDOWS and hwnd:
        return _cached_windows_opted_out(int(hwnd))
    if IS_MACOS:
        return _macos_window_opted_out(bounds)
    if IS_LINUX:
        return _linux_window_opted_out()
    return False


def foreground_should_skip_capture(
    hwnd: int | None = None,
    process_name: str = "",
) -> bool:
    """Capture-pipeline entry point. Safe on Windows and Linux.

    Looks up macOS bounds only when running on macOS so callers never import
    a mac-only helper themselves.
    """
    bounds = _mac_optout_bounds() if IS_MACOS else None
    return window_opted_out(hwnd, process_name, bounds)


def forget_optout_cache() -> None:
    with _cache_lock:
        _cache.clear()


def should_skip_capture(
    hwnd: int | None = None,
    process_name: str = "",
    bounds: tuple[int, int, int, int] | None = None,
) -> bool:
    """Alias for ``window_opted_out``."""
    return window_opted_out(hwnd, process_name, bounds)
