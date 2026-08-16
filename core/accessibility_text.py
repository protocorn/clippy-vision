from __future__ import annotations

import re
from collections import deque

from core.platform_support import IS_MACOS, IS_WINDOWS, _run_command

MAX_TEXT_CHARS = 4000
MIN_USEFUL_CHARS = 40
MAX_UI_NODES = 250
MAX_UI_DEPTH = 10
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")

# Window-manager / browser chrome that still leaks as Name strings.
UI_CHROME_LINES = {
    "minimize",
    "maximize",
    "restore",
    "close",
    "chrome legacy window",
    "close find bar",
    "find in page",
    "previous",
    "next",
    "back",
    "forward",
    "reload",
    "home",
    "bookmarks",
    "new tab",
    "tab search",
    "collapse tabs",
    "address and search bar",
    "view site information",
    "hidden toolbar buttons",
    "infobar container",
    "side panel resize handle (draggable)",
    "vertical tab strip resize handle (draggable)",
    "open tab in split view",
    "control your music, videos, and more",
    "open gemini in chrome",
    "to open gemini in chrome, press alt+g",
    "separator",
    "apps",
    "managed bookmarks",
    "saved tab groups",
    "menu containing hidden bookmarks",
    "all bookmarks",
    "clear input",
    "has access to this site",
    "wants access to this site",
    "skip to content",
    "skip navigation",
    "tooltip",
}

# Prefer these control types when collecting content.
_CONTENT_CONTROL_TYPES = {
    "DocumentControl",
    "EditControl",
    "TextControl",
    "ListItemControl",
    "DataItemControl",
    "HyperlinkControl",
    "GroupControl",  # often wraps labeled content in web apps
}

# Never treat these as page/editor content (browser/app chrome).
_CHROME_CONTROL_TYPES = {
    "ButtonControl",
    "ToolBarControl",
    "TabControl",
    "TabItemControl",
    "TitleBarControl",
    "MenuBarControl",
    "MenuControl",
    "MenuItemControl",
    "SeparatorControl",
    "ThumbControl",
    "ScrollBarControl",
    "ProgressBarControl",
    "SliderControl",
    "SpinnerControl",
    "SplitButtonControl",
    "ToolTipControl",
    "StatusBarControl",
    "ImageControl",
}

# Sibling panes/groups we compete between for "main content".
_REGION_CONTROL_TYPES = {
    "PaneControl",
    "GroupControl",
    "DocumentControl",
    "CustomControl",
    "ListControl",
    "TreeControl",
    "EditControl",
    "TableControl",
    "DataGridControl",
}

MIN_REGION_AREA = 40_000
_MAX_REGION_EXPAND_DEPTH = 4


def strip_ui_chrome(text: str) -> str:
    """Drop known window-chrome lines; keep real page/app content."""
    lines = []
    for raw in str(text or "").splitlines():
        line = _SPACE_RE.sub(" ", raw).strip()
        if not line:
            continue
        if line.casefold() in UI_CHROME_LINES:
            continue
        # Drop private-use / object-replacement glyphs Chrome injects as icons.
        if all(ord(ch) >= 0xE000 or ch in {"\ufffc", "\ufffd", " "} for ch in line):
            continue
        lines.append(line)
    return "\n".join(lines)


def normalize_accessibility_text(*values: object) -> str:
    seen = set()
    lines = []
    for value in values:
        for raw_line in str(value or "").splitlines():
            line = _SPACE_RE.sub(" ", raw_line).strip()
            key = line.casefold()
            if len(line) < 2 or key in seen or key in UI_CHROME_LINES:
                continue
            if all(ord(ch) >= 0xE000 or ch in {"\ufffc", "\ufffd", " "} for ch in line):
                continue
            seen.add(key)
            lines.append(line)
    return "\n".join(lines)[:MAX_TEXT_CHARS]


def _nonempty_lines(text: str) -> list[str]:
    return [line for line in str(text or "").splitlines() if line.strip()]


def _avg_line_length(text: str) -> float:
    lines = _nonempty_lines(text)
    if not lines:
        return 0.0
    return sum(len(line) for line in lines) / len(lines)


def looks_like_nav_soup(text: str) -> bool:
    """True when text is mostly short nav/sidebar labels, not prose."""
    lines = _nonempty_lines(strip_ui_chrome(text))
    if len(lines) < 6:
        return False
    avg = sum(len(line) for line in lines) / len(lines)
    short = sum(1 for line in lines if len(line.split()) <= 4)
    return avg < 28 and (short / len(lines)) >= 0.7


