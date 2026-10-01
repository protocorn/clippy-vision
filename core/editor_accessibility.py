"""Turn on screen-reader text for VS Code and editors built from it.

Monaco hides the open file from the accessibility tree unless
``editor.accessibilitySupport`` is ``"on"``. ``"auto"`` only exposes the file
when the operating system reports a screen reader, which Clippy is not.
The Shift+Alt+F1 shortcut is Cursor and VS Code only, so this writes the
setting those editors already watch.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

try:
    from core.platform_support import IS_MACOS, IS_WINDOWS
    from core.process_names import process_key
except ImportError:
    from platform_support import IS_MACOS, IS_WINDOWS
    from process_names import process_key


# Process stem -> the app's user-data folder under APPDATA or Application Support.
_APP_DIRS = {
    "cursor": "Cursor",
    "code": "Code",
    "visual studio code": "Code",
    "code - insiders": "Code - Insiders",
    "code insiders": "Code - Insiders",
    "vscodium": "VSCodium",
    "vs codium": "VSCodium",
    "codium": "VSCodium",
    "windsurf": "Windsurf",
    "positron": "Positron",
    "trae": "Trae",
    "void": "Void",
    "kiro": "Kiro",
}

_SETTING_RE = re.compile(
    r'("editor\.accessibilitySupport"\s*:\s*")(on|off|auto)(")',
    re.IGNORECASE,
)


def app_dir_name(process_name: str) -> str | None:
    """User-data folder for a VS Code-family process, or None for other apps."""
    return _APP_DIRS.get(process_key(process_name))


def user_settings_path(process_name: str, *, root: Path | None = None) -> Path | None:
    """Path of the editor's user settings.json when that app is installed."""
    folder = app_dir_name(process_name)
    if not folder:
        return None
    base = root if root is not None else _support_root()
    if base is None:
        return None
    app_dir = base / folder
    if not app_dir.is_dir():
        return None
    return app_dir / "User" / "settings.json"


def ensure_screen_reader_support(process_name: str, *, root: Path | None = None) -> bool:
    """Set the editor's accessibility support to on. Returns True when a file changed.

    A project ``.vscode/settings.json`` is updated only when it already sets
    this key. A missing project key leaves the user setting in effect, and
    writing one would put the change in the user's repository.
    """
    changed = False
    user_path = user_settings_path(process_name, root=root)
    if user_path is not None and _ensure_on(user_path, insert=True):
        changed = True
        print(f"[capture] turned on screen reader accessibility for {process_name}")
    if root is None:
        for path in _workspace_settings_paths():
            if _ensure_on(path, insert=False):
                changed = True
                print(f"[capture] turned on screen reader accessibility in {path}")
    return changed


def _support_root() -> Path | None:
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        return Path(appdata) if appdata else None
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support"
    return None


def _ensure_on(path: Path, *, insert: bool) -> bool:
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return False
    if not text and not insert:
        return False
    updated, state = _with_support_on(text)
    if state == "on" or (state == "missing" and not insert):
        return False
    if state == "missing":
        updated = _insert_setting(text)
    if updated == text:
        return False
    return _write_text(path, updated)


def _with_support_on(text: str) -> tuple[str, str]:
    """Replace an existing value. State is ``on``, ``changed``, or ``missing``."""
    in_block = False
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            in_block = "*/" not in stripped[2:]
            continue
        if stripped.startswith(("//", "*")):
            continue
        match = _SETTING_RE.search(line)
        if match is None:
            continue
        if match.group(2).lower() == "on":
            return text, "on"
        lines[index] = line[: match.start(2)] + "on" + line[match.end(2) :]
        return "".join(lines), "changed"
    return text, "missing"


def _insert_setting(text: str) -> str:
    stripped = text.rstrip()
    newline = "\n" if text.endswith("\n") or not stripped else ""
    if not stripped:
        return '{\n    "editor.accessibilitySupport": "on"\n}\n'
    end = stripped.rfind("}")
    if end < 0:
        return text
    head = stripped[:end].rstrip()
    if head.endswith("{") or head.endswith(","):
        comma = ""
    else:
        comma = ","
    body = f'{head}{comma}\n    "editor.accessibilitySupport": "on"\n'
    return body + stripped[end:] + newline


def _write_text(path: Path, text: str) -> bool:
    temporary = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _workspace_settings_paths() -> list[Path]:
    """Project settings of the foreground editor, when its command line names a folder."""
    paths: list[Path] = []
    for argument in _foreground_command_args():
        if argument.startswith("-"):
            continue
        folder = Path(argument)
        try:
            settings = folder / ".vscode" / "settings.json"
            if folder.is_dir() and settings.is_file():
                paths.append(settings)
        except OSError:
            continue
    return paths


def _foreground_command_args() -> list[str]:
    try:
        import psutil
    except ImportError:
        return []
    try:
        if IS_WINDOWS:
            import win32gui
            import win32process

            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return []
            _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
        elif IS_MACOS:
            return []
        else:
            return []
        if not pid:
            return []
        return [str(part) for part in psutil.Process(pid).cmdline()]
    except Exception:
        return []
