"""Tiny cross-process state file for desktop and browser status indicators."""

from __future__ import annotations

import json
import time
from pathlib import Path

try:
    from core.paths import get_data_dir
except ImportError:
    from paths import get_data_dir


def _state_path() -> Path:
    # Electron and the browser-facing API read this small file without opening
    # the capture process, so it must live beside the rest of user data.
    return get_data_dir() / "capture_status.json"


def set_capture_status(active: bool, pid: int | None = None) -> dict:
    payload = {
        "active": bool(active),
        "pid": pid,
        "updated_at": time.time(),
    }
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    try:
        # Replace atomically so readers never observe a partially-written JSON
        # document while the heartbeat updates the status.
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
    return payload


def get_capture_status() -> dict:
    path = _state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        updated_at = float(payload.get("updated_at") or 0)

        # A stale heartbeat means the process exited without getting a chance
        # to run its normal shutdown hook.
        active = bool(payload.get("active")) and (time.time() - updated_at) < 15 * 60
        return {"active": active, "pid": payload.get("pid"), "updated_at": updated_at}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"active": False, "pid": None, "updated_at": None}
