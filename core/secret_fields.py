"""Remember sensitive form fields and paint them before a screenshot is saved.

Windows clears IsPassword when the reveal control is clicked. The field's
caption and the labels around it stay, so a fingerprint taken while the
field was still a password field keeps matching after the reveal.

The fingerprint stores captions, neighbor captions, and a rectangle. It
never stores the field value.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from PIL import Image, ImageDraw

from core.secret_patterns import label_kind, normalize_label

# Single-line fields are about one control tall. A document editor is much
# taller and is handled by the pattern pass, not by painting the whole pane.
_SINGLE_LINE_MAX_PX = 140
_CHROME_EDIT_NAMES = {
    "address and search bar",
    "search or enter address",
    "search with google or enter address",
    "find in page",
    "find",
}


def should_redact_edit(name: str, height: int) -> bool:
    """Every single-line edit is redacted, except the address bar and find box."""
    if height <= 0 or height > _SINGLE_LINE_MAX_PX:
        return False
    if normalize_label(name) in _CHROME_EDIT_NAMES:
        return False
    return True

_NEAR_X = 280
_NEAR_Y = 140
_MAX_WINDOWS = 12
_MAX_NEIGHBORS = 12

_lock = threading.Lock()
_cache: dict[str, list["RememberedField"]] = {}


@dataclass(frozen=True)
class RememberedField:
    label: str
    neighbors: tuple[str, ...]
    bounds: tuple[int, int, int, int]
    is_password: bool


def clear_field_memory() -> None:
    with _lock:
        _cache.clear()


def _gap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int]:
    horizontal = max(0, max(a[0], b[0]) - min(a[2], b[2]))
    vertical = max(0, max(a[1], b[1]) - min(a[3], b[3]))
    return horizontal, vertical


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    if intersection <= 0:
        return 0.0
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union else 0.0


def _jaccard(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    left, right = set(a), set(b)
    if not left and not right:
        return 0.0
    return len(left & right) / len(left | right)


def _neighbor_names(edit: dict, nodes: list[dict]) -> tuple[str, ...]:
    bounds = edit["bounds"]
    names: list[str] = []
    for node in nodes:
        if node is edit or node.get("is_edit") or node.get("is_password"):
            continue
        other = node.get("bounds")
        name = normalize_label(str(node.get("name") or ""))
        if not other or not name or len(name) > 32 or len(name.split()) > 4:
            continue
        horizontal, vertical = _gap(bounds, other)
        if horizontal <= _NEAR_X and vertical <= _NEAR_Y:
            names.append(name)
    return tuple(sorted(set(names))[:_MAX_NEIGHBORS])


def _snaps_from_nodes(nodes: list[dict]) -> list[RememberedField]:
    snaps: list[RememberedField] = []
    for node in nodes:
        if not (node.get("is_edit") or node.get("is_password")):
            continue
        bounds = node.get("bounds")
        if not bounds or len(bounds) != 4:
            continue
        neighbors = _neighbor_names(node, nodes)
        label = label_kind(str(node.get("name") or "")) or ""
        # The caption often sits beside the box ("Password") rather than on
        # the edit itself. That still identifies the field on the first look,
        # including after the reveal control has cleared IsPassword.
        if not label:
            for name in neighbors:
                kind = label_kind(name)
                if kind:
                    label = kind
                    break
        snaps.append(
            RememberedField(
                label=label,
                neighbors=neighbors,
                bounds=tuple(int(v) for v in bounds),
                is_password=bool(node.get("is_password")),
            )
        )
    return snaps


def _directly_sensitive(field: RememberedField) -> bool:
    return field.is_password or bool(field.label)


def _same_field(current: RememberedField, previous: RememberedField) -> bool:
    """True when `current` is the revealed form of a field we already marked."""
    if not _directly_sensitive(previous):
        return False
    overlap = _iou(current.bounds, previous.bounds)
    if overlap >= 0.6:
        return True
    labels_match = bool(current.label) and current.label == previous.label
    neighbors = _jaccard(current.neighbors, previous.neighbors)
    if labels_match and (overlap >= 0.3 or neighbors >= 0.4):
        return True
    return overlap >= 0.45 and neighbors >= 0.34


def note_window_fields(window_key: str, nodes: list[dict] | None) -> None:
    """Replace the remembered fields for one window.

    ``nodes is None`` means the walk failed. The previous fingerprint stays,
    so a timed-out accessibility pass cannot uncover a password field.
    An empty list means the walk succeeded and this window has no edits.
    """
    if not window_key or nodes is None:
        return
    snaps = _snaps_from_nodes(nodes)
    with _lock:
        previous = _cache.get(window_key, [])
        remembered: list[RememberedField] = []
        for snap in snaps:
            sensitive = _directly_sensitive(snap)
            if not sensitive:
                sensitive = any(_same_field(snap, old) for old in previous)
            if sensitive:
                remembered.append(snap)
        _cache.pop(window_key, None)
        _cache[window_key] = remembered
        while len(_cache) > _MAX_WINDOWS:
            _cache.pop(next(iter(_cache)))


def remembered_rects(window_key: str) -> list[tuple[int, int, int, int]]:
    with _lock:
        return [field.bounds for field in _cache.get(window_key, [])]


def map_screen_rect(
    bounds: tuple[int, int, int, int],
    *,
    monitor: dict,
    image_width: int,
    image_height: int,
    allow_large: bool = False,
) -> tuple[int, int, int, int] | None:
    """Map a screen-space accessibility rectangle into this frame's pixels."""
    monitor_width = float(monitor.get("width") or image_width)
    monitor_height = float(monitor.get("height") or image_height)
    if monitor_width <= 0 or monitor_height <= 0:
        return None
    monitor_left = float(monitor.get("left", 0))
    monitor_top = float(monitor.get("top", 0))
    left, top, right, bottom = bounds
    box = (
        round((left - monitor_left) * image_width / monitor_width),
        round((top - monitor_top) * image_height / monitor_height),
        round((right - monitor_left) * image_width / monitor_width),
        round((bottom - monitor_top) * image_height / monitor_height),
    )
    x0 = max(0, min(image_width, box[0]))
    y0 = max(0, min(image_height, box[1]))
    x1 = max(0, min(image_width, box[2]))
    y1 = max(0, min(image_height, box[3]))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    # A single field is a small control. Document and window fallbacks are
    # allowed to cover the editor or the browser when that is the only way
    # to keep a secret out of the file.
    if not allow_large and (
        (y1 - y0) > image_height * 0.3
        or (x1 - x0) * (y1 - y0) > image_width * image_height * 0.2
    ):
        return None
    return x0, y0, x1, y1


