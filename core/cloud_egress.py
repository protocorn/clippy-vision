"""Last filter on text handed to a cloud MCP client.

Capture already paints fields and scrubs the stored frame. This pass runs
on the tool result only. It reuses the same secret catalog, drops a
screenshot file path, and replaces the body of a privacy-listed window
with a one-line note.

Summaries, clipboard text, and memory lines often no longer name a window.
Those stay in the reply only when the stored row is known to be clean.
A session that has no private flag yet may get one written here. The
summary text itself is not rewritten in the database.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.cloud_provenance import (
    cluster_label_is_clean,
    event_is_private,
    fact_is_stored,
    fact_text_is_clean,
    span_is_clean,
    summary_text_is_clean,
)
from core.paths import get_screenshots_dir
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
    "url",
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
_UNATTRIBUTED_KEYS = frozenset({
    "summary",
    "active_task",
    "entities",
    "payload",
    "pasted_content",
    "clipboard_content",
    "ocr_text",
    "vision_ocr_text",
    "ocr_preview",
    "vision_activity",
    "active_url",
    "url",
    "interest_reason",
})
_KEEP_PROSE_PREFIXES = ("time:", "event_type:", "process_name:")
_BODY_PREFIXES = (
    "summary:",
    "active_task:",
    "entities:",
    "payload:",
    "vision_ocr_text:",
    "vision_activity:",
    "active_url:",
    "ocr_preview:",
    "ocr_text:",
    "pasted_content:",
    "clipboard_content:",
    "interest_reason:",
)
_FIELD_HEADS = _BODY_PREFIXES + (
    "time:",
    "event_type:",
    "process_name:",
    "current_window_title:",
    "private:",
)
_CLUSTER_LINE = re.compile(
    r"^(?P<indent>\s*)\[(?P<label>[^\]]+)\]\s+(?P<body>.+?)"
    r"(?:\s+\((?P<count>\d+) facts, freshness=(?P<fresh>[\d.]+)\))?\s*$"
)
_FACT_BULLET = re.compile(
    r"^(?P<prefix>\s*-\s*(?:\([\d.]+\)\s+)?)(?P<text>\S.*)$"
)


def redact_cloud_result(payload: str) -> str:
    """Return a cloud-safe copy of a tool result. Raises if scrubbing fails."""
    text, _blanked = prepare_cloud_result(payload)
    return text


def prepare_cloud_result(payload: str) -> tuple[str, bool]:
    """Return the scrubbed reply and whether a private body was removed."""
    if payload is None:
        return "", False
    text = payload if isinstance(payload, str) else str(payload)
    state = {"blanked": False}
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # A memory line can start with "[label]". A broken object still
            # withholds, because prose scanning would miss nested fields.
            if stripped.startswith("{"):
                raise
        else:
            scrubbed, _private = _scrub(parsed, state)
            return json.dumps(scrubbed, default=str, ensure_ascii=False), bool(state["blanked"])
    return _scrub_prose(text, state), bool(state["blanked"])


def _scrub(value, state: dict):
    if isinstance(value, str):
        return _scrub_text(value), False
    if isinstance(value, list):
        items = []
        private = False
        for item in value:
            scrubbed, item_private = _scrub(item, state)
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
            scrubbed, nested_private = _scrub(item, state)
            out[key] = scrubbed
            child_private = child_private or nested_private
        drop = private or (child_private and _has_capture_body(out)) or _unattributed_capture(out)
        if drop:
            out = _blank_private(out)
            state["blanked"] = True
        return out, private or child_private or drop
    return value, False


def _scrub_text(text: str) -> str:
    return _strip_screenshot_paths(redact_secrets(text))


def _scrub_prose(text: str, state: dict) -> str:
    parts = re.split(r"\n---\n", text)
    scrubbed: list[str] = []
    for part in parts:
        if _capture_block(part):
            if _prose_should_blank(part):
                state["blanked"] = True
                scrubbed.append(_blank_prose_body(part))
            else:
                scrubbed.append(part)
            continue
        lines = [_scrub_memory_line(line, state) for line in part.splitlines()]
        scrubbed.append("\n".join(lines))
    return _scrub_text("\n---\n".join(scrubbed))


def _capture_block(part: str) -> bool:
    for line in part.splitlines():
        if line.startswith(("time:", "event_type:", "summary:", "process_name:")):
            return True
    return False


def _prose_has_body(part: str) -> bool:
    for line in part.splitlines():
        if line.lower().startswith(_BODY_PREFIXES):
            return True
    return False


def _prose_should_blank(part: str) -> bool:
    process, title = _prose_window(part)
    named = bool(process) or title.strip().casefold() == "private window"
    if named:
        if event_is_private(process, title):
            return True
        if process:
            return False
    if not _prose_has_body(part):
        return False
    summary = _field(part, "summary")
    if summary:
        return not summary_text_is_clean(summary)
    return True


def _blank_prose_body(part: str) -> str:
    kept = [
        line
        for line in part.splitlines()
        if line.startswith(_KEEP_PROSE_PREFIXES)
    ]
    kept.append(f"private: {_PRIVATE_NOTE}")
    return "\n".join(kept)


def _field(part: str, name: str) -> str:
    prefix = name + ":"
    buf: list[str] = []
    collecting = False
    for line in part.splitlines():
        if line.startswith(prefix):
            collecting = True
            buf = [line[len(prefix):].strip()]
            continue
        if not collecting:
            continue
        if line.lower().startswith(_FIELD_HEADS):
            break
        buf.append(line)
    return "\n".join(buf).strip()


def _scrub_memory_line(line: str, state: dict) -> str:
    cluster = _CLUSTER_LINE.match(line)
    if cluster and cluster.group("body"):
        if cluster_label_is_clean(cluster.group("label")):
            return line
        state["blanked"] = True
        suffix = ""
        if cluster.group("count") is not None:
            suffix = (
                f" ({cluster.group('count')} facts, freshness={cluster.group('fresh')})"
            )
        return f"{cluster.group('indent')}[{cluster.group('label')}] {_PRIVATE_NOTE}{suffix}"
    bullet = _FACT_BULLET.match(line)
    if bullet:
        text = bullet.group("text").strip()
        if fact_is_stored(text) and not fact_text_is_clean(text):
            state["blanked"] = True
            return f"{bullet.group('prefix')}{_PRIVATE_NOTE}"
    return line


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


def _named_process(obj: dict) -> str:
    if "process_name" not in obj and "process" not in obj:
        return ""
    return str(obj.get("process_name") or obj.get("process") or "").strip()


def _record_is_private(obj: dict) -> bool:
    title = str(obj.get("current_window_title") or obj.get("title") or "")
    process = _named_process(obj)
    if not process and title.strip().casefold() != "private window":
        return False
    return event_is_private(process, title)


def _unattributed_capture(obj: dict) -> bool:
    """True when this record has screen text and no clean window to blame."""
    if _named_process(obj) or _record_is_private(obj):
        return False
    if not any(key in obj for key in _UNATTRIBUTED_KEYS):
        return False
    start = obj.get("window_start")
    end = obj.get("window_end")
    if start is not None and end is not None:
        return not span_is_clean(start, end)
    summary = obj.get("summary")
    if isinstance(summary, str) and summary.strip():
        return not summary_text_is_clean(summary)
    return True


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
