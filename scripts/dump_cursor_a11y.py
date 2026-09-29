"""Write the live Cursor accessibility tree to cursor_a11y_tree.txt."""

from __future__ import annotations

import datetime
from pathlib import Path

import win32gui
import uiautomation as auto

OUT = Path(__file__).resolve().parents[1] / "cursor_a11y_tree.txt"
MAX_NODES = 4000
MAX_DEPTH = 28
NAME_LIMIT = 400


def bounds(control):
    try:
        rect = control.BoundingRectangle
        left, top = int(rect.left), int(rect.top)
        right, bottom = int(rect.right), int(rect.bottom)
        if right > left and bottom > top:
            return left, top, right, bottom
    except Exception:
        pass
    return None


def ctype(control) -> str:
    try:
        return str(control.ControlTypeName or "")
    except Exception:
        return ""


def find_cursor():
    foreground = win32gui.GetForegroundWindow()
    found = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ""
        if "Cursor" not in title:
            return
        rect = win32gui.GetWindowRect(hwnd)
        area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
        found.append((hwnd == foreground, area, hwnd, title, rect))

    win32gui.EnumWindows(visit, None)
    if not found:
        return None
    found.sort(reverse=True)
    return found[0]


def main() -> None:
    target = find_cursor()
    if target is None:
        raise SystemExit("No visible Cursor window")
    is_foreground, _area, hwnd, title, rect = target
    root = auto.ControlFromHandle(hwnd)
    lines: list[str] = [
        f"captured_at: {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"foreground: {is_foreground}",
        f"hwnd: {hwnd}",
        f"title: {title}",
        f"window_rect: {rect}",
        f"node_limit: {MAX_NODES}  depth_limit: {MAX_DEPTH}",
        "",
    ]
    count = 0
    truncated = False
    documents = []

    def walk(control, depth: int) -> None:
        nonlocal count, truncated
        if count >= MAX_NODES or depth > MAX_DEPTH:
            truncated = True
            return
        count += 1
        try:
            name = str(control.Name or "")
        except Exception:
            name = ""
        name = " ".join(name.split())
        if len(name) > NAME_LIMIT:
            name = name[:NAME_LIMIT] + "..."
        value = ""
        try:
            if not bool(getattr(control, "IsPassword", False)):
                pattern = control.GetValuePattern()
                if pattern is not None and pattern.Value:
                    value = " ".join(str(pattern.Value).split())
        except Exception:
            value = ""
        if len(value) > NAME_LIMIT:
            value = value[:NAME_LIMIT] + "..."
        kind = ctype(control)
        try:
            kids = list(control.GetChildren())
        except Exception:
            kids = []
        extra = f" value={value!r}" if value else ""
        lines.append(
            f"{'  ' * depth}{kind} bounds={bounds(control)} children={len(kids)} name={name!r}{extra}"
        )
        if kind == "DocumentControl":
            documents.append(control)
        for child in kids:
            walk(child, depth + 1)

    walk(root, 0)
    lines.extend(
        [
            "",
            f"nodes_written: {count}",
            f"walk_stopped_early: {truncated}",
            "",
            "document_text:",
        ]
    )
    for index, doc in enumerate(documents, start=1):
        raw = ""
        try:
            pattern = doc.GetTextPattern()
            if pattern is not None and pattern.DocumentRange is not None:
                raw = pattern.DocumentRange.GetText(12000) or ""
        except Exception as exc:
            raw = f"<unreadable: {exc}>"
        lines.append(f"--- document {index} bounds={bounds(doc)} chars={len(raw)} ---")
        lines.append(raw)
        lines.append("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} nodes={count} documents={len(documents)} bytes={OUT.stat().st_size}")


if __name__ == "__main__":
    main()
