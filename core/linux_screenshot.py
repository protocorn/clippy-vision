"""Linux display session detection and screenshot capture.

X11 sessions use ``mss`` (Xlib). Native Wayland is not supported via the
desktop portal yet; when capture would be blank (typical under XWayland-only
setups), screenshots are skipped with a one-time warning. ``grim`` is used
when available on wlroots compositors.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
from typing import Literal

from PIL import Image

try:
    from core.platform_support import _run_command
except ImportError:
    from platform_support import _run_command

LinuxSessionType = Literal["x11", "wayland", "unknown"]

_wayland_skip_warned = False
_blank_frame_warned = False

_GEOM_SHELL_RE = re.compile(r"^(X|Y|WIDTH|HEIGHT)=(\d+)$", re.MULTILINE)


def linux_session_type() -> LinuxSessionType:
    """Best-effort session kind from environment variables."""
    explicit = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if explicit == "wayland":
        return "wayland"
    if explicit == "x11":
        return "x11"
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def image_is_blank_or_black(img: Image.Image, *, min_non_black_ratio: float = 0.01) -> bool:
    """True when the frame looks empty (all-black / failed grab)."""
    if img.width <= 0 or img.height <= 0:
        return True
    sample = img.convert("L")
    sample.thumbnail((96, 96))
    hist = sample.histogram()
    if not hist:
        return True
    total = sum(hist)
    non_black = sum(hist[9:])
    return non_black < max(1, int(total * min_non_black_ratio))


def _warn_wayland_skip_once() -> None:
    global _wayland_skip_warned
    if _wayland_skip_warned:
        return
    _wayland_skip_warned = True
    print(
        "[capture] Linux Wayland screenshots are not supported yet "
        "(xdg-desktop-portal not implemented); skipping blank captures",
        file=sys.stderr,
    )


def _warn_blank_frame_once() -> None:
    global _blank_frame_warned
    if _blank_frame_warned:
        return
    _blank_frame_warned = True
    print(
        "[capture] screenshot grab returned a blank frame; skipping",
        file=sys.stderr,
    )


def _monitor_dict_for_image(img: Image.Image) -> dict:
    return {"left": 0, "top": 0, "width": img.width, "height": img.height}


def _grab_via_grim() -> tuple[Image.Image, dict] | None:
    grim = shutil.which("grim")
    if not grim:
        return None
    try:
        result = subprocess.run(
            [grim, "-"],
            capture_output=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        img = Image.open(io.BytesIO(result.stdout)).convert("RGB")
    except Exception:
        return None
    return img, _monitor_dict_for_image(img)


def _grab_via_mss(capture_all_monitors: bool) -> tuple[Image.Image, dict] | None:
    if not os.environ.get("DISPLAY"):
        return None
    try:
        import mss
    except ImportError:
        return None
    try:
        with mss.MSS() as sct:
            if capture_all_monitors:
                monitor = dict(sct.monitors[0])
            elif len(sct.monitors) > 1:
                monitor = dict(sct.monitors[1])
            else:
                monitor = dict(sct.monitors[0])
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
    except Exception:
        return None
    return img, monitor


def _active_window_rect() -> tuple[int, int, int, int] | None:
    if not shutil.which("xdotool"):
        return None
    raw = _run_command(["xdotool", "getactivewindow", "getwindowgeometry", "--shell"], timeout=2.0)
    if not raw:
        return None
    values: dict[str, int] = {}
    for match in _GEOM_SHELL_RE.finditer(raw):
        values[match.group(1)] = int(match.group(2))
    if not all(k in values for k in ("X", "Y", "WIDTH", "HEIGHT")):
        return None
    left = values["X"]
    top = values["Y"]
    right = left + values["WIDTH"]
    bottom = top + values["HEIGHT"]
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _crop_to_rect(
    img: Image.Image, monitor: dict, rect: tuple[int, int, int, int]
) -> tuple[Image.Image, dict]:
    left, top, right, bottom = rect
    mon_left = int(monitor.get("left", 0))
    mon_top = int(monitor.get("top", 0))
    mon_right = mon_left + int(monitor.get("width", img.width))
    mon_bottom = mon_top + int(monitor.get("height", img.height))
    crop_left = max(left, mon_left)
    crop_top = max(top, mon_top)
    crop_right = min(right, mon_right)
    crop_bottom = min(bottom, mon_bottom)
    if crop_right <= crop_left or crop_bottom <= crop_top:
        return img, monitor
    rel = (
        crop_left - mon_left,
        crop_top - mon_top,
        crop_right - mon_left,
        crop_bottom - mon_top,
    )
    cropped = img.crop(rel)
    new_monitor = {
        "left": crop_left,
        "top": crop_top,
        "width": crop_right - crop_left,
        "height": crop_bottom - crop_top,
    }
    return cropped, new_monitor


def grab_linux_screen(capture_all_monitors: bool) -> tuple[Image.Image, dict] | None:
    """Capture the screen on Linux, or None when capture should be skipped."""
    session = linux_session_type()
    candidates: list[tuple[Image.Image, dict]] = []

    if session == "wayland" and shutil.which("grim"):
        grim_result = _grab_via_grim()
        if grim_result is not None:
            candidates.append(grim_result)

    if os.environ.get("DISPLAY"):
        mss_result = _grab_via_mss(capture_all_monitors)
        if mss_result is not None:
            candidates.append(mss_result)

    for img, monitor in candidates:
        if image_is_blank_or_black(img):
            continue
        rect = _active_window_rect()
        if rect is not None:
            img, monitor = _crop_to_rect(img, monitor, rect)
            if image_is_blank_or_black(img):
                continue
        return img, monitor

    if candidates:
        _warn_blank_frame_once()
    elif session == "wayland":
        _warn_wayland_skip_once()
    elif not os.environ.get("DISPLAY"):
        _warn_wayland_skip_once()
    return None


def reset_linux_screenshot_warnings() -> None:
    """Test helper: clear one-shot warning flags."""
    global _wayland_skip_warned, _blank_frame_warned
    _wayland_skip_warned = False
    _blank_frame_warned = False
