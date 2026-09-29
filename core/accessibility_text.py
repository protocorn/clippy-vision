from __future__ import annotations

import heapq
import re
from collections import deque
import threading
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
    """True when text is a short list of nav labels, not a real screen.

    An editor, terminal, or file list is also short lines, but there are many
    of them and they differ. A sidebar is a handful of repeated labels.
    """
    lines = _nonempty_lines(strip_ui_chrome(text))
    if len(lines) < 6:
        return False
    unique = len({line.casefold() for line in lines})
    compact = "".join(character for character in "\n".join(lines) if character.isalnum())
    if len(lines) >= 30 and unique >= 15 and len(compact) >= 500:
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


def prefer_active_text(active: str, body: str) -> str:
    """Keep the caret neighborhood ahead of the rest of the page.

    Stored text is capped. A document read from the top can fill that cap
    before the field the user is actually in. Lines from ``active`` stay
    first; the same line later in ``body`` is dropped.
    """
    active = str(active or "").strip()
    body = str(body or "").strip()
    if not active:
        return normalize_accessibility_text(body)
    if not body:
        return normalize_accessibility_text(active)
    return normalize_accessibility_text(active, body)


def rank_text_by_point(
    pieces: list[tuple[str, tuple | None]],
    point: tuple[int, int] | None,
) -> str:
    """Order text snippets so the ones nearest ``point`` survive the cap."""
    if point is None:
        return normalize_accessibility_text(*(text for text, _bounds in pieces))

    def distance(bounds: tuple | None) -> float:
        if not bounds:
            return 10**18
        center_x = (bounds[0] + bounds[2]) / 2
        center_y = (bounds[1] + bounds[3]) / 2
        return (center_x - point[0]) ** 2 + (center_y - point[1]) ** 2

    ordered = sorted(
        enumerate(pieces),
        key=lambda item: (distance(item[1][1]), item[0]),
    )
    return normalize_accessibility_text(*(item[1][0] for item in ordered))


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


_PATTERN_TEXT_TYPES = {"EditControl", "DocumentControl", "ComboBoxControl"}


def _text_from_control(control, *, patterns: bool = True) -> str:
    """Prefer TextPattern document text, then Value, then Name for content types.

    ``patterns=False`` reads only the Name. Pattern queries are COM round
    trips; a label, link, or list row already carries its text in Name.
    """
    if control is None or _is_password(control):
        return ""
    chunks: list[str] = []

    if patterns:
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


def _box_area(bounds: tuple[int, int, int, int] | None) -> int:
    if not bounds:
        return 0
    return max(0, bounds[2] - bounds[0]) * max(0, bounds[3] - bounds[1])


def _box_contains_point(bounds: tuple[int, int, int, int], x: float, y: float) -> bool:
    return bounds[0] <= x < bounds[2] and bounds[1] <= y < bounds[3]


