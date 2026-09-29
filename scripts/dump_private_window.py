"""Dump the screen's accessibility tree 10 seconds after Ctrl+Alt+1.

Windows has no UI Automation flag for a private browser window. Chrome and
Edge use the same window class either way, and the title is often just the
page name. This waits, then writes the visible windows plus the accessibility
tree, and lists any node whose name looks like a private-browsing label.

Run from the repo:

    python scripts\\dump_private_window.py

Focus the private window during the 10 seconds. Press Ctrl+C to stop.
"""

from __future__ import annotations

import datetime
import threading
from pathlib import Path

import win32gui
import win32process
import uiautomation as auto
from pynput import keyboard

OUT_DIR = Path(__file__).resolve().parents[1] / "private_a11y_dumps"
WAIT_SECONDS = 5
MAX_NODES = 1800
MAX_DEPTH = 22
NAME_LIMIT = 300
MAX_DEEP_WINDOWS = 4

# Labels browsers put on a private window. A bare "private" matches too much
# (privacy settings, private keys), so it is not in this list.
PRIVATE_MARKERS = (
    "incognito",
    "inprivate",
    "private browsing",
    "private window",
    "you've gone incognito",
    "you’ve gone incognito",
    "you're incognito",
    "you’re incognito",
    "this window is incognito",
    "inprivate browsing",
)

BROWSER_PROCESSES = {
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "vivaldi.exe",
    "arc.exe",
}

_busy = threading.Lock()


def bounds(control):
    try:
        rect = control.BoundingRectangle
        box = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        if box[2] > box[0] and box[3] > box[1]:
            return box
    except Exception:
        pass
    return None


def process_of(hwnd: int) -> tuple[int, str]:
    try:
        _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        return 0, ""
    try:
        import win32api

        handle = win32api.OpenProcess(0x1000, False, pid)
        path = win32process.GetModuleFileNameEx(handle, 0)
        return pid, Path(path).name
    except Exception:
        return pid, ""


def text_of(control, attr: str) -> str:
    try:
        return " ".join(str(getattr(control, attr) or "").split())
    except Exception:
        return ""


def legacy_bits(control) -> str:
    try:
        pattern = control.GetLegacyIAccessiblePattern()
    except Exception:
        return ""
    if pattern is None:
        return ""
    parts = []
    for label, reader in (
        ("legacy_name", lambda: pattern.Name),
        ("legacy_description", lambda: pattern.Description),
        ("legacy_value", lambda: pattern.Value),
    ):
        try:
            value = " ".join(str(reader() or "").split())
        except Exception:
            value = ""
        if value:
            parts.append(f"{label}={value[:NAME_LIMIT]!r}")
    return " ".join(parts)


def window_rows() -> list[dict]:
    rows = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ""
        try:
            class_name = win32gui.GetClassName(hwnd) or ""
        except Exception:
            class_name = ""
        if class_name in {"Progman", "WorkerW", "Shell_TrayWnd", "DV2ControlHost"}:
            return
        rect = win32gui.GetWindowRect(hwnd)
        area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
        if area < 40_000:
            return
        pid, process = process_of(hwnd)
        rows.append(
            {
                "hwnd": hwnd,
                "pid": pid,
                "process": process,
                "class_name": class_name,
                "title": title,
                "rect": rect,
            }
        )

    win32gui.EnumWindows(visit, None)
    return rows


def markers_in(*values: str) -> list[str]:
    blob = "\n".join(values).casefold()
    return [marker for marker in PRIVATE_MARKERS if marker in blob]