def paint_screen_rects(
    img: Image.Image,
    monitor: dict,
    rects: list[tuple[int, int, int, int]],
    *,
    allow_large: bool = False,
) -> int:
    """Black out screen-space rectangles. Returns how many were painted."""
    if not rects:
        return 0
    draw = ImageDraw.Draw(img)
    painted = 0
    for bounds in rects:
        mapped = map_screen_rect(
            bounds,
            monitor=monitor,
            image_width=img.width,
            image_height=img.height,
            allow_large=allow_large,
        )
        if mapped is None:
            continue
        left, top, right, bottom = mapped
        pad = 3
        draw.rectangle(
            [
                max(0, left - pad),
                max(0, top - pad),
                min(img.width, right + pad),
                min(img.height, bottom + pad),
            ],
            fill=(0, 0, 0),
        )
        painted += 1
    return painted


def paint_remembered_fields(img: Image.Image, monitor: dict, window_key: str) -> int:
    """Black out remembered secret fields. Returns how many rectangles were painted."""
    rects = remembered_rects(window_key)
    if not rects:
        return 0
    draw = ImageDraw.Draw(img)
    painted = 0
    for bounds in rects:
        mapped = map_screen_rect(
            bounds,
            monitor=monitor,
            image_width=img.width,
            image_height=img.height,
        )
        if mapped is None:
            continue
        left, top, right, bottom = mapped
        pad = 3
        draw.rectangle(
            [
                max(0, left - pad),
                max(0, top - pad),
                min(img.width, right + pad),
                min(img.height, bottom + pad),
            ],
            fill=(0, 0, 0),
        )
        painted += 1
    return painted
