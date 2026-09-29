"""One name for an app on Windows and macOS.

Windows reports a file name (``chrome.exe``). macOS reports the application
name (``Google Chrome``). Settings store whichever the user typed, so both
have to mean the same app.
"""

from __future__ import annotations

# Stem after removing ``.exe``, mapped to a shared key.
_ALIASES = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "msedge": "edge",
    "microsoft edge": "edge",
    "brave": "brave",
    "brave browser": "brave",
    "firefox": "firefox",
    "mozilla firefox": "firefox",
    "telegram": "telegram",
    "telegramdesktop": "telegram",
    "whatsapp": "whatsapp",
    "discord": "discord",
    "slack": "slack",
    "signal": "signal",
    "instagram": "instagram",
    "cursor": "cursor",
    "clippy vision": "clippy",
    "clippy-vision": "clippy",
}


def process_key(process_name: str) -> str:
    """Lowercase identity used to compare a live process with a saved name."""
    name = (process_name or "").strip().lower()
    if name.endswith(".exe"):
        name = name[:-4].strip()
    return _ALIASES.get(name, name)
