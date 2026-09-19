"""Resolve writable data paths for Clippy Vision.

Dev (default):  <repo>/core/data/
Packaged:       set CLIPPY_DATA_DIR to %APPDATA%/Clippy Vision/data
                (Electron main.js sets this when spawning Python).

Safety net: if some test module is imported under pytest without setting
CLIPPY_DATA_DIR (e.g. a stray bench/test_*.py collected by a bare `pytest`
invocation, or a forgotten isolation guard in a future test file), silently
falling back to the real dev data dir would let destructive test code
(DELETE FROM events/sessions, synthetic event inserts, etc.) mutate the
user's real activity history. This has happened before — see
tests/test_runtime_regressions.py's own CLIPPY_DATA_DIR guard, which does
not protect files that import core.storage before it runs. Detect pytest
and force an isolated temp dir here instead, independent of any per-file
discipline.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent
_pytest_fallback_dir: str | None = None


def _running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def get_data_dir() -> Path:
    global _pytest_fallback_dir

    env = (os.environ.get("CLIPPY_DATA_DIR") or "").strip()
    if env:
        p = Path(env)
    elif _running_under_pytest():
        # No explicit CLIPPY_DATA_DIR under pytest — never touch the real
        # dev data dir. Reuse one temp dir for the whole process so repeated
        # calls within a test session stay consistent.
        if _pytest_fallback_dir is None:
            _pytest_fallback_dir = tempfile.mkdtemp(prefix="clippy-pytest-fallback-")
            print(
                f"[paths] WARNING — pytest detected with no CLIPPY_DATA_DIR set; "
                f"using isolated temp dir instead of the real data dir: {_pytest_fallback_dir}"
            )
        p = Path(_pytest_fallback_dir)
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
