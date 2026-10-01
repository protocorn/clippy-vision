"""Privacy / access-control settings for screenshot redaction.

When a target is enabled, matching windows are blacked out in captures
(same path as always-redacting the Clippy Vision window).
"""
from __future__ import annotations

import json
import time
from typing import Optional

try:
    from core.storage import conn
except ImportError:
    from storage import conn

_META_KEY = "settings.privacy_redact"



# Curated targets shown in Settings → Access control.
# Match by process name and/or case-insensitive window title substrings.
PRIVACY_TARGETS: list[dict] = [
    {
        "id": "whatsapp",
        "label": "WhatsApp",
        "description": "Black out WhatsApp Desktop and browser tabs",
        "processes": ["whatsapp.exe", "whatsapp"],
        "title_patterns": ["whatsapp"],
    },
    {
        "id": "instagram",
        "label": "Instagram",
        "description": "Black out Instagram Desktop and browser tabs",
        "processes": ["instagram.exe", "instagram"],
        "title_patterns": ["instagram"],
    },
    {
        "id": "telegram",
        "label": "Telegram",
        "description": "Black out Telegram Desktop windows",
        "processes": ["telegram.exe", "telegramdesktop.exe", "telegram"],
        "title_patterns": ["telegram"],
    },
    {
        "id": "signal",
        "label": "Signal",
        "description": "Black out Signal Desktop windows",
        "processes": ["signal.exe", "signal"],
        "title_patterns": ["signal"],
    },
    {
        "id": "discord",
        "label": "Discord",
        "description": "Black out Discord windows",
        "processes": ["discord.exe", "discord"],
        "title_patterns": ["discord"],
    },
    {
        "id": "slack",
        "label": "Slack",
        "description": "Black out Slack windows",
        "processes": ["slack.exe", "slack"],
        "title_patterns": ["slack"],
    },
    {
        "id": "messages",
        "label": "Messages / SMS apps",
        "description": "Black out common messaging apps (Your Phone, Messages)",
        "processes": ["yourphone.exe", "phonelink.exe", "messages", "message"],
        "title_patterns": ["phone link", "your phone", "messages"],
    },
    {
        "id": "private_browsing",
        "label": "Private browsing",
        "description": (
            "On by default. Blacks out private windows in Google Chrome, "
            "Microsoft Edge, and Brave, on Windows and macOS."
        ),
        "processes": [],
        "title_patterns": [],
        "default": True,
    },
]

_TARGET_IDS = {t["id"] for t in PRIVACY_TARGETS}



# Always redact Clippy Vision itself (never user-toggleable).
# electron.exe = npm start; packaged builds use the product exe name.
ALWAYS_REDACT_PROCESSES = frozenset({
    "clippy vision.exe",
    "clippy-vision.exe",
    "clippy vision",
})
ALWAYS_REDACT_TITLE_PATTERNS = ("clippy vision",)


def _default_enabled() -> dict[str, bool]:
    return {t["id"]: bool(t.get("default", False)) for t in PRIVACY_TARGETS}


def get_privacy_enabled() -> dict[str, bool]:
    """Return {target_id: enabled} for every known target."""
    defaults = _default_enabled()
    row = conn.execute(
        "SELECT value FROM memory_meta WHERE key = ?",
        (_META_KEY,),
    ).fetchone()
    if not row:
        return defaults
    try:
        stored = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return defaults
    if not isinstance(stored, dict):
        return defaults
    for tid in defaults:
        if tid in stored:
            defaults[tid] = bool(stored[tid])
    return defaults


def set_privacy_enabled(enabled: dict[str, bool]) -> dict[str, bool]:
    """Merge and persist enabled flags. Unknown keys ignored."""
    global _cache_rules, _cache_at
    current = get_privacy_enabled()
    for tid, val in (enabled or {}).items():
        if tid in _TARGET_IDS:
            current[tid] = bool(val)
    conn.execute(
        "INSERT OR REPLACE INTO memory_meta (key, value) VALUES (?, ?)",
        (_META_KEY, json.dumps(current)),
    )
    conn.commit()


    _cache_rules = None
    _cache_at = 0.0
    return current