_EDITOR_STUB_MARKERS = (
    "the editor is not accessible at this time",
    "to enable screen reader optimized mode",
)


def is_useful_accessibility_text(text: str) -> bool:
    filtered = strip_ui_chrome(text)
    if looks_like_nav_soup(filtered):
        return False
    folded = filtered.casefold()
    if any(marker in folded for marker in _EDITOR_STUB_MARKERS):
        return False
    compact = "".join(character for character in filtered if character.isalnum())
    return len(compact) >= MIN_USEFUL_CHARS and len(filtered.split()) >= 4


def _control_area(control) -> int:
    try:
        rect = control.BoundingRectangle
        width = max(0, int(rect.right) - int(rect.left))
        height = max(0, int(rect.bottom) - int(rect.top))
        return width * height
    except Exception:
        return 0


def _control_type_name(control) -> str:
    try:
        return str(getattr(control, "ControlTypeName", "") or "")
    except Exception:
        return ""


def _is_content_element(control) -> bool:
    try:
        return bool(getattr(control, "IsContentElement", False))
    except Exception:
        return False


def _is_password(control) -> bool:
    try:
        return bool(getattr(control, "IsPassword", False) or getattr(control, "IsPasswordProperty", False))
    except Exception:
        return False


def _text_from_control(control) -> str:
    """Prefer TextPattern document text, then Value, then Name for content types."""
    if control is None or _is_password(control):
        return ""
    chunks: list[str] = []

    try:
        pattern = control.GetTextPattern()
        if pattern is not None and getattr(pattern, "DocumentRange", None) is not None:
            text = pattern.DocumentRange.GetText(MAX_TEXT_CHARS) or ""
            if text.strip():
                chunks.append(text)
    except Exception:
        pass

    try:
        pattern = control.GetValuePattern()
        value = "" if pattern is None else (pattern.Value or "")
        # Skip bare URLs as primary "content" — keep them only if nothing else exists.
        if value.strip() and not (
            value.strip().startswith("http://") or value.strip().startswith("https://")
        ):
            chunks.append(value)
        elif value.strip() and not chunks:
            chunks.append(value)
    except Exception:
        pass

    if not chunks:
        try:
            name = getattr(control, "Name", "") or ""
            if name.strip():
                chunks.append(name)
        except Exception:
            pass

    return normalize_accessibility_text(*chunks)


def _find_best_document(root):
    """Largest on-screen Document that exposes readable content (browser page body)."""
    best = None
    best_score = -1
    queue = deque([(root, 0)])
    visited = 0
    while queue and visited < MAX_UI_NODES * 2:
        control, depth = queue.popleft()
        visited += 1
        try:
            if _control_type_name(control) == "DocumentControl":
                area = _control_area(control)
                if area >= 50_000:
                    sample = _text_from_control(control)
                    if sample.strip():
                        # Prefer IsContentElement documents (real page) over empty WebViews.
                        score = area + (1_000_000_000 if _is_content_element(control) else 0)
                        if score > best_score:
                            best = control
                            best_score = score
            if depth < MAX_UI_DEPTH:
                queue.extend((child, depth + 1) for child in control.GetChildren())
        except Exception:
            continue
    return best


def _collect_content_walk(root) -> str:
    """Fallback: BFS but only content-ish controls, skipping chrome types."""
    values: list[str] = []
    queue = deque([(root, 0)])
    visited = 0
    while queue and visited < MAX_UI_NODES:
        control, depth = queue.popleft()
        visited += 1
        try:
            ctype = _control_type_name(control)
            if ctype in _CHROME_CONTROL_TYPES:
                # Still walk children — content can sit under a pane near chrome.
                if depth < MAX_UI_DEPTH:
                    queue.extend((child, depth + 1) for child in control.GetChildren())
                continue
            if ctype in _CONTENT_CONTROL_TYPES or _is_content_element(control):
                piece = _text_from_control(control)
                if piece:
                    values.append(piece)
            if depth < MAX_UI_DEPTH:
                queue.extend((child, depth + 1) for child in control.GetChildren())
        except Exception:
            continue
        if sum(len(value) for value in values) >= MAX_TEXT_CHARS * 2:
            break
    return normalize_accessibility_text(*values)


def _region_score(text: str, area: int) -> float:
    """
    Prefer more text after blocklist, but demote short-label nav rails and
    tiny panes so sidebars don't beat a shorter main thread.
    """
    filtered = strip_ui_chrome(text)
    if not filtered.strip():
        return -1.0
    char_count = len(filtered)
    avg = _avg_line_length(filtered)
    # Paragraph-like text scores higher than many 1–3 word nav labels.
    prose_factor = 0.35 + 0.65 * min(avg / 36.0, 1.0)
    area_factor = 0.45 + 0.55 * min(max(area, 0) / 180_000.0, 1.0)
    if looks_like_nav_soup(filtered):
        prose_factor *= 0.25
    return float(char_count) * prose_factor * area_factor


