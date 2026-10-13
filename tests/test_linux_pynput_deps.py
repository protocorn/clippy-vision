"""Linux pynput must not require compiling evdev."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"


def _requirement_lines() -> list[str]:
    return [
        line.strip()
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_requirements_keep_pynput_off_linux_marker():
    lines = _requirement_lines()
    pynput_lines = [line for line in lines if line.startswith("pynput")]
    assert pynput_lines, "expected a platform-marked pynput requirement"
    assert any(
        'platform_system != "Linux"' in line or "platform_system != 'Linux'" in line
        for line in pynput_lines
    )
    assert any(line.startswith("python-xlib") and "Linux" in line for line in lines)
    assert any(line.startswith("six") and "Linux" in line for line in lines)
    assert not any(line == "pynput" for line in lines), "bare pynput would pull evdev on Linux"


@pytest.mark.linux
def test_pynput_imports_with_xlib_backend_without_evdev():
    """CI installs pynput with --no-deps; the X11 backend should still import."""
    assert importlib.util.find_spec("Xlib") is not None, "python-xlib should be installed on Linux"
    assert importlib.util.find_spec("six") is not None
    # Intentionally do not require evdev — that is the whole point of --no-deps.
    assert importlib.util.find_spec("evdev") is None

    from pynput import keyboard

    assert keyboard.Listener is not None
