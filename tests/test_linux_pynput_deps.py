"""Linux pynput must not require compiling evdev."""

from __future__ import annotations

import importlib.metadata
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
def test_pynput_installed_with_xlib_backend_without_evdev():
    """CI installs pynput with --no-deps; X11 deps must be present and evdev absent.

    Avoid importing pynput.keyboard here: that opens an X connection and the
    main Linux pytest job has no DISPLAY.
    """
    assert importlib.util.find_spec("Xlib") is not None, "python-xlib should be installed on Linux"
    assert importlib.util.find_spec("six") is not None
    # Intentionally do not require evdev — that is the whole point of --no-deps.
    assert importlib.util.find_spec("evdev") is None

    dist = importlib.metadata.distribution("pynput")
    file_names = {str(f).replace("\\", "/") for f in (dist.files or [])}
    assert any(name.endswith("pynput/keyboard/_xorg.py") for name in file_names), (
        "pynput X11 backend module missing from install"
    )