def _significant_region_children(control) -> list:
    """Direct children that look like competing content regions."""
    regions = []
    try:
        children = list(control.GetChildren())
    except Exception:
        return regions
    for child in children:
        try:
            ctype = _control_type_name(child)
            if ctype in _CHROME_CONTROL_TYPES:
                continue
            area = _control_area(child)
            if area < MIN_REGION_AREA:
                continue
            if ctype in _REGION_CONTROL_TYPES or _is_content_element(child):
                regions.append(child)
        except Exception:
            continue
    return regions


def _competing_regions(scope, *, depth: int = 0) -> list:
    """
    Expand into sibling panes when one child dominates the tree.
    Stops at a set of peer regions we can score against each other.
    """
    children = _significant_region_children(scope)
    if not children:
        return [scope]
    if depth >= _MAX_REGION_EXPAND_DEPTH:
        return children

    parent_area = max(_control_area(scope), 1)
    if len(children) == 1:
        only = children[0]
        # Dig into the single large child to find sidebar vs main siblings.
        if _control_area(only) >= parent_area * 0.55:
            return _competing_regions(only, depth=depth + 1)
        return children

    # One huge pane + tiny siblings → dig into the huge pane for real competition.
    areas = [_control_area(child) for child in children]
    largest_idx = max(range(len(children)), key=lambda i: areas[i])
    if areas[largest_idx] >= parent_area * 0.65 and areas[largest_idx] >= sum(areas) * 0.7:
        nested = _competing_regions(children[largest_idx], depth=depth + 1)
        if len(nested) > 1:
            return nested
    return children


def _control_bounds(control) -> tuple[int, int, int, int] | None:
    """Return a UIA screen-space rectangle, excluding empty/off-screen bounds."""
    try:
        rect = control.BoundingRectangle
        left, top = int(rect.left), int(rect.top)
        right, bottom = int(rect.right), int(rect.bottom)
        if right > left and bottom > top:
            return left, top, right, bottom
    except Exception:
        pass
    return None


def _crop_candidate_score(region, root_bounds: tuple[int, int, int, int]) -> float:
    """
    Prefer a large, central peer pane for OCR. Text only helps break ties:
    UIA's text can be chrome/noise even when its bounding rectangle is useful.
    """
    bounds = _control_bounds(region)
    if bounds is None:
        return -1.0
    root_left, root_top, root_right, root_bottom = root_bounds
    left = max(root_left, bounds[0])
    top = max(root_top, bounds[1])
    right = min(root_right, bounds[2])
    bottom = min(root_bottom, bounds[3])
    root_area = max((root_right - root_left) * (root_bottom - root_top), 1)
    area_ratio = max(0, (right - left) * (bottom - top)) / root_area
    # A whole-window wrapper cannot distinguish app chrome from work.
    if area_ratio < 0.08 or area_ratio > 0.92:
        return -1.0

    centre_x = (left + right) / 2
    centre_y = (top + bottom) / 2
    root_centre_x = (root_left + root_right) / 2
    root_centre_y = (root_top + root_bottom) / 2
    half_width = max((root_right - root_left) / 2, 1)
    half_height = max((root_bottom - root_top) / 2, 1)
    centre_distance = min(
        1.0,
        ((centre_x - root_centre_x) / half_width) ** 2
        + ((centre_y - root_centre_y) / half_height) ** 2,
    )
    centrality = 1.0 - centre_distance

    text = strip_ui_chrome(_collect_content_walk(region))
    text_bonus = 0.0
    if text and not looks_like_nav_soup(text):
        text_bonus = min(len(text) / 1000.0, 1.0)
    # Geometry is deliberately weighted higher than text here.
    return area_ratio * 0.60 + centrality * 0.30 + text_bonus * 0.10


def _best_content_region(scope):
    """Return the best bounded pane inside scope for use as an OCR crop."""
    root_bounds = _control_bounds(scope)
    if root_bounds is None:
        return None
    best = None
    best_score = -1.0
    for region in _competing_regions(scope):
        score = _crop_candidate_score(region, root_bounds)
        if score > best_score:
            best = region
            best_score = score
    return best


