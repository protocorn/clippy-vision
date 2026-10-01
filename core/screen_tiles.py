"""Divide a window into tiles from its accessibility tree.

An accessibility tree is a map. Wrapper nodes repeat the window rectangle;
the panes below them tile it: a sidebar, an editor, a terminal, a chat
column. The active tile still decides the rectangle sent to OCR. Stored
text is every pane that was read, with the active pane first, plus a
text-holder buffer when that buffer is longer than the pane's labels.
On a later walk of the same window, lines that changed come first, so the
character cap keeps the edit instead of the file tree and the terminal.
A full-window document is a wrapper and is not a second copy of the screen.

The rules use geometry and control type only, so they hold for Electron,
browsers, Office, and native apps alike.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field

from core.accessibility_text import (
    MAX_TEXT_CHARS,
    _activity_center,
    _activity_excerpt,
    _box_area,
    _box_contains_point,
    _boxes_overlap,
    _collect_content_pieces,
    _control_bounds,
    _control_type_name,
    _pointer_inside,
    _resolve_activity_box,
    _text_from_control,
    accessibility_lines,
    normalize_accessibility_text,
    prefer_active_text,
    rank_text_by_point,
    strip_ui_chrome,
)

# A tile has to be a pane a person can work in.
TILE_MIN_AREA = 40_000
TILE_MIN_SIDE = 48
TILE_MIN_SHARE = 0.05
# A child this close to its parent's size is the parent again.
WRAPPER_SHARE = 0.85
# A tile smaller than this is not divided further.
TILE_SPLIT_MIN_AREA = 150_000
MAX_TILES = 12
MAX_TILE_NODES = 320
MAX_TILE_DEPTH = 26
# Text nodes read per tile. Small so twelve tiles still fit one walk.
TILE_TEXT_NODES = 60
# The caret's tile is read more deeply. The other panes are still read.
CARET_TILE_NODES = 160
# One pass over text holders, shared by every tile. Pattern queries are
# slower than reading a name, so the walk stays bounded.
HOLDER_NODE_BUDGET = 800

# Bars are never tiles. Their labels are not what the user is working on.
BAR_CONTROL_TYPES = {
    "StatusBarControl",
    "MenuBarControl",
    "MenuControl",
    "ToolBarControl",
    "TitleBarControl",
    "TabControl",
    "ScrollBarControl",
    "SeparatorControl",
    "ThumbControl",
}
_TEXT_HOLDER_TYPES = {"EditControl", "DocumentControl"}

# The caret is the user's own activity. It outranks output that changed
# elsewhere, so only its tile has to be read.
SCORE_CARET = 10.0
SCORE_SCROLL = 3.0
SCORE_TEXT_BASE = 2.0
SCORE_TEXT_RANGE = 3.0
SCORE_FOCUS = 2.0
SCORE_POINTER = 1.0


@dataclass
class Tile:
    control: object
    bounds: tuple[int, int, int, int]
    type_name: str
    text: str = ""
    lines: frozenset[str] = field(default_factory=frozenset)
    scroll: float | None = None
    score: float = 0.0
    read: bool = False


@dataclass
class TileChoice:
    text: str
    bounds: tuple[int, int, int, int] | None
    tiles: list[Tile]
    winners: list[Tile]


class _Budget:
    __slots__ = ("nodes",)

    def __init__(self) -> None:
        self.nodes = 0


def _clip(box: tuple[int, int, int, int], frame: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return (
        max(box[0], frame[0]),
        max(box[1], frame[1]),
        min(box[2], frame[2]),
        min(box[3], frame[3]),
    )


def _divide(control, bounds, frame, depth: int, budget: _Budget) -> list[Tile]:
    """Tiles under ``control``. Empty when the node does not divide."""
    if depth > MAX_TILE_DEPTH or budget.nodes >= MAX_TILE_NODES:
        return []
    try:
        children = list(control.GetChildren())
    except Exception:
        return []
    parent_area = max(_box_area(bounds), 1)
    tiles: list[Tile] = []
    for child in children:
        if budget.nodes >= MAX_TILE_NODES:
            break
        budget.nodes += 1
        try:
            type_name = _control_type_name(child)
            if type_name in BAR_CONTROL_TYPES:
                continue
            raw = _control_bounds(child)
        except Exception:
            continue
        if raw is None or not _boxes_overlap(raw, frame):
            continue
        box = _clip(raw, frame)
        width, height = box[2] - box[0], box[3] - box[1]
        if width < TILE_MIN_SIDE or height < TILE_MIN_SIDE:
            continue
        area = width * height
        if area < TILE_MIN_AREA:
            continue
        if area >= WRAPPER_SHARE * parent_area:
            # Same rectangle again. Look through it.
            tiles.extend(_divide(child, box, frame, depth + 1, budget))
            continue
        if area < TILE_MIN_SHARE * parent_area:
            continue
        below: list[Tile] = []
        if area >= TILE_SPLIT_MIN_AREA and not _scrolls(child):
            below = _divide(child, box, frame, depth + 1, budget)
        tiles.extend(below or [Tile(child, box, type_name)])
    if len(tiles) < 2:
        return []
    return tiles


def _scrolls(control) -> bool:
    """A scrollable pane is one place to work. Its rows are content, not tiles."""
    try:
        pattern = control.GetScrollPattern()
    except Exception:
        return False
    if pattern is None:
        return False
    try:
        return bool(pattern.VerticallyScrollable)
    except Exception:
        return False


def _dedupe(tiles: list[Tile]) -> list[Tile]:
    seen: set[tuple[int, int, int, int]] = set()
    kept: list[Tile] = []
    for tile in tiles:
        key = tuple(value // 4 for value in tile.bounds)
        if key in seen:
            continue
        seen.add(key)
        kept.append(tile)
    return kept


def discover_tiles(scope) -> list[Tile]:
    """Panes that tile ``scope``. ``scope`` itself when nothing divides it."""
    frame = _control_bounds(scope)
    if frame is None:
        return []
    tiles = _dedupe(_divide(scope, frame, frame, 0, _Budget()))
    if not tiles:
        return [Tile(scope, frame, _control_type_name(scope))]
    if len(tiles) > MAX_TILES:
        tiles = sorted(tiles, key=lambda tile: _box_area(tile.bounds), reverse=True)[:MAX_TILES]
    tiles.sort(key=lambda tile: (tile.bounds[1], tile.bounds[0]))
    return tiles


def _scroll_percent(control) -> float | None:
    try:
        pattern = control.GetScrollPattern()
    except Exception:
        return None
    if pattern is None:
        return None
    try:
        value = float(pattern.VerticalScrollPercent)
    except Exception:
        return None
    if value < 0 or value > 100:
        return None
    return value


def _tile_pieces(tile: Tile, limit: int, visit) -> list[tuple[str, tuple | None]]:
    pieces = _collect_content_pieces(tile.control, None, limit=limit, visit=visit)
    if not pieces and tile.type_name in _TEXT_HOLDER_TYPES:
        own = _text_from_control(tile.control)
        if own.strip():
            pieces = [(own, tile.bounds)]
    return pieces


def _line_id(line: str) -> str:
    return hashlib.blake2b(line.encode("utf-8", "ignore"), digest_size=8).hexdigest()


def _line_set(text: str) -> frozenset[str]:
    return frozenset(_line_id(line) for line in normalize_accessibility_text(text).splitlines())


def order_stored_text(lines: list[str], previous_ids: set[str]) -> str:
    """Changed lines first, so the character cap keeps the edit.

    A first look at a window keeps the original order (active pane, then the
    others). A later look moves lines that were not in the previous walk to
    the front. Unchanged file-tree and chrome lines fall off the end of the cap.
    When nothing changed, the order stays put so a duplicate frame still reads
    the same way.
    """
    if not previous_ids:
        return "\n".join(lines)[:MAX_TEXT_CHARS]
    fresh: list[str] = []
    repeated: list[str] = []
    for line in lines:
        if _line_id(line) in previous_ids:
            repeated.append(line)
        else:
            fresh.append(line)
    if not fresh:
        return "\n".join(lines)[:MAX_TEXT_CHARS]
    return "\n".join(fresh + repeated)[:MAX_TEXT_CHARS]


def _previous_line_ids(previous: dict) -> set[str]:
    found: set[str] = set()
    for entry in previous.values():
        if entry and entry[0]:
            found.update(entry[0])
    return found


def _read_tiles(tiles: list[Tile], point: tuple[int, int] | None, limit: int, visit) -> None:
    for tile in tiles:
        pieces = _tile_pieces(tile, limit, visit)
        inside = point is not None and _box_contains_point(tile.bounds, point[0], point[1])
        ordered = rank_text_by_point(pieces, point if inside else None)
        tile.text = strip_ui_chrome(ordered)
        tile.lines = _line_set(tile.text)
        tile.scroll = _scroll_percent(tile.control)
        tile.read = True


_memory_lock = threading.Lock()
_memory: dict[str, dict[tuple[int, int, int, int], tuple[frozenset[str], float | None]]] = {}
_MEMORY_WINDOWS = 16


def _tile_key(bounds: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return tuple(value // 8 for value in bounds)  # type: ignore[return-value]


def _remember(window: str, tiles: list[Tile]) -> dict[tuple[int, int, int, int], tuple[frozenset[str], float | None]]:
    """Previous line sets and scroll for this window; store the ones read now.

    A tile that was not read this time keeps its last entry, so its next
    read still compares against real text.
    """
    with _memory_lock:
        previous = _memory.pop(window, {})
        current = dict(previous)
        for tile in tiles:
            if tile.read:
                current[_tile_key(tile.bounds)] = (tile.lines, tile.scroll)
        _memory[window] = current
        while len(_memory) > _MEMORY_WINDOWS:
            _memory.pop(next(iter(_memory)))
    return previous


def forget_windows() -> None:
    with _memory_lock:
        _memory.clear()


def _text_change(current: frozenset[str], previous: frozenset[str] | None) -> float:
    if previous is None or not current:
        return 0.0
    new = len(current - previous)
    if new == 0:
        return 0.0
    return new / max(len(current), 1)


def score_tiles(
    tiles: list[Tile],
    *,
    previous: dict,
    activity_box: tuple[int, int, int, int] | None,
    focused_bounds: tuple[int, int, int, int] | None,
    pointer: tuple[int, int] | None,
) -> None:
    for tile in tiles:
        score = 0.0
        if activity_box is not None and _boxes_overlap(tile.bounds, activity_box):
            score += SCORE_CARET
        if focused_bounds is not None and _box_area(focused_bounds) <= _box_area(tile.bounds):
            center = _activity_center(focused_bounds)
            if _box_contains_point(tile.bounds, center[0], center[1]):
                score += SCORE_FOCUS
        if pointer is not None and _box_contains_point(tile.bounds, pointer[0], pointer[1]):
            score += SCORE_POINTER
        if tile.read:
            old_lines, old_scroll = previous.get(_tile_key(tile.bounds), (None, None))
            if tile.scroll is not None and old_scroll is not None and abs(tile.scroll - old_scroll) >= 1.0:
                score += SCORE_SCROLL
            changed = _text_change(tile.lines, old_lines)
            if changed > 0:
                score += SCORE_TEXT_BASE + SCORE_TEXT_RANGE * changed
        tile.score = score


def pick_winners(tiles: list[Tile]) -> list[Tile]:
    if not tiles:
        return []
    top = max(tile.score for tile in tiles)
    return [tile for tile in tiles if abs(tile.score - top) < 1e-9]


def _union(boxes: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int] | None:
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _winner_text(tile: Tile, activity_box, center) -> str:
    if (
        tile.type_name in _TEXT_HOLDER_TYPES
        and activity_box is not None
        and center is not None
        and _box_contains_point(tile.bounds, center[0], center[1])
    ):
        excerpt = _activity_excerpt(tile.control)
        if excerpt.strip():
            return prefer_active_text(excerpt, tile.text)
    return tile.text


def _holder_buffers(scope) -> list[str]:
    """TextPattern buffers of edit and document controls that are real panes.

    A document that covers the window is the window again (menus, title,
    the same labels the tiles already have). Small and mid-size holders
    are the editor, the terminal, and a canvas whose name is only a label.
    """
    frame = _control_bounds(scope)
    frame_area = _box_area(frame) if frame is not None else 0
    try:
        stack = list(scope.GetChildren())
    except Exception:
        return []
    found: list[str] = []
    seen = 0
    while stack and seen < HOLDER_NODE_BUDGET:
        control = stack.pop()
        seen += 1
        try:
            stack.extend(control.GetChildren())
        except Exception:
            pass
        if _control_type_name(control) not in _TEXT_HOLDER_TYPES:
            continue
        box = _control_bounds(control)
        if frame_area and box is not None and _box_area(box) >= frame_area * WRAPPER_SHARE:
            continue
        text = _text_from_control(control, patterns=True)
        if text.strip():
            found.append(text)
    return found


def _stored_pane_text(
    winners: list[Tile],
    tiles: list[Tile],
    holders: list[str],
    previous_ids: set[str],
) -> str:
    """Active pane first, then the other panes. Later walks lead with what changed."""
    rest_tiles = [tile for tile in tiles if tile not in winners and tile.text.strip()]
    rest_tiles.sort(key=lambda tile: len(tile.text), reverse=True)
    holders_by_length = sorted((text for text in holders if text.strip()), key=len, reverse=True)
    lines = accessibility_lines(
        *(tile.text for tile in winners),
        *(tile.text for tile in rest_tiles),
        *holders_by_length,
    )
    return order_stored_text(lines, previous_ids)


def choose_active_tile(
    scope,
    *,
    window: str,
    activity_box: tuple[int, int, int, int] | None = None,
    focused_bounds: tuple[int, int, int, int] | None = None,
    pointer: tuple[int, int] | None = None,
    visit=None,
) -> TileChoice:
    """Text of every pane, and the rectangle of the tile with the most activity.

    ``window`` keys the memory of the last walk. The first walk of a window
    has no text history, so caret, focus, and pointer decide which rectangle
    OCR should use. Every tile is still read. A caret does not discard the
    editor, the terminal, or a chat column that sits in another pane.
    """
    tiles = discover_tiles(scope)
    if not tiles:
        return TileChoice("", None, [], [])
    center = _activity_center(activity_box) if activity_box is not None else None
    point = (int(center[0]), int(center[1])) if center is not None else pointer
    caret_tiles = (
        [tile for tile in tiles if _boxes_overlap(tile.bounds, activity_box)]
        if activity_box is not None
        else []
    )
    if caret_tiles:
        _read_tiles(caret_tiles, point, CARET_TILE_NODES, visit)
        others = [tile for tile in tiles if tile not in caret_tiles]
        if others:
            _read_tiles(others, point, TILE_TEXT_NODES, visit)
    else:
        limit = max(24, min(TILE_TEXT_NODES, 240 // len(tiles)))
        _read_tiles(tiles, point, limit, visit)
    for tile in tiles:
        excerpt = _winner_text(tile, activity_box, center)
        if excerpt.strip():
            tile.text = excerpt
    previous = _remember(window, tiles)
    score_tiles(
        tiles,
        previous=previous,
        activity_box=activity_box,
        focused_bounds=focused_bounds,
        pointer=pointer,
    )
    winners = pick_winners(tiles)
    text = _stored_pane_text(winners, tiles, _holder_buffers(scope), _previous_line_ids(previous))
    return TileChoice(text, _union([tile.bounds for tile in winners]), tiles, winners)


def active_screen_text(root, focused, document, *, window: str, visit=None) -> TileChoice:
    """Foreground pane text, plus the active tile's rectangle for the OCR crop."""
    scope = document if document is not None else root
    if scope is None:
        return TileChoice("", None, [], [])
    activity = _resolve_activity_box(root, focused)
    focused_bounds = _control_bounds(focused) if focused is not None else None
    pointer = _pointer_inside(_control_bounds(scope))
    return choose_active_tile(
        scope,
        window=window,
        activity_box=activity,
        focused_bounds=focused_bounds,
        pointer=pointer,
        visit=visit,
    )
