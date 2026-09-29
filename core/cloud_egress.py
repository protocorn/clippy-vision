"""Last filter on text handed to a cloud MCP client.

Capture already paints fields and scrubs the stored frame. This pass runs
on the tool result only. It reuses the same secret catalog, drops a
screenshot file path, and replaces the body of a privacy-listed window
with a one-line note. The database is not modified.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.paths import get_screenshots_dir
from core.privacy_settings import should_redact_window
from core.secret_patterns import redact_secrets

_PRIVATE_NOTE = "This moment was private."
_CONTENT_KEYS = frozenset({
    "summary",
    "ocr_preview",
    "ocr_text",
    "vision_ocr_text",
    "payload",
    "vision_activity",
    "active_url",
    "title",
    "current_window_title",
    "interest_reason",
    "active_task",
    "entities",
    "pasted_content",
    "clipboard_content",
    "text",
    "content",
    "filename",
})
_KEEP_PROSE_PREFIXES = ("time:", "event_type:", "process_name:")


def redact_cloud_result(payload: str) -> str:
    """Return a cloud-safe copy of a tool result. Raises if scrubbing fails."""
    if payload is None:
        return ""
    text = payload if isinstance(payload, str) else str(payload)
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        parsed = json.loads(text)
        scrubbed, _private = _scrub(parsed)
        return json.dumps(scrubbed, default=str, ensure_ascii=False)
    return _scrub_prose(text)


def _scrub(value):
    if isinstance(value, str):
        return _scrub_text(value), False
    if isinstance(value, list):
        items = []
        private = False
        for item in value:
            scrubbed, item_private = _scrub(item)
            items.append(scrubbed)
            private = private or item_private
        return items, private
    if isinstance(value, dict):
        private = _record_is_private(value)
        out: dict = {}
        child_private = False
        for key, item in value.items():
            if key == "path" and isinstance(item, str) and _is_screenshot_path(item):
                out[key] = None
                continue
            scrubbed, nested_private = _scrub(item)
            out[key] = scrubbed
            child_private = child_private or nested_private
        if private or (child_private and _has_capture_body(out)):
            out = _blank_private(out)
        return out, private or child_private
    return value, False


def _scrub_text(text: str) -> str:
    return _strip_screenshot_paths(redact_secrets(text))


def _scrub_prose(text: str) -> str:
    parts = re.split(r"\n---\n", text)
    scrubbed: list[str] = []
    for part in parts:
        process, title = _prose_window(part)
        if process and should_redact_window(process, title):
            kept = [
                line
                for line in part.splitlines()
                if line.startswith(_KEEP_PROSE_PREFIXES)
            ]
            kept.append(f"private: {_PRIVATE_NOTE}")
            scrubbed.append("\n".join(kept))
        else:
            scrubbed.append(part)
    return _scrub_text("\n---\n".join(scrubbed))


def _prose_window(part: str) -> tuple[str, str]:
    process = ""
    title = ""
    for match in re.finditer(
        r"^(process_name|current_window_title):\s*(.*)$",
        part,
        re.MULTILINE,
    ):
        if match.group(1) == "process_name":
            process = match.group(2).strip()
        else:
            title = match.group(2).strip()
    return process, title


def _record_is_private(obj: dict) -> bool:
    if "process_name" not in obj and "process" not in obj:
        return False
    process = str(obj.get("process_name") or obj.get("process") or "")
    title = str(obj.get("current_window_title") or obj.get("title") or "")
    return should_redact_window(process, title)


def _has_capture_body(obj: dict) -> bool:
    return any(key in obj for key in ("ocr_text", "vision_ocr_text", "summary", "payload", "path"))


def _blank_private(obj: dict) -> dict:
    blanked = dict(obj)
    for key in list(blanked):
        if key in _CONTENT_KEYS or key == "path":
            blanked[key] = None
    if "image_available" in blanked:
        blanked["image_available"] = False
    blanked["private"] = True
    blanked["note"] = _PRIVATE_NOTE
    return blanked


def _is_screenshot_path(value: str) -> bool:
    try:
        path = Path(value)
    except (TypeError, ValueError):
        return False
    if not path.is_absolute():
        return False
    try:
        path.resolve().relative_to(get_screenshots_dir().resolve())
    except (OSError, ValueError):
        return False
    return True


def _strip_screenshot_paths(text: str) -> str:
    if not text:
        return text or ""
    root = get_screenshots_dir().resolve()
    needles = {
        str(root),
        str(root).replace("\\", "/"),
        str(root).replace("\\", "\\\\"),
    }
    out = text
    for needle in sorted(needles, key=len, reverse=True):
        if not needle:
            continue
        pattern = re.escape(needle) + r"(?:[\\/][^\\/\s\"']+)?"
        out = re.sub(pattern, "[screenshot]", out, flags=re.IGNORECASE)
    return out
