"""macOS Accessibility queries for the same capture privacy as Windows.

Windows uses UI Automation. This module lists on-screen windows and the
front window's fields through AXUIElement, and falls back to System Events
when the AX frameworks are not installed. Callers on other platforms get
None and keep their own path.
"""

from __future__ import annotations

import subprocess

try:
    from core.platform_support import IS_MACOS, get_foreground_window_bounds
except ImportError:
    from platform_support import IS_MACOS, get_foreground_window_bounds

_MAX_NODES = 400
_MAX_DEPTH = 14
_VALUE_CHARS = 4000
_SKIP_ROLES = {
    "AXMenu",
    "AXMenuBar",
    "AXMenuItem",
    "AXMenuBarItem",
}
_FIELD_ROLES = {"AXTextField", "AXTextArea", "AXSecureTextField", "AXComboBox", "AXStaticText"}
_CONTENT_ROLES = {"AXWebArea", "AXTextArea", "AXScrollArea"}
_DENIED = -25211


def redaction_from_nodes(nodes: list[dict]) -> dict:
    """Turn field records into the rectangles and scrubbed text capture stores."""
    try:
        from core.secret_fields import should_redact_edit
        from core.secret_patterns import (
            is_secret_name,
            paired_row_secrets,
            redact_field_values,
            redact_secrets,
            secret_values,
        )
    except ImportError:
        from secret_fields import should_redact_edit
        from secret_patterns import (
            is_secret_name,
            paired_row_secrets,
            redact_field_values,
            redact_secrets,
            secret_values,
        )

    edit_rects: list[tuple[int, int, int, int]] = []
    edit_values: list[str] = []
    secret_rects: list[tuple[int, int, int, int]] = []
    row_nodes: list[dict] = []
    chunks: list[str] = []
    seen: set[tuple[int, int, int, int]] = set()

    for node in nodes:
        raw_bounds = node.get("bounds")
        if not raw_bounds or len(raw_bounds) != 4:
            continue
        bounds = tuple(int(v) for v in raw_bounds)
        role = str(node.get("role") or "")
        name = " ".join(str(node.get("name") or "").split())
        value = str(node.get("value") or "")
        height = bounds[3] - bounds[1]
        is_password = role == "AXSecureTextField" or bool(node.get("password"))
        if is_password or (role in {"AXTextField", "AXComboBox"} and should_redact_edit(name, height)):
            if bounds not in seen:
                seen.add(bounds)
                edit_rects.append(bounds)
                edit_values.append("" if is_password else value.strip()[:200])
        if role == "AXTextArea" and value.strip():
            chunks.append(value)
            if secret_values(value):
                secret_rects.append(bounds)
        label = name[:180] if name and "\n" not in name else ""
        if label:
            row_nodes.append({"text": label, "bounds": bounds})
            if is_secret_name(label) and not is_password:
                field_value = value.strip()
                if field_value and field_value != label and "\n" not in field_value:
                    row_nodes.append({"text": field_value[:180], "bounds": bounds})
        elif role == "AXStaticText":
            text = " ".join(value.split())
            if text and len(text) <= 180:
                row_nodes.append({"text": text, "bounds": bounds})

    paired = paired_row_secrets(row_nodes)
    secret_rects.extend(item["bounds"] for item in paired)
    tokens = edit_values + [item["text"] for item in paired]
    return {
        "edit_rects": edit_rects,
        "secret_rects": secret_rects,
        "redacted_text": redact_secrets(redact_field_values("\n".join(chunks), tokens)),
    }