def walk_window(hwnd: int, lines: list[str], hits: list[str]) -> None:
    root = auto.ControlFromHandle(hwnd)
    if root is None:
        lines.append("uia: no control for this hwnd")
        return
    window_bits = [
        f"uia_name={text_of(root, 'Name')!r}",
        f"uia_class={text_of(root, 'ClassName')!r}",
        f"uia_automation_id={text_of(root, 'AutomationId')!r}",
        f"uia_help={text_of(root, 'HelpText')!r}",
        legacy_bits(root),
    ]
    lines.append("window_uia: " + " ".join(bit for bit in window_bits if bit))
    found = markers_in(text_of(root, "Name"), text_of(root, "HelpText"), legacy_bits(root))
    if found:
        hits.append(f"hwnd {hwnd} window: {', '.join(found)}")

    count = 0
    truncated = False

    def walk(control, depth: int) -> None:
        nonlocal count, truncated
        if count >= MAX_NODES or depth > MAX_DEPTH:
            truncated = True
            return
        count += 1
        name = text_of(control, "Name")
        class_name = text_of(control, "ClassName")
        automation_id = text_of(control, "AutomationId")
        try:
            kind = str(control.ControlTypeName or "")
        except Exception:
            kind = ""
        try:
            password = bool(getattr(control, "IsPassword", False))
        except Exception:
            password = False
        value = ""
        if not password:
            try:
                pattern = control.GetValuePattern()
                if pattern is not None and pattern.Value:
                    value = " ".join(str(pattern.Value).split())
            except Exception:
                value = ""
        matched = markers_in(name, class_name, automation_id, value)
        if matched:
            hits.append(
                f"hwnd {hwnd} {kind} bounds={bounds(control)} markers={matched} name={name[:120]!r}"
            )
        if len(name) > NAME_LIMIT:
            name = name[:NAME_LIMIT] + "..."
        extra = ""
        if class_name:
            extra += f" class={class_name!r}"
        if automation_id:
            extra += f" id={automation_id!r}"
        if value:
            extra += f" value={value[:NAME_LIMIT]!r}"
        try:
            kids = list(control.GetChildren())
        except Exception:
            kids = []
        lines.append(
            f"{'  ' * depth}{kind} bounds={bounds(control)} children={len(kids)} name={name!r}{extra}"
        )
        for child in kids:
            walk(child, depth + 1)

    walk(root, 0)
    lines.append(f"nodes_written: {count}  walk_stopped_early: {truncated}")


def capture() -> Path:
    foreground = win32gui.GetForegroundWindow()
    rows = window_rows()
    deep = []
    for row in rows:
        if row["hwnd"] == foreground or row["process"].lower() in BROWSER_PROCESSES:
            deep.append(row)
        if len(deep) >= MAX_DEEP_WINDOWS and any(item["hwnd"] == foreground for item in deep):
            break
    if not any(item["hwnd"] == foreground for item in deep):
        pid, process = process_of(foreground)
        deep.insert(
            0,
            {
                "hwnd": foreground,
                "pid": pid,
                "process": process,
                "class_name": win32gui.GetClassName(foreground) if foreground else "",
                "title": win32gui.GetWindowText(foreground) if foreground else "",
                "rect": win32gui.GetWindowRect(foreground) if foreground else None,
            },
        )
    deep = deep[:MAX_DEEP_WINDOWS]

    lines = [
        f"captured_at: {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"foreground_hwnd: {foreground}",
        "",
        "visible_windows:",
    ]
    for row in rows:
        mark = " foreground" if row["hwnd"] == foreground else ""
        lines.append(
            f"  hwnd={row['hwnd']} pid={row['pid']} process={row['process']!r} "
            f"class={row['class_name']!r} rect={row['rect']} title={row['title']!r}{mark}"
        )
    hits: list[str] = []
    for row in deep:
        lines.extend(
            [
                "",
                f"=== tree hwnd={row['hwnd']} process={row['process']!r} "
                f"class={row['class_name']!r} title={row['title']!r} ===",
            ]
        )
        walk_window(row["hwnd"], lines, hits)
    lines.extend(["", "private_markers:"])
    lines.extend(f"  {hit}" for hit in hits)
    if not hits:
        lines.append("  none")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUT_DIR / f"{stamp}.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def dump_after_wait() -> None:
    if not _busy.acquire(blocking=False):
        print("a dump is already waiting")
        return

    def run() -> None:
        try:
            print(f"Ctrl+Alt+1 seen. Dumping the screen in {WAIT_SECONDS} seconds. Focus the private window.")
            threading.Event().wait(WAIT_SECONDS)
            print("reading UI Automation...")
            with auto.UIAutomationInitializerInThread():
                path = capture()
            print(f"wrote {path}")
        except Exception as exc:
            print(f"dump failed: {exc}")
        finally:
            _busy.release()

    threading.Thread(target=run, daemon=True).start()


def main() -> None:
    print("Listening for Ctrl+Alt+1. The dump runs 10 seconds later. Ctrl+C stops this.")
    with keyboard.GlobalHotKeys({"<ctrl>+<alt>+1": dump_after_wait}) as listener:
        listener.join()


if __name__ == "__main__":
    main()
