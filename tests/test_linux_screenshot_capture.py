"""Linux screenshot session detection, blank-frame guard, and Xvfb capture."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import pytest
from PIL import Image

from core.linux_screenshot import (
    grab_linux_screen,
    image_is_blank_or_black,
    linux_session_type,
    reset_linux_screenshot_warnings,
)


class TestLinuxSessionDetection(unittest.TestCase):
    @pytest.mark.linux
    def test_session_x11_from_env(self):
        with patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}, clear=False):
            self.assertEqual(linux_session_type(), "x11")

    @pytest.mark.linux
    def test_session_wayland_from_xdg(self):
        with patch.dict(
            os.environ,
            {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-1"},
            clear=False,
        ):
            self.assertEqual(linux_session_type(), "wayland")

    @pytest.mark.linux
    def test_session_wayland_from_wayland_display(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_SESSION_TYPE"}
        env["WAYLAND_DISPLAY"] = "wayland-0"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(linux_session_type(), "wayland")


class TestBlankFrameGuard(unittest.TestCase):
    @pytest.mark.linux
    def test_detects_all_black(self):
        img = Image.new("RGB", (100, 80), (0, 0, 0))
        self.assertTrue(image_is_blank_or_black(img))

    @pytest.mark.linux
    def test_accepts_colored_frame(self):
        img = Image.new("RGB", (100, 80), (40, 120, 200))
        self.assertFalse(image_is_blank_or_black(img))


class TestGrabLinuxScreen(unittest.TestCase):
    @pytest.mark.linux
    @patch("core.linux_screenshot._grab_via_mss")
    @patch("core.linux_screenshot.linux_session_type", return_value="wayland")
    def test_wayland_blank_mss_skips(self, _session, mock_mss):
        reset_linux_screenshot_warnings()
        mock_mss.return_value = (Image.new("RGB", (10, 10), (0, 0, 0)), {"left": 0, "top": 0, "width": 10, "height": 10})
        with patch.dict(os.environ, {"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland"}, clear=False):
            with patch("builtins.print") as mock_print:
                self.assertIsNone(grab_linux_screen(False))
                stderr_calls = [c for c in mock_print.call_args_list if c.kwargs.get("file") is sys.stderr]
                self.assertTrue(stderr_calls)

    @pytest.mark.linux
    @patch("core.linux_screenshot._grab_via_mss")
    @patch("core.linux_screenshot.linux_session_type", return_value="x11")
    def test_x11_non_blank_mss_returns(self, _session, mock_mss):
        img = Image.new("RGB", (64, 48), (10, 20, 30))
        monitor = {"left": 0, "top": 0, "width": 64, "height": 48}
        mock_mss.return_value = (img, monitor)
        with patch.dict(os.environ, {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}, clear=False):
            with patch("core.linux_screenshot._active_window_rect", return_value=None):
                result = grab_linux_screen(False)
        self.assertIsNotNone(result)
        self.assertEqual(result[0].size, (64, 48))

    @pytest.mark.linux
    @patch("core.linux_screenshot.linux_session_type", return_value="wayland")
    def test_wayland_without_display_warns(self, _session):
        reset_linux_screenshot_warnings()
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        env["XDG_SESSION_TYPE"] = "wayland"
        env["WAYLAND_DISPLAY"] = "wayland-1"
        with patch.dict(os.environ, env, clear=True):
            with patch("shutil.which", return_value=None):
                with patch("builtins.print") as mock_print:
                    self.assertIsNone(grab_linux_screen(False))
                    stderr_calls = [c for c in mock_print.call_args_list if c.kwargs.get("file") is sys.stderr]
                    self.assertTrue(stderr_calls)


def _spawn_xvfb_fixture_window() -> subprocess.Popen | None:
    """Draw a visible window so mss has non-black pixels under Xvfb.
    
    Waits for the window to be mapped using xdotool to avoid race conditions.
    
    Returns:
        The xterm process, or None if xterm is not installed/available.
        
    Raises:
        RuntimeError: If xterm spawns but the window never maps (test failure).
    """
    try:
        proc = subprocess.Popen(
            [
                "xterm",
                "-bg",
                "#336699",
                "-fg",
                "white",
                "-geometry",
                "240x160+24+24",
                "-e",
                "sleep",
                "90",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    
    # Wait for the window to be mapped using xdotool
    # If this times out, it's a test failure, not a skip
    try:
        _wait_for_xterm_window(proc, timeout=5.0)
    except RuntimeError:
        proc.terminate()
        raise
    
    return proc


def _wait_for_xterm_window(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """Wait for an xterm window to appear and be mapped.
    
    Uses xdotool search --sync to wait for the window.
    Falls back to polling xwininfo if xdotool is not available.
    
    Args:
        proc: The xterm process to wait for.
        timeout: Maximum time to wait in seconds.
        
    Raises:
        RuntimeError: If the window doesn't map within the timeout.
    """
    import shutil
    
    # First try xdotool search --sync (most reliable)
    if shutil.which("xdotool"):
        try:
            result = subprocess.run(
                ["xdotool", "search", "--sync", "--class", "XTerm"],
                timeout=timeout,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                # Give the window a moment to finish initial rendering
                time.sleep(0.2)
                return
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"xterm window (PID {proc.pid}) did not map within {timeout}s timeout"
            )
        except (OSError, subprocess.SubprocessError):
            pass
    
    # Fallback: poll for window list
    if shutil.which("xwininfo"):
        start = time.time()
        while time.time() - start < timeout:
            try:
                result = subprocess.run(
                    ["xwininfo", "-root", "-tree"],
                    timeout=2.0,
                    capture_output=True,
                    check=False,
                )
                if result.returncode == 0 and b"xterm" in result.stdout.lower():
                    time.sleep(0.2)
                    return
            except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
                pass
            time.sleep(0.1)
        
        raise RuntimeError(
            f"xterm window (PID {proc.pid}) did not appear in window list within {timeout}s timeout"
        )
    
    # Last resort: fixed sleep (no verification possible)
    time.sleep(1.5)


@pytest.mark.linux
@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only Xvfb capture test")
def test_x11_screenshot_non_blank_under_xvfb():
    """Real grab under Xvfb; CI runs this step with xvfb-run.
    
    Polls for a non-blank capture to handle any remaining rendering delays.
    """
    if not os.environ.get("DISPLAY"):
        pytest.skip("DISPLAY not set (run under xvfb-run)")

    fixture = _spawn_xvfb_fixture_window()
    if fixture is None:
        pytest.skip("xterm not available for Xvfb fixture")

    try:
        reset_linux_screenshot_warnings()
        
        # Poll for a non-blank capture within timeout
        # This handles any remaining race conditions with window rendering
        result = None
        timeout = 10.0
        start = time.time()
        attempt = 0
        
        with patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11"}, clear=False):
            while time.time() - start < timeout:
                attempt += 1
                reset_linux_screenshot_warnings()  # Suppress spam during polling
                result = grab_linux_screen(capture_all_monitors=True)
                if result is not None:
                    img, monitor = result
                    if not image_is_blank_or_black(img):
                        break
                result = None
                time.sleep(0.3)
        
        if result is None:
            pytest.fail(
                f"Failed to capture non-blank frame after {attempt} attempts "
                f"over {time.time() - start:.1f}s. The xterm fixture window may not "
                "be rendering properly under Xvfb."
            )
        
        img, monitor = result
        assert img.width >= 1 and img.height >= 1
        assert monitor["width"] == img.width
        assert monitor["height"] == img.height
        assert not image_is_blank_or_black(img)
    finally:
        fixture.terminate()


@pytest.mark.linux
@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only scheduler integration")
def test_capture_screenshot_integration_under_xvfb():
    if not os.environ.get("DISPLAY"):
        pytest.skip("DISPLAY not set (run under xvfb-run)")

    from pathlib import Path
    from unittest.mock import patch

    from core import screenshot_files
    from core import screenshot_scheduler as scheduler

    reset_linux_screenshot_warnings()
    fixture = _spawn_xvfb_fixture_window()
    if fixture is None:
        pytest.skip("xterm not available for Xvfb fixture")
    
    try:
        settings = {
            "capture_screenshots": True,
            "capture_all_monitors": True,
            "min_gap_seconds": 0,
            "capture_clipboard": False,
        }
        metadata = {
            "timestamp": 0.0,
            "current_window_title": "test",
            "active_url": None,
            "process_name": "xvfb",
        }
        with patch.object(scheduler, "get_capture_settings", return_value=settings), patch.object(
            scheduler, "get_window_metadata", return_value=metadata
        ), patch.object(scheduler, "should_watch_process", return_value=True), patch.object(
            scheduler, "_redact_clippy_windows"
        ), patch.object(scheduler, "save_crop_metadata"), patch(
            "core.editor_accessibility.ensure_screen_reader_support"
        ), patch.object(screenshot_files, "write_frame_window"):
            path = scheduler.capture_screenshot(999_001)
        
        assert path is not None
        assert Path(path).is_file()
        img = Image.open(path)
        assert not image_is_blank_or_black(img.convert("RGB"))
        path.unlink(missing_ok=True)
    finally:
        fixture.terminate()


if __name__ == "__main__":
    unittest.main()
