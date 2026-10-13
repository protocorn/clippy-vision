"""Detect a private Chrome, Edge, Brave, Opera, Vivaldi, or Firefox window.

On Windows the Chromium-family browsers (Chrome, Edge, Brave, Opera and
Vivaldi) are detected from the accessibility tree: the visible window title
drops the private-mode word once a page loads, or keeps it only in a form
that also appears on ordinary pages, but the browser's root pane keeps a
stable accessible name.

A private window ends with the brand and a parenthetical label, in whatever
language that label uses:

    Page title - Google Chrome (Incognito)
    Page title - Microsoft Edge (InPrivate)
    Page title - Brave (Private)

A normal window ends with the brand, or with the brand, a dash, and a
profile name. Parentheses in that profile name are not the private label.

    Page title - Google Chrome - Alex (Work)
    Page title - Microsoft Edge
    Page title - Brave

Chrome and Edge call the pane ``BrowserRootView``. Brave calls it
``BraveBrowserRootView``. The window class is ``Chrome_WidgetWin_1`` for
all three, so the class is not the signal.

Firefox is detected separately, from "Private Browsing" markers in the
accessibility tree. On macOS the front window's mode is read with AppleScript
(Chrome, Edge, Brave, Firefox and Opera; Vivaldi is not mapped). On Linux,
private-window detection is not implemented and this module always returns
False.
"""

from __future__ import annotations

import re
import threading

# A profile is " - Name" or " – Name". Text after the brand is allowed
# (a channel, or Edge's "Sleeping") until that dash.
_PROFILE_DASH = r"[ \u00a0][\u002d\u2013][ \u00a0]"


def _brand_private(brand: str) -> re.Pattern[str]:
    return re.compile(
        brand + r"(?:(?!" + _PROFILE_DASH + r").)* \([^)]+\)\s*$",
        re.IGNORECASE,
    )


# The space inside the brand can be a non-breaking space. Edge's visible
# title also inserts a zero-width space; the pane usually does not.
_CHROME_PRIVATE_NAME = _brand_private(r"Google[ \u00a0]Chrome")
_EDGE_PRIVATE_NAME = _brand_private(r"Microsoft\u200b?[ \u00a0]Edge")
_BRAVE_PRIVATE_NAME = _brand_private(r"Brave")

# Firefox private browsing: look for "Private Browsing" or "InPrivate Browsing" in window/element names
_FIREFOX_PRIVATE_PATTERN = re.compile(
    r"(private\s+browsing|inprivate\s+browsing|private\s+window|you'?ve\s+gone\s+incognito|navigation\s+priv[ée]e)",
    re.IGNORECASE,
)

# Opera and Vivaldi (Chromium-based) may use parenthetical pattern like Chrome
_OPERA_PRIVATE_NAME = _brand_private(r"Opera")
_VIVALDI_PRIVATE_NAME = _brand_private(r"Vivaldi")

# Keys come from core.process_names, so chrome.exe and Google Chrome share one.
_BY_KEY = {
    "chrome": _CHROME_PRIVATE_NAME,
    "edge": _EDGE_PRIVATE_NAME,
    "brave": _BRAVE_PRIVATE_NAME,
    "opera": _OPERA_PRIVATE_NAME,
    "vivaldi": _VIVALDI_PRIVATE_NAME,
}
# AppleScript application names. Their window mode is "incognito" on all three.
_MAC_APP = {
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "brave": "Brave Browser",
    "firefox": "Firefox",
    "opera": "Opera",
}
_ROOT_CLASSES = {"BrowserRootView", "BraveBrowserRootView"}
_MAX_CACHE = 64

_lock = threading.Lock()
_cache: dict[int, bool] = {}


def _pattern_for(process_name: str):
    from core.process_names import process_key

    return _BY_KEY.get(process_key(process_name))


def _is_firefox(process_name: str) -> bool:
    """True when this is a Firefox process."""
    from core.process_names import process_key
    return process_key(process_name) == "firefox"


def root_name_is_private(name: str, process_name: str) -> bool:
    """True when this browser's root-pane name marks a private window."""
    # Firefox uses a different pattern (not parenthetical brand)
    if _is_firefox(process_name):
        return _FIREFOX_PRIVATE_PATTERN.search(str(name or "").strip()) is not None
    
    pattern = _pattern_for(process_name)
    if pattern is None:
        return False
    return pattern.search(str(name or "").strip()) is not None


def chrome_root_name_is_private(name: str) -> bool:
    """True when a BrowserRootView name is a private Chrome window."""
    return root_name_is_private(name, "chrome.exe")


def _class_name(control) -> str:
    try:
        return str(getattr(control, "ClassName", "") or "")
    except Exception:
        return ""


def _control_name(control) -> str:
    try:
        return str(getattr(control, "Name", "") or "")
    except Exception:
        return ""