def list_privacy_targets() -> list[dict]:
    """Targets for the settings UI, with current enabled state."""
    enabled = get_privacy_enabled()
    return [
        {
            "id": t["id"],
            "label": t["label"],
            "description": t["description"],
            "enabled": bool(enabled.get(t["id"], False)),
        }
        for t in PRIVACY_TARGETS
    ]


def get_active_redact_rules() -> dict:
    """Rules used by vision capture (always-redact + user-enabled targets).

    Returns:
      {
        "processes": set[str],
        "title_patterns": tuple[str],
      }
    """
    processes: set[str] = set(ALWAYS_REDACT_PROCESSES)
    titles: list[str] = list(ALWAYS_REDACT_TITLE_PATTERNS)

    enabled = get_privacy_enabled()
    for target in PRIVACY_TARGETS:
        if not enabled.get(target["id"]):
            continue
        for p in target.get("processes") or []:
            processes.add(p.lower())
        for pat in target.get("title_patterns") or []:
            titles.append(pat.lower())


    # Dedupe titles while preserving order
    seen: set[str] = set()
    unique_titles: list[str] = []
    for t in titles:
        if t not in seen:
            seen.add(t)
            unique_titles.append(t)

    return {
        "processes": processes,
        "title_patterns": tuple(unique_titles),
        "private_browsing": bool(enabled.get("private_browsing", True)),
    }




# ---- Cached rules for the capture process (avoids DB hit every EnumWindows) ----
_cache_rules: dict | None = None
_cache_at: float = 0.0
_CACHE_TTL_SECS = 5.0


def get_active_redact_rules_cached() -> dict:
    global _cache_rules, _cache_at
    now = time.time()
    if _cache_rules is None or (now - _cache_at) >= _CACHE_TTL_SECS:
        _cache_rules = get_active_redact_rules()
        _cache_at = now
    return _cache_rules


def _process_matches(process_name: str, saved_names: set[str]) -> bool:
    from core.process_names import process_key

    key = process_key(process_name)
    if not key:
        return False
    return any(process_key(name) == key for name in saved_names)


def private_browsing_redaction_enabled() -> bool:
    """True unless the user turned off the private-browsing toggle."""
    rules = get_active_redact_rules_cached()
    return bool(rules.get("private_browsing", True))


def is_clippy_window(process_name: str, window_title: str) -> bool:
    """True if this is the Clippy Vision app window (always-redact target)."""
    title = (window_title or "").lower()
    if _process_matches(process_name, set(ALWAYS_REDACT_PROCESSES)):
        return True
    return any(pat in title for pat in ALWAYS_REDACT_TITLE_PATTERNS if pat)


def dev_electron_is_clippy(process_name: str, executable: str = "") -> bool:
    """True for ``npm start``, which runs as electron.exe inside this repo.

    The packaged app is ``Clippy Vision.exe`` and matches ``is_clippy_window``
    by process name. A bare electron.exe is also used by other apps, so the
    executable path has to be this project's.
    """
    from core.process_names import process_key

    if process_key(process_name) != "electron":
        return False
    path = (executable or "").replace("/", "\\").casefold()
    return any(token in path for token in ("clippy vision", "clippy_vision", "clippy-vision"))


def hides_screen_text(process_name: str, window_title: str, executable: str = "") -> bool:
    """True when this window's accessibility text must not be stored.

    The screenshot is already painted black. The tree walk is a separate
    copy of the same window and has to stop here too.
    """
    if is_clippy_window(process_name, window_title) or dev_electron_is_clippy(process_name, executable):
        return True
    return should_redact_window(process_name, window_title)


def should_redact_window(process_name: str, window_title: str) -> bool:
    """True if this window should be blacked out in screenshots.

    Clippy Vision itself is included here; callers that need foreground-only
    behavior for Clippy should use is_clippy_window() separately.
    A Windows file name and the macOS application name match as the same app.
    """
    rules = get_active_redact_rules_cached()
    title = (window_title or "").lower()
    if _process_matches(process_name, rules["processes"]):
        return True
    for pat in rules["title_patterns"]:
        if pat and pat in title:
            return True
    return False