def _boxes_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _activity_center(box: tuple[int, int, int, int]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def _distance_to_point(bounds: tuple | None, point: tuple[int, int]) -> float:
    if not bounds:
        return 10**18
    center_x = (bounds[0] + bounds[2]) / 2
    center_y = (bounds[1] + bounds[3]) / 2
    return (center_x - point[0]) ** 2 + (center_y - point[1]) ** 2


# Controls smaller than this are icons. Their children are glyphs.
_DESCEND_MIN_AREA = 1_500


def _note_content_piece(
    control,
    pieces: list[tuple[str, tuple | None]],
    bounds: tuple[int, int, int, int] | None = None,
) -> None:
    ctype = _control_type_name(control)
    if ctype in _CHROME_CONTROL_TYPES:
        return
    if ctype not in _CONTENT_CONTROL_TYPES and not _is_content_element(control):
        return
    piece = _text_from_control(control, patterns=ctype in _PATTERN_TEXT_TYPES)
    if piece:
        pieces.append((piece, bounds if bounds is not None else _control_bounds(control)))


def _worth_descending(bounds: tuple[int, int, int, int] | None, depth: int) -> bool:
    if depth >= MAX_UI_DEPTH:
        return False
    return bounds is None or _box_area(bounds) >= _DESCEND_MIN_AREA


def _collect_content_pieces(
    root,
    point: tuple[int, int] | None = None,
    *,
    limit: int = MAX_UI_NODES,
    visit=None,
) -> list[tuple[str, tuple | None]]:
    """Content snippets under root, reading at most ``limit`` controls.

    With a point, visit controls nearest that point first. A file tree on
    the left otherwise spends the node budget before the editor is reached.
    ``visit(control, type_name, bounds)`` sees every control read, so a
    caller can note edit fields in the same pass.
    """
    pieces: list[tuple[str, tuple | None]] = []
    if root is None:
        return pieces
    if point is None:
        queue = deque([(root, 0)])
        visited = 0
        while queue and visited < limit:
            control, depth = queue.popleft()
            visited += 1
            try:
                ctype = _control_type_name(control)
                bounds = _control_bounds(control)
                if visit is not None:
                    visit(control, ctype, bounds)
                if ctype in _CHROME_CONTROL_TYPES:
                    continue
                _note_content_piece(control, pieces, bounds)
                if _worth_descending(bounds, depth):
                    queue.extend((child, depth + 1) for child in control.GetChildren())
            except Exception:
                continue
            if sum(len(text) for text, _bounds in pieces) >= MAX_TEXT_CHARS * 2:
                break
        return pieces

    heap: list[tuple[float, int, object, int]] = [(0.0, 0, root, 0)]
    seen: set[int] = set()
    order = 1
    visited = 0
    while heap and visited < limit:
        _distance, _order, control, depth = heapq.heappop(heap)
        if id(control) in seen:
            continue
        seen.add(id(control))
        visited += 1
        try:
            ctype = _control_type_name(control)
            bounds = _control_bounds(control)
            if visit is not None:
                visit(control, ctype, bounds)
            if ctype in _CHROME_CONTROL_TYPES:
                continue
            _note_content_piece(control, pieces, bounds)
            if _worth_descending(bounds, depth):
                for child in control.GetChildren():
                    if id(child) in seen:
                        continue
                    order += 1
                    heapq.heappush(
                        heap,
                        (_distance_to_point(_control_bounds(child), point), order, child, depth + 1),
                    )
        except Exception:
            continue
    return pieces


def _collect_content_walk(root, point: tuple[int, int] | None = None) -> str:
    """Fallback: content controls only, skipping chrome types."""
    return rank_text_by_point(_collect_content_pieces(root, point), point)


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
    # Reject near-empty slivers outright. Do NOT reject near-full-window
    # regions the same way — single-pane apps (editors, terminals, most
    # Electron apps) legitimately have one content region filling nearly the
    # entire window. Rejecting those left _best_content_region() with zero
    # candidates for that whole class of app, forcing a heuristic crop every
    # time (confirmed via scripts/probe_ocr.py against Cursor.exe itself).
    if area_ratio < 0.08:
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
    if best is None:
        # No competing region cleared the bar (e.g. every candidate was a
        # tiny sliver). scope itself still has real UIA geometry — better
        # than falling all the way back to a percentage-based guess.
        return scope
    return best


def foreground_content_bounds(hwnd: int | None = None) -> tuple[int, int, int, int] | None:
    """
    Best-effort screen-space work-region rectangle for OCR.

    The result is geometry only: it remains useful when a UIA tree exposes
    Cursor/VS Code chrome text but not the actual editor buffer.
    """
    if IS_MACOS:
        try:
            from core.mac_ui import front_content_bounds
        except ImportError:
            from mac_ui import front_content_bounds
        return front_content_bounds()
    if not IS_WINDOWS:
        return None
    try:
        import uiautomation as auto
        import win32gui

        target = hwnd or win32gui.GetForegroundWindow()
        root = auto.ControlFromHandle(target) if target else None
        if root is None:
            return None
        document = _find_best_document(root)
        scope = document if document is not None else root
        region = _best_content_region(scope)
        return _control_bounds(region) if region is not None else None
    except Exception:
        return None


def _text_of_region(region, point: tuple[int, int] | None) -> str:
    raw = _collect_content_walk(region, point)
    if not raw.strip() and _control_type_name(region) in {"EditControl", "DocumentControl"}:
        raw = _text_from_control(region)
    return strip_ui_chrome(raw)


def _best_region_text(scope, point: tuple[int, int] | None = None) -> str:
    """Densest non-nav region. Used by probes; capture uses ``core.screen_tiles``."""
    if scope is None:
        return ""
    regions = _competing_regions(scope)
    best_text = ""
    best_score = -1.0
    for region in regions:
        try:
            # Walk the region subtree — avoid Document TextPattern dumping the
            # whole page (sidebar + main) into one blob.
            filtered = _text_of_region(region, point)
            score = _region_score(filtered, _control_area(region))
            if score > best_score:
                best_score = score
                best_text = filtered
        except Exception:
            continue

    if best_text.strip():
        return best_text[:MAX_TEXT_CHARS]

    # No competing peers — fall back to scoped walk / document text.
    walked = strip_ui_chrome(_collect_content_walk(scope, point))
    if walked.strip():
        return walked[:MAX_TEXT_CHARS]
    return strip_ui_chrome(_text_from_control(scope))[:MAX_TEXT_CHARS]


def _screen_text_from_root(root, focused, document, *, window: str = "", visit=None):
    """Text and rectangle of the tile the user is working in.

    The window is divided into tiles from the tree's geometry. The tile
    with the most activity since the last walk wins; ties are all kept.
    An empty winner stays empty, which is what sends OCR to that tile.
    """
    from core.screen_tiles import active_screen_text

    return active_screen_text(
        root, focused, document, window=window or _window_memory_key(root), visit=visit
    )


def _window_memory_key(root) -> str:
    try:
        return str(int(getattr(root, "NativeWindowHandle", 0) or 0))
    except Exception:
        return ""


def _windows_text(hwnd: int | None = None) -> str:
    """Foreground text from the active tile of the foreground window."""
    try:
        import uiautomation as auto
        import win32gui

        target = hwnd or win32gui.GetForegroundWindow()
        if not target:
            return ""
        root = auto.ControlFromHandle(target)
        if root is None:
            return ""
        try:
            focused = auto.GetFocusedControl()
        except Exception:
            focused = None
        choice = _screen_text_from_root(root, focused, _find_best_document(root), window=str(target))
        return choice.text
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


def extract_accessibility_text(hwnd: int | None = None) -> str:
    """Read bounded text from the foreground UI without taking a screenshot."""
    if IS_WINDOWS:
        return _windows_text(hwnd)
    if IS_MACOS:
        return _mac_text()
    return ""


_EDIT_CONTROL_TYPES = {"EditControl", "SpinnerControl"}
_FIELD_NAME_MAX = 48
_MAX_SECRET_LOOKUPS = 12


def _edit_value(control) -> str:
    """Current field contents. Password controls still report empty while masked."""
    if _is_password(control):
        return ""
    try:
        pattern = control.GetValuePattern()
        value = "" if pattern is None else str(pattern.Value or "")
    except Exception:
        return ""
    return value.strip()[:200]


def _pattern_source_text(control) -> str:
    """Raw TextPattern buffer for the control that actually holds the text.

    The .env case is this buffer: the focused editor's DocumentRange, which
    is what gets written to the accessibility sidecar. FindText needs those
    characters unchanged, so this does not normalize or drop lines.
    """
    if control is None:
        return ""
    try:
        pattern = control.GetTextPattern()
        document = None if pattern is None else pattern.DocumentRange
        if document is None:
            return ""
        return str(document.GetText(MAX_TEXT_CHARS) or "")
    except Exception:
        return ""


def _rects_for_values(control, values: list[str]) -> tuple[list[tuple[int, int, int, int]], bool]:
    """Visible line rectangles for secret strings, and whether every value was located.

    IUIAutomationTextRange.FindText returns the first hit. The search range
    then starts at that hit's end so a repeated secret is not left on screen.
    ``complete`` is false when any value has no rectangle; the caller paints
    the whole control instead of leaving that span visible.
    """
    try:
        pattern = control.GetTextPattern()
        document = None if pattern is None else pattern.DocumentRange
    except Exception:
        return [], False
    if document is None:
        return [], False
    try:
        import uiautomation as auto

        start = auto.TextPatternRangeEndpoint.Start
        end = auto.TextPatternRangeEndpoint.End
    except Exception:
        return [], False

    rects: list[tuple[int, int, int, int]] = []
    complete = len(values) <= _MAX_SECRET_LOOKUPS
    for value in values[:_MAX_SECRET_LOOKUPS]:
        try:
            search = document.Clone()
        except Exception:
            return rects, False
        located = False
        for _hit in range(4):
            try:
                found = search.FindText(value, False, False)
            except Exception:
                found = None
            if found is None:
                break
            try:
                for rect in found.GetBoundingRectangles():
                    if rect.width() >= 4 and rect.height() >= 4:
                        rects.append(
                            (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
                        )
                        located = True
            except Exception:
                pass
            try:
                if not search.MoveEndpointByRange(start, found, end, waitTime=0):
                    break
            except Exception:
                break
        if not located:
            complete = False
    return rects, complete


_ACTIVITY_LINES = 8
_ACTIVITY_CHARS = 1600
# A box larger than this is the page, not the place the user is working.
_ACTIVITY_BOX_AREA = 250_000


def _pointer_inside(bounds: tuple[int, int, int, int] | None) -> tuple[int, int] | None:
    """Cursor position when it sits inside ``bounds``. None otherwise."""
    if bounds is None or not IS_WINDOWS:
        return None
    try:
        import win32gui

        x, y = win32gui.GetCursorPos()
    except Exception:
        return None
    left, top, right, bottom = bounds
    if left <= x < right and top <= y < bottom:
        return int(x), int(y)
    return None


def _caret_box() -> tuple[int, int, int, int] | None:
    """Screen rectangle of the text caret in the foreground window.

    A caret is often 0 pixels wide. That still marks where the user is typing
    or selecting, including inside editors the accessibility tree cannot read.
    """
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        import win32gui
        import win32process

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", RECT),
            ]

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        thread_id, _pid = win32process.GetWindowThreadProcessId(hwnd)
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not ctypes.windll.user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return None
        if not info.hwndCaret:
            return None
        rect = info.rcCaret
        left, top = int(rect.left), int(rect.top)
        right, bottom = int(rect.right), int(rect.bottom)
        if bottom <= top:
            return None
        if right <= left:
            right = left + 2
        return left, top, right, bottom
    except Exception:
        return None


def _selection_box(control) -> tuple[int, int, int, int] | None:
    """Visible caret or selection from the accessibility text pattern.

    Monaco and other custom editors often have no Win32 caret. The text
    pattern still reports a 1px-wide range where the user is typing.
    """
    if control is None:
        return None
    try:
        pattern = control.GetTextPattern()
    except Exception:
        return None
    if pattern is None:
        return None
    try:
        selected = pattern.GetSelection() or []
    except Exception:
        return None
    if not selected:
        return None
    try:
        rects = selected[0].GetBoundingRectangles() or []
    except Exception:
        return None
    if not rects:
        return None
    rect = rects[0]
    left, top = int(rect.left), int(rect.top)
    right, bottom = int(rect.right), int(rect.bottom)
    if bottom <= top:
        return None
    if right <= left:
        right = left + 2
    box = (left, top, right, bottom)
    bounds = _control_bounds(control)
    if bounds and _box_area(box) > _box_area(bounds) * 0.5:
        return None
    return box


def _resolve_activity_box(root, focused) -> tuple[int, int, int, int] | None:
    """Caret, then the text-pattern selection, then a small focused field."""
    root_bounds = _control_bounds(root)
    caret = _caret_box()
    if caret and root_bounds and _boxes_overlap(caret, root_bounds):
        return caret
    selected = _selection_box(focused)
    if selected and (root_bounds is None or _boxes_overlap(selected, root_bounds)):
        return selected
    if focused is None:
        return None
    bounds = _control_bounds(focused)
    if bounds and _box_area(bounds) <= _ACTIVITY_BOX_AREA and root_bounds and _boxes_overlap(bounds, root_bounds):
        return bounds
    return None


def _excerpt_around(text_range) -> str:
    """Lines around a caret or hit, without uiautomation's default half-second sleep."""
    try:
        import uiautomation as auto

        cloned = text_range.Clone()
        start = auto.TextPatternRangeEndpoint.Start
        end = auto.TextPatternRangeEndpoint.End
        cloned.MoveEndpointByUnit(start, auto.TextUnit.Line, -_ACTIVITY_LINES, waitTime=0)
        cloned.MoveEndpointByUnit(end, auto.TextUnit.Line, _ACTIVITY_LINES, waitTime=0)
        return str(cloned.GetText(_ACTIVITY_CHARS) or "")
    except Exception:
        return ""


def _activity_excerpt(control) -> str:
    """Text around the caret, or around the cursor when it is inside the control."""
    if control is None:
        return ""
    try:
        pattern = control.GetTextPattern()
    except Exception:
        return ""
    if pattern is None:
        return ""
    try:
        selected = pattern.GetSelection() or []
    except Exception:
        selected = []
    if selected:
        excerpt = _excerpt_around(selected[0])
        if excerpt.strip():
            return excerpt
    point = _pointer_inside(_control_bounds(control))
    if point is None:
        return ""
    try:
        hit = pattern.RangeFromPoint(point[0], point[1])
    except Exception:
        return ""
    if hit is None:
        return ""
    return _excerpt_around(hit)


def _focused_editor(auto):
    """Focused Edit or Document. That is the control whose buffer was stored for Cursor."""
    try:
        focused = auto.GetFocusedControl()
    except Exception:
        return None
    if focused is None or _control_type_name(focused) not in {"EditControl", "DocumentControl"}:
        return None
    return focused


def _own_label(control) -> str:
    """Text that belongs to this control, not the document underneath it."""
    try:
        name = str(getattr(control, "Name", "") or "").strip()
    except Exception:
        return ""
    if not name or len(name) > 180 or "\n" in name:
        return ""
    return name


def _note_edit(control, edit_rects: list, edit_values: list, seen: set) -> None:
    try:
        from core.secret_fields import should_redact_edit
    except ImportError:
        from secret_fields import should_redact_edit

    bounds = _control_bounds(control)
    if bounds is None or bounds in seen:
        return
    is_password = _is_password(control)
    name = ""
    try:
        name = str(getattr(control, "Name", "") or "")
    except Exception:
        name = ""
    height = bounds[3] - bounds[1]
    if not is_password and not should_redact_edit(name, height):
        return
    seen.add(bounds)
    edit_rects.append(bounds)
    edit_values.append(_edit_value(control))


def _mac_redaction() -> dict | None:
    """Password fields, secret spans, and scrubbed text from the front Mac window."""
    try:
        from core.mac_ui import front_field_nodes, redaction_from_nodes
    except ImportError:
        from mac_ui import front_field_nodes, redaction_from_nodes

    nodes = front_field_nodes()
    if nodes is None:
        return None
    result = redaction_from_nodes(nodes)
    result["redacted_text"] = normalize_accessibility_text(result.get("redacted_text") or "")
    return result


def collect_redaction(hwnd: int | None = None) -> dict | None:
    """Fields and secret spans to remove before a screenshot is stored.

    Single-line edits are collected from the page document, not the window
    root. A browser toolbar otherwise spends the node budget before the
    login fields are reached. Secret strings are taken from that document
    and from the focused editor, because a .env buffer is the focused
    editor's text, not a single-line field. Returns None when the walk
    cannot run, so the caller can drop the frame.
    """
    try:
        from core.secret_patterns import (
            is_secret_name,
            paired_row_secrets,
            redact_field_values,
            redact_secrets,
            secret_values,
        )
    except ImportError:
        from secret_patterns import (
            is_secret_name,
            paired_row_secrets,
            redact_field_values,
            redact_secrets,
            secret_values,
        )

    if IS_MACOS:
        return _mac_redaction()
    if not IS_WINDOWS:
        return {"edit_rects": [], "secret_rects": [], "redacted_text": ""}
    try:
        import uiautomation as auto
        import win32gui

        target = hwnd or win32gui.GetForegroundWindow()
        if not target:
            return None
        root = auto.ControlFromHandle(target)
        if root is None:
            return None
    except Exception:
        return None

    document = _find_best_document(root)
    focused = _focused_editor(auto)

    edit_rects: list[tuple[int, int, int, int]] = []
    edit_values: list[str] = []
    seen_edits: set[tuple[int, int, int, int]] = set()
    row_nodes: list[dict] = []

    def _visit(control, ctype: str, bounds) -> None:
        # Runs inside the tile reads, so fields are found in the same pass
        # that collects text. A second walk of the same tree is not needed.
        is_password = _is_password(control)
        if ctype in _EDIT_CONTROL_TYPES or is_password:
            _note_edit(control, edit_rects, edit_values, seen_edits)
        label = _own_label(control)
        if label and bounds is not None:
            row_nodes.append({"text": label, "bounds": bounds})
            if is_secret_name(label):
                value = _edit_value(control)
                if value and value != label:
                    row_nodes.append({"text": value, "bounds": bounds})

    # Pattern text below still locates secret rectangles. The stored string
    # is the active tile only, and that tile's rectangle is the OCR crop.
    choice = _screen_text_from_root(root, focused, document, window=str(target), visit=_visit)
    if focused is not None:
        _note_edit(focused, edit_rects, edit_values, seen_edits)

    secret_rects: list[tuple[int, int, int, int]] = []
    seen_controls = set()
    for control in (focused, document):
        if control is None or id(control) in seen_controls:
            continue
        seen_controls.add(id(control))
        raw = _pattern_source_text(control)
        if not raw.strip():
            continue
        values = secret_values(raw)
        if not values:
            continue
        located, complete = _rects_for_values(control, values)
        secret_rects.extend(located)
        if not complete:
            bounds = _control_bounds(control)
            if bounds is not None:
                secret_rects.append(bounds)
    paired = paired_row_secrets(row_nodes)
    secret_rects.extend(item["bounds"] for item in paired)
    value_tokens = edit_values + [item["text"] for item in paired]
    redacted = redact_secrets(redact_field_values(choice.text, value_tokens))
    return {
        "edit_rects": edit_rects,
        "secret_rects": secret_rects,
        "redacted_text": normalize_accessibility_text(redacted),
        "content_bounds": choice.bounds,
    }


def collect_edit_snapshots(hwnd: int | None = None) -> list[dict] | None:
    """Edit boxes and nearby captions for secret-field memory.

    Returns None when the walk could not run, so the caller keeps the
    previous fingerprint. Returns [] when the window truly has no edits.
    Values are not read. Names longer than a caption are dropped so a
    document body cannot become part of the fingerprint.
    """
    if not IS_WINDOWS:
        return []
    try:
        import uiautomation as auto
        import win32gui

        target = hwnd or win32gui.GetForegroundWindow()
        if not target:
            return None
        root = auto.ControlFromHandle(target)
        if root is None:
            return None
    except Exception:
        return None

    nodes: list[dict] = []
    queue = deque([(root, 0)])
    visited = 0
    while queue and visited < MAX_UI_NODES:
        control, depth = queue.popleft()
        visited += 1
        try:
            ctype = _control_type_name(control)
            bounds = _control_bounds(control)
            is_password = _is_password(control)
            name = ""
            try:
                name = str(getattr(control, "Name", "") or "")
            except Exception:
                name = ""
            if not name:
                try:
                    name = str(getattr(control, "AutomationId", "") or "")
                except Exception:
                    name = ""
            name = " ".join(name.split())
            if len(name) > _FIELD_NAME_MAX:
                name = ""
            is_edit = ctype in _EDIT_CONTROL_TYPES or is_password
            if bounds is not None and (is_edit or name):
                nodes.append(
                    {
                        "name": name,
                        "bounds": bounds,
                        "is_password": is_password,
                        "is_edit": is_edit,
                    }
                )
            if depth < MAX_UI_DEPTH:
                queue.extend((child, depth + 1) for child in control.GetChildren())
        except Exception:
            continue
    return nodes


def _run_with_timeout(func, timeout: float, *args, **kwargs):
    """Run ``func`` on a helper thread and abandon it past ``timeout``.

    UIA COM calls can hang indefinitely against certain apps. Python cannot
    kill a thread, so on timeout we return the fallback immediately and let
    the stuck worker thread die on its own; it only ever touches its own
    locals, never shared state, so an abandoned thread is harmless.

    UI Automation has to be initialized on that helper thread. Without it
    every walk fails with CoInitialize and the frame is stored with no text.
    """
    box: list = [None]

    def _target():
        try:
            if IS_WINDOWS:
                import uiautomation as auto

                with auto.UIAutomationInitializerInThread():
                    box[0] = func(*args, **kwargs)
            else:
                box[0] = func(*args, **kwargs)
        except Exception:
            box[0] = None

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None
    return box[0]

def foreground_content_bounds_safe(
    hwnd: int | None = None, timeout: float = 1.0
) -> tuple[int, int, int, int] | None:
    """Timeout-guarded wrapper — the one background workers should call."""
    return _run_with_timeout(foreground_content_bounds, timeout, hwnd)
def extract_accessibility_text_safe(hwnd: int | None = None, timeout: float = 1.5) -> str:
    """Timeout-guarded wrapper — the one background workers should call."""
    return _run_with_timeout(extract_accessibility_text, timeout, hwnd) or ""


def collect_edit_snapshots_safe(
    hwnd: int | None = None, timeout: float = 1.5
) -> list[dict] | None:
    """Timeout-guarded field walk. None means keep the previous fingerprint."""
    return _run_with_timeout(collect_edit_snapshots, timeout, hwnd)


def collect_redaction_safe(hwnd: int | None = None, timeout: float = 1.5) -> dict | None:
    """Timeout-guarded redaction targets. None means the walk did not finish."""
    return _run_with_timeout(collect_redaction, timeout, hwnd)