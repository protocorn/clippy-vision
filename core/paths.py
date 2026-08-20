"""Resolve writable data paths for Clippy Vision.

Dev (default):  <repo>/core/data/
Packaged:       set CLIPPY_DATA_DIR to %APPDATA%/Clippy Vision/data
                (Electron main.js sets this when spawning Python).
"""

from __future__ import annotations

import os
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent


def get_data_dir() -> Path:
    env = (os.environ.get("CLIPPY_DATA_DIR") or "").strip()
    if env:
        p = Path(env)
    else:
        p = _CORE_DIR / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_screenshots_dir() -> Path:
    p = get_data_dir() / "screenshots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_db_path() -> Path:
    return get_data_dir() / "events.db"


def get_baseline_path() -> Path:
    return get_data_dir() / "baseline.json"