def parse_window_dump(raw: str) -> list[dict]:
    """Parse System Events window lines: process, title, x, y, width, height."""
    windows: list[dict] = []
    for line in (raw or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        process_name, title, x, y, width, height = parts
        try:
            left = int(float(x))
            top = int(float(y))
            wide = int(float(width))
            high = int(float(height))
        except ValueError:
            continue
        if wide < 4 or high < 4 or not process_name.strip():
            continue
        windows.append(
            {
                "process_name": process_name.strip(),
                "title": title.strip(),
                "bounds": (left, top, left + wide, top + high),
            }
        )
    return windows


def parse_field_dump(raw: str) -> list[dict]:
    """Parse System Events field lines: role, name, value, x, y, width, height."""
    nodes: list[dict] = []
    for line in (raw or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        role, name, value, x, y, width, height = parts
        try:
            left = int(float(x))
            top = int(float(y))
            wide = int(float(width))
            high = int(float(height))
        except ValueError:
            continue
        if wide < 4 or high < 4:
            continue
        nodes.append(
            {
                "role": role.strip(),
                "name": name.replace("\\n", " ").strip(),
                "value": "" if role.strip() == "AXSecureTextField" else value.replace("\\n", "\n"),
                "bounds": (left, top, left + wide, top + high),
            }
        )
    return nodes


def list_visible_windows() -> list[dict] | None:
    """On-screen windows. None means Accessibility could not be queried."""
    if not IS_MACOS:
        return None
    listed, status = _ax_windows()
    if status == "ok":
        return listed
    if status == "denied":
        return None
    return _script_windows()


def front_field_nodes() -> list[dict] | None:
    """Fields in the front window. None means the walk did not finish."""
    if not IS_MACOS:
        return None
    nodes, status = _ax_fields()
    if status == "ok":
        return nodes
    if status == "denied":
        return None
    return _script_fields()


def front_content_bounds() -> tuple[int, int, int, int] | None:
    """Largest web or text region in the front window, else the window itself."""
    if not IS_MACOS:
        return None
    bounds, status = _ax_content_bounds()
    if status == "ok" and bounds is not None:
        return bounds
    if status == "denied":
        return None
    return get_foreground_window_bounds()


def _run_osascript(script: str, timeout: float = 1.2) -> str | None:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def _script_windows() -> list[dict] | None:
    raw = _run_osascript(_WINDOW_SCRIPT, timeout=1.5)
    if raw is None:
        return None
    return parse_window_dump(raw)


def _script_fields() -> list[dict] | None:
    raw = _run_osascript(_FIELD_SCRIPT, timeout=1.4)
    if raw is None:
        return None
    return parse_field_dump(raw)


def _ax_copy(element, attribute: str):
    try:
        from ApplicationServices import AXUIElementCopyAttributeValue
    except Exception:
        return None, "unavailable"
    try:
        result = AXUIElementCopyAttributeValue(element, attribute, None)
    except Exception:
        return None, "error"
    if isinstance(result, tuple):
        err = int(result[0] or 0)
        value = result[-1] if len(result) > 1 else None
        if err == _DENIED:
            return None, "denied"
        if err:
            return None, "error"
        return value, "ok"
    return result, "ok"


def _ax_pair(value, kind: str) -> tuple[float, float] | None:
    if value is None:
        return None
    x = getattr(value, "x", None)
    y = getattr(value, "y", None)
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return float(x), float(y)
    width = getattr(value, "width", None)
    height = getattr(value, "height", None)
    if kind == "size" and isinstance(width, (int, float)) and isinstance(height, (int, float)):
        return float(width), float(height)
    try:
        import ctypes

        from Quartz import AXValueGetType, AXValueGetValue, CGPoint, CGSize, kAXValueCGPointType, kAXValueCGSizeType

        expected = kAXValueCGPointType if kind == "point" else kAXValueCGSizeType
        if AXValueGetType(value) != expected:
            return None
        holder = CGPoint() if kind == "point" else CGSize()
        if not AXValueGetValue(value, expected, ctypes.byref(holder)):
            return None
        if kind == "point":
            return float(holder.x), float(holder.y)
        return float(holder.width), float(holder.height)
    except Exception:
        return None


def _bounds_of(element) -> tuple[int, int, int, int] | None:
    pos, pos_status = _ax_copy(element, "AXPosition")
    size, size_status = _ax_copy(element, "AXSize")
    if pos_status == "denied" or size_status == "denied":
        return None
    point = _ax_pair(pos, "point")
    dimensions = _ax_pair(size, "size")
    if point is None or dimensions is None:
        return None
    left, top = point
    width, height = dimensions
    if width < 4 or height < 4:
        return None
    return (int(left), int(top), int(left + width), int(top + height))


def _text_attr(element, attribute: str, limit: int) -> str:
    value, status = _ax_copy(element, attribute)
    if status != "ok" or value is None:
        return ""
    text = str(value).replace("\t", " ")
    return text[:limit]


def _ax_app(pid: int):
    try:
        from ApplicationServices import AXUIElementCreateApplication
    except Exception:
        return None, "unavailable"
    try:
        return AXUIElementCreateApplication(pid), "ok"
    except Exception:
        return None, "unavailable"


def _ax_windows() -> tuple[list[dict] | None, str]:
    try:
        from AppKit import NSWorkspace
    except Exception:
        return None, "unavailable"
    try:
        apps = list(NSWorkspace.sharedWorkspace().runningApplications())
    except Exception:
        return None, "unavailable"
    windows: list[dict] = []
    saw_tree = False
    for app in apps:
        try:
            if int(app.activationPolicy()) != 0 or bool(app.isHidden()):
                continue
            pid = int(app.processIdentifier())
            process_name = str(app.localizedName() or "")
        except Exception:
            continue
        element, status = _ax_app(pid)
        if status != "ok" or element is None:
            if status == "denied":
                return None, "denied"
            continue
        children, child_status = _ax_copy(element, "AXWindows")
        if child_status == "denied":
            return None, "denied"
        if child_status == "unavailable":
            return None, "unavailable"
        if child_status != "ok" or not children:
            continue
        saw_tree = True
        for window in list(children):
            minimized, _status = _ax_copy(window, "AXMinimized")
            if minimized:
                continue
            bounds = _bounds_of(window)
            if bounds is None:
                continue
            title, _title_status = _ax_copy(window, "AXTitle")
            windows.append(
                {
                    "process_name": process_name,
                    "title": str(title or ""),
                    "bounds": bounds,
                }
            )
    if not saw_tree and not windows:
        # No AX tree at all usually means the frameworks or permission are missing.
        # An empty desktop still returns ok once any app answered.
        probe, probe_status = _ax_app(1)
        if probe_status == "unavailable":
            return None, "unavailable"
        del probe
    return windows, "ok"


def _front_application():
    try:
        from AppKit import NSWorkspace
    except Exception:
        return None, "unavailable"
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        return None, "error"
    if app is None:
        return None, "error"
    try:
        return (int(app.processIdentifier()), str(app.localizedName() or "")), "ok"
    except Exception:
        return None, "error"


def _walk_fields(root) -> tuple[list[dict], str]:
    nodes: list[dict] = []
    queue = [(root, 0)]
    visited = 0
    while queue and visited < _MAX_NODES:
        element, depth = queue.pop(0)
        visited += 1
        role, status = _ax_copy(element, "AXRole")
        if status == "denied":
            return [], "denied"
        if status == "unavailable":
            return [], "unavailable"
        role_name = str(role or "")
        if role_name in _SKIP_ROLES:
            continue
        if role_name in _FIELD_ROLES or role_name in _CONTENT_ROLES:
            bounds = _bounds_of(element)
            if bounds is not None:
                value = ""
                name = ""
                if role_name in _FIELD_ROLES and role_name != "AXSecureTextField":
                    value = _text_attr(element, "AXValue", _VALUE_CHARS)
                if role_name in _FIELD_ROLES:
                    name = _text_attr(element, "AXTitle", 180) or _text_attr(element, "AXDescription", 180)
                nodes.append(
                    {
                        "role": role_name,
                        "name": name,
                        "value": value,
                        "bounds": bounds,
                    }
                )
        if depth >= _MAX_DEPTH:
            continue
        children, child_status = _ax_copy(element, "AXChildren")
        if child_status == "denied":
            return [], "denied"
        if child_status == "ok" and children:
            queue.extend((child, depth + 1) for child in list(children))
    return nodes, "ok"


def _ax_fields() -> tuple[list[dict] | None, str]:
    front, status = _front_application()
    if status != "ok" or front is None:
        return None, status
    element, app_status = _ax_app(front[0])
    if app_status != "ok" or element is None:
        return None, app_status
    window, window_status = _ax_copy(element, "AXFocusedWindow")
    if window_status == "denied":
        return None, "denied"
    if window_status != "ok" or window is None:
        windows, windows_status = _ax_copy(element, "AXWindows")
        if windows_status != "ok" or not windows:
            return None, windows_status if windows_status != "ok" else "error"
        window = list(windows)[0]
    return _walk_fields(window)


def _ax_content_bounds() -> tuple[tuple[int, int, int, int] | None, str]:
    nodes, status = _ax_fields()
    if status != "ok" or nodes is None:
        return None, status
    best = None
    best_rank = -1
    for node in nodes:
        role = node.get("role")
        if role not in _CONTENT_ROLES:
            continue
        bounds = node.get("bounds")
        if not bounds:
            continue
        area = max(0, bounds[2] - bounds[0]) * max(0, bounds[3] - bounds[1])
        # Prefer the page or editor over a scroll view that includes the toolbar.
        rank = area + (10**12 if role in {"AXWebArea", "AXTextArea"} else 0)
        if rank > best_rank:
            best = bounds
            best_rank = rank
    return best, "ok"


_WINDOW_SCRIPT = r'''
tell application "System Events"
    set outputText to ""
    repeat with proc in (every application process whose background only is false)
        set procName to name of proc
        try
            repeat with w in (every window of proc)
                try
                    set minimized to false
                    try
                        set minimized to value of attribute "AXMinimized" of w
                    end try
                    if minimized is false then
                        set wName to ""
                        try
                            set wName to name of w
                        end try
                        set wPos to position of w
                        set wSize to size of w
                        set outputText to outputText & procName & tab & wName & tab & (item 1 of wPos as integer as text) & tab & (item 2 of wPos as integer as text) & tab & (item 1 of wSize as integer as text) & tab & (item 2 of wSize as integer as text) & linefeed
                    end if
                end try
            end repeat
        end try
    end repeat
    return outputText
end tell
'''

_FIELD_SCRIPT = r'''
on flattenText(rawText)
    set rawText to rawText as text
    set oldDelims to AppleScript's text item delimiters
    set AppleScript's text item delimiters to {tab, linefeed, return}
    set parts to text items of rawText
    set AppleScript's text item delimiters to " "
    set flattened to parts as text
    set AppleScript's text item delimiters to oldDelims
    if length of flattened > 500 then set flattened to text 1 thru 500 of flattened
    return flattened
end flattenText

tell application "System Events"
    set frontProc to first application process whose frontmost is true
    set outputText to ""
    set seenCount to 0
    try
        set uiItems to entire contents of front window of frontProc
        repeat with uiItem in uiItems
            if seenCount >= 250 then exit repeat
            try
                set itemRole to role of uiItem
                if itemRole is in {"AXTextField", "AXTextArea", "AXSecureTextField", "AXComboBox", "AXStaticText"} then
                    set itemName to ""
                    set itemValue to ""
                    try
                        set itemName to name of uiItem as text
                    end try
                    if itemRole is not "AXSecureTextField" then
                        try
                            set itemValue to value of uiItem as text
                        end try
                    end if
                    set itemPos to position of uiItem
                    set itemSize to size of uiItem
                    tell me to set itemName to flattenText(itemName)
                    tell me to set itemValue to flattenText(itemValue)
                    set outputText to outputText & itemRole & tab & itemName & tab & itemValue & tab & (item 1 of itemPos as integer as text) & tab & (item 2 of itemPos as integer as text) & tab & (item 1 of itemSize as integer as text) & tab & (item 2 of itemSize as integer as text) & linefeed
                    set seenCount to seenCount + 1
                end if
            end try
        end repeat
    end try
    return outputText
end tell
'''