def foreground_content_bounds() -> tuple[int, int, int, int] | None:
    """
    Best-effort screen-space work-region rectangle for OCR.

    The result is geometry only: it remains useful when a UIA tree exposes
    Cursor/VS Code chrome text but not the actual editor buffer.
    """
    if not IS_WINDOWS:
        return None
    try:
        import uiautomation as auto
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        root = auto.ControlFromHandle(hwnd) if hwnd else None
        if root is None:
            return None
        document = _find_best_document(root)
        scope = document if document is not None else root
        region = _best_content_region(scope)
        return _control_bounds(region) if region is not None else None
    except Exception:
        return None


def _best_region_text(scope) -> str:
    """
    Split scope into competing regions, blocklist each, pick the max-scoring
    region as document text.
    """
    if scope is None:
        return ""
    regions = _competing_regions(scope)
    best_text = ""
    best_score = -1.0
    for region in regions:
        try:
            # Walk the region subtree — avoid Document TextPattern dumping the
            # whole page (sidebar + main) into one blob.
            raw = _collect_content_walk(region)
            if not raw.strip() and _control_type_name(region) in {
                "EditControl",
                "DocumentControl",
            }:
                raw = _text_from_control(region)
            filtered = strip_ui_chrome(raw)
            score = _region_score(filtered, _control_area(region))
            if score > best_score:
                best_score = score
                best_text = filtered
        except Exception:
            continue

    if best_text.strip():
        return best_text[:MAX_TEXT_CHARS]

    # No competing peers — fall back to scoped walk / document text.
    walked = strip_ui_chrome(_collect_content_walk(scope))
    if walked.strip():
        return walked[:MAX_TEXT_CHARS]
    return strip_ui_chrome(_text_from_control(scope))[:MAX_TEXT_CHARS]


def _windows_text() -> str:
    """
    Extract foreground text with structure-first filtering:
      1) focused Edit/Document
      2) best region under largest content Document (max text after blocklist)
      3) best region under window root
      4) content-control walk (skip toolbars/menus/buttons)
    """
    try:
        import uiautomation as auto
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return ""
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return ""

        # 1) Focused editor / document — best for Cursor, Notepad, forms.
        try:
            focused = auto.GetFocusedControl()
        except Exception:
            focused = None
        if focused is not None and _control_type_name(focused) in {"EditControl", "DocumentControl"}:
            focused_text = _text_from_control(focused)
            if is_useful_accessibility_text(focused_text):
                return focused_text[:MAX_TEXT_CHARS]

        # 2) Browser / app document: compete regions (sidebar vs main pane).
        document = _find_best_document(root)
        if document is not None:
            region_text = _best_region_text(document)
            if region_text.strip():
                return region_text[:MAX_TEXT_CHARS]

        # 3) No document pane — compete regions under the window itself.
        window_region = _best_region_text(root)
        if window_region.strip():
            return window_region[:MAX_TEXT_CHARS]

        # 4) Structured fallback walk.
        walked = _collect_content_walk(root)
        if walked.strip():
            return walked[:MAX_TEXT_CHARS]

        # Last resort: focused control even if short (still better than chrome soup).
        if focused is not None:
            return _text_from_control(focused)[:MAX_TEXT_CHARS]
        return ""
    except Exception:
        return ""


_MAC_ACCESSIBILITY_SCRIPT = r'''
tell application "System Events"
    set frontProc to first application process whose frontmost is true
    set outputText to ""
    try
        set uiItems to entire contents of front window of frontProc
        repeat with uiItem in uiItems
            try
                set itemRole to role of uiItem
                if itemRole is not "AXSecureTextField" then
                    if itemRole is in {"AXButton", "AXToolbar", "AXTabGroup", "AXSplitter", "AXScrollBar", "AXMenu", "AXMenuItem", "AXMenuBar"} then
                        -- skip chrome-like roles
                    else
                        set itemText to ""
                        try
                            set itemText to value of uiItem as text
                        end try
                        if itemText is "" or itemText is "missing value" then
                            try
                                set itemText to name of uiItem as text
                            end try
                        end if
                        if itemText is not "" and itemText is not "missing value" then
                            set outputText to outputText & itemText & linefeed
                        end if
                    end if
                end if
            end try
            if length of outputText > 8000 then exit repeat
        end repeat
    end try
    return outputText
end tell
'''


def _mac_text() -> str:
    return normalize_accessibility_text(
        _run_command(["osascript", "-e", _MAC_ACCESSIBILITY_SCRIPT], timeout=2.0)
    )


def extract_accessibility_text() -> str:
    """Read bounded text from the foreground UI without taking a screenshot."""
    if IS_WINDOWS:
        return _windows_text()
    if IS_MACOS:
        return _mac_text()
    return ""