def control_is_private(control, process_name: str) -> bool | None:
    """Read the window control. None when the tree has no root pane yet.

    A missing pane is not a normal window. The caller retries next time
    instead of remembering a guess.
    
    For Firefox, checks window and child element names for "Private Browsing" markers.
    For Chromium-based browsers, checks BrowserRootView for parenthetical private label.
    
    This function works with both real Windows controls and mock objects for testing.
    """
    if control is None:
        return None
    
    # Firefox: check window name and walk shallow tree for "Private Browsing" markers
    if _is_firefox(process_name):
        try:
            window_name = _control_name(control)
            if _FIREFOX_PRIVATE_PATTERN.search(window_name):
                return True
            # Check children for private browsing markers
            children = list(control.GetChildren())
            for child in children[:10]:  # Check first 10 children only
                child_name = _control_name(child)
                if _FIREFOX_PRIVATE_PATTERN.search(child_name):
                    return True
        except Exception:
            pass
        return None
    
    # Chromium-based browsers (Chrome, Edge, Brave, Opera, Vivaldi)
    pattern = _pattern_for(process_name)
    if pattern is None:
        return None
    try:
        children = list(control.GetChildren())
    except Exception:
        return None
    for child in children:
        if _class_name(child) in _ROOT_CLASSES:
            return pattern.search(_control_name(child).strip()) is not None
        try:
            grandchildren = list(child.GetChildren())
        except Exception:
            continue
        for grand in grandchildren:
            if _class_name(grand) in _ROOT_CLASSES:
                return pattern.search(_control_name(grand).strip()) is not None
    return None


def chrome_control_is_private(control) -> bool | None:
    """Chrome entry point kept for callers that already know the process."""
    return control_is_private(control, "chrome.exe")


def forget_private_windows() -> None:
    with _lock:
        _cache.clear()


def mode_reply_is_private(reply: str) -> bool:
    """True when AppleScript reported a Chromium or Firefox private window."""
    token = (reply or "").strip().lower()
    return token in {"incognito", "inprivate", "private"}


def parse_private_bounds(reply: str) -> list[tuple[int, int, int, int]]:
    """Parse ``left,top,right,bottom`` lines from the private-window script."""
    found: list[tuple[int, int, int, int]] = []
    for line in (reply or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            left, top, right, bottom = (int(float(part)) for part in parts)
        except ValueError:
            continue
        if right - left < 4 or bottom - top < 4:
            continue
        found.append((left, top, right, bottom))
    return found


def bounds_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """True when most of the smaller rectangle sits inside the other."""
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    if right <= left or bottom <= top:
        return False
    shared = (right - left) * (bottom - top)
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return smaller > 0 and shared / smaller >= 0.6


def _redaction_enabled() -> bool:
    from core.privacy_settings import private_browsing_redaction_enabled

    return private_browsing_redaction_enabled()


def mac_private_bounds(process_name: str) -> list[tuple[int, int, int, int]]:
    """Rectangles of private Chrome, Edge, or Brave windows. Empty off macOS."""
    from core.platform_support import IS_MACOS, _run_command

    if not _redaction_enabled():
        return []
    app = _MAC_APP.get(_pattern_key(process_name))
    if not IS_MACOS or not app:
        return []
    script = (
        f'tell application "{app}"\n'
        "set out to \"\"\n"
        "repeat with w in windows\n"
        "try\n"
        'if mode of w is "incognito" or mode of w is "inprivate" then\n'
        "set b to bounds of w\n"
        'set out to out & (item 1 of b as text) & "," & (item 2 of b as text) & "," & (item 3 of b as text) & "," & (item 4 of b as text) & linefeed\n'
        "end if\n"
        "end try\n"
        "end repeat\n"
        "return out\n"
        "end tell"
    )
    return parse_private_bounds(_run_command(["osascript", "-e", script], timeout=2.0))


def _pattern_key(process_name: str) -> str:
    from core.process_names import process_key

    return process_key(process_name)


def _mac_front_is_private(process_name: str) -> bool:
    from core.platform_support import IS_MACOS, _run_command

    app = _MAC_APP.get(_pattern_key(process_name))
    if not IS_MACOS or not app:
        return False
    script = f'tell application "{app}" to get mode of front window'
    return mode_reply_is_private(_run_command(["osascript", "-e", script], timeout=1.5))


def window_is_private(hwnd: int | None, process_name: str) -> bool:
    """True for a Chrome, Edge, Brave, Firefox, Opera, or Vivaldi window marked private.

    On Windows the answer is remembered per window handle. The check walks the
    accessibility tree (two nodes deep for Chromium, shallow for Firefox), so later
    captures do not re-walk the page. On macOS the browser reports the front window's
    mode, which is ``incognito`` for Chromium and ``private`` for Firefox.
    On Linux, always returns False (browser private mode detection not yet implemented).
    """
    if not _is_firefox(process_name) and _pattern_for(process_name) is None:
        return False
    if not _redaction_enabled():
        return False
    from core.platform_support import IS_MACOS, IS_LINUX

    if IS_LINUX:
        return False
    if IS_MACOS:
        return _mac_front_is_private(process_name)
    if not hwnd:
        return False
    with _lock:
        cached = _cache.get(int(hwnd))
    if cached is not None:
        return cached
    try:
        import uiautomation as auto
    except ImportError:
        return False
    try:
        with auto.UIAutomationInitializerInThread():
            answer = control_is_private(auto.ControlFromHandle(int(hwnd)), process_name)
    except Exception:
        return False
    if answer is None:
        return False
    with _lock:
        _cache[int(hwnd)] = answer
        while len(_cache) > _MAX_CACHE:
            _cache.pop(next(iter(_cache)))
    return answer


def chrome_window_is_private(hwnd: int | None, process_name: str = "chrome.exe") -> bool:
    """True for a Chrome window. Other processes are ignored."""
    if (process_name or "").lower() not in {"", "chrome.exe"}:
        return False
    return window_is_private(hwnd, "chrome.exe")
