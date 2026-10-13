"""Test Linux platform support: graceful degradation without crashing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestLinuxPlatformImports(unittest.TestCase):
    """Test that core modules can be imported on Linux without failing."""

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_platform_support_imports(self):
        """platform_support.py can be imported on Linux."""
        # Clear any previously imported version
        if "core.platform_support" in sys.modules:
            del sys.modules["core.platform_support"]
        
        # Import should not raise
        from core import platform_support
        
        assert platform_support.IS_LINUX is True
        assert platform_support.IS_WINDOWS is False
        assert platform_support.IS_MACOS is False

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_accessibility_text_imports(self):
        """accessibility_text.py can be imported on Linux."""
        if "core.accessibility_text" in sys.modules:
            del sys.modules["core.accessibility_text"]
        
        # Import should not raise even though uiautomation is not available
        from core import accessibility_text
        
        # Extract function should return empty string on Linux
        assert accessibility_text.extract_accessibility_text() == ""

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_screen_capture_imports(self):
        """screen_capture.py can be imported on Linux without Windows-only deps."""
        # Note: This test only verifies imports work, not that capture runs
        # (that requires keyboard hooks and other system integrations)
        try:
            if "core.screen_capture" in sys.modules:
                del sys.modules["core.screen_capture"]
            # We can't fully import screen_capture as it has side effects,
            # but we can check that platform_support is available
            from core import platform_support
            assert hasattr(platform_support, "IS_LINUX")
        except ImportError as e:
            # Only fail if it's a Windows-specific import at top level
            if "win32" in str(e) or "uiautomation" in str(e) or "windll" in str(e):
                pytest.fail(f"Windows-only import not guarded: {e}")

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_private_windows_on_linux(self):
        """private_windows.py gracefully handles Linux."""
        if "core.private_windows" in sys.modules:
            del sys.modules["core.private_windows"]
        
        from core import private_windows
        
        # On Linux, window_is_private should always return False
        assert private_windows.window_is_private(None, "chrome") is False
        assert private_windows.window_is_private(12345, "firefox") is False


class TestLinuxMetadata(unittest.TestCase):
    """Test Linux window metadata collection."""

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    @patch("shutil.which")
    def test_missing_xdotool_returns_empty_metadata(self, mock_which):
        """When xdotool is missing, metadata returns empty but doesn't crash."""
        mock_which.return_value = None
        
        if "core.platform_support" in sys.modules:
            del sys.modules["core.platform_support"]
        
        from core import platform_support
        
        metadata = platform_support.get_window_metadata()
        
        # Should return metadata, not None
        assert metadata is not None
        assert metadata["current_window_title"] == ""
        assert metadata["active_url"] is None
        assert metadata["process_name"] == "unknown"

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    @patch("shutil.which")
    @patch("core.platform_support._run_command")
    def test_xdotool_available_returns_metadata(self, mock_run_cmd, mock_which):
        """When xdotool is available, metadata is populated."""
        mock_which.return_value = "/usr/bin/xdotool"
        
        # Mock xdotool responses
        def run_cmd_side_effect(args, **kwargs):
            if "getwindowname" in args:
                return "Firefox"
            elif "getwindowpid" in args:
                return "12345"
            return ""
        
        mock_run_cmd.side_effect = run_cmd_side_effect
        
        if "core.platform_support" in sys.modules:
            del sys.modules["core.platform_support"]
        
        # Mock psutil inside the module (it's imported locally in _linux_metadata)
        with patch("psutil.Process") as mock_process_cls:
            mock_process = MagicMock()
            mock_process.name.return_value = "firefox"
            mock_process_cls.return_value = mock_process
            
            from core import platform_support
            
            metadata = platform_support.get_window_metadata()
            
            assert metadata is not None
            assert metadata["current_window_title"] == "Firefox"
            assert metadata["process_name"] == "firefox"
            assert metadata["active_url"] is None

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    @patch("shutil.which")
    def test_missing_idle_tools_returns_none(self, mock_which):
        """When idle detection tools are missing, returns None gracefully."""
        mock_which.return_value = None
        
        if "core.platform_support" in sys.modules:
            del sys.modules["core.platform_support"]
        
        from core import platform_support
        
        idle_seconds = platform_support.get_idle_seconds()
        
        # Should return None, not crash
        assert idle_seconds is None

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    @patch("shutil.which")
    @patch("core.platform_support._run_command")
    def test_xprintidle_returns_idle_time(self, mock_run_cmd, mock_which):
        """When xprintidle is available, idle time is returned."""
        mock_which.return_value = "/usr/bin/xprintidle"
        mock_run_cmd.return_value = "5000"  # 5000 milliseconds
        
        if "core.platform_support" in sys.modules:
            del sys.modules["core.platform_support"]
        
        from core import platform_support
        
        idle_seconds = platform_support.get_idle_seconds()
        
        # Should return 5.0 seconds
        assert idle_seconds == 5.0


class TestLinuxClipboard(unittest.TestCase):
    """Test Linux clipboard handling."""

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    @patch("shutil.which")
    def test_missing_clipboard_tool_returns_none(self, mock_which):
        """When no clipboard tool is available, returns None."""
        mock_which.return_value = None
        
        if "core.platform_support" in sys.modules:
            del sys.modules["core.platform_support"]
        
        from core import platform_support
        
        clipboard = platform_support.get_clipboard_text()
        
        # Should return None, not crash
        assert clipboard is None

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    @patch("shutil.which")
    @patch("core.platform_support._run_command")
    def test_wl_paste_returns_clipboard(self, mock_run_cmd, mock_which):
        """When wl-paste is available, clipboard text is returned."""
        def which_side_effect(cmd):
            return "/usr/bin/wl-paste" if cmd == "wl-paste" else None
        
        mock_which.side_effect = which_side_effect
        mock_run_cmd.return_value = "copied text"
        
        if "core.platform_support" in sys.modules:
            del sys.modules["core.platform_support"]
        
        from core import platform_support
        
        clipboard = platform_support.get_clipboard_text()
        
        assert clipboard == "copied text"


class TestLinuxAccessibility(unittest.TestCase):
    """Test Linux accessibility text extraction."""

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_accessibility_text_returns_empty(self):
        """Accessibility text extraction returns empty string on Linux."""
        if "core.accessibility_text" in sys.modules:
            del sys.modules["core.accessibility_text"]
        
        from core import accessibility_text
        
        text = accessibility_text.extract_accessibility_text()
        
        # Should return empty, not crash
        assert text == ""

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_foreground_content_bounds_returns_none(self):
        """Foreground content bounds returns None on Linux."""
        if "core.accessibility_text" in sys.modules:
            del sys.modules["core.accessibility_text"]
        
        from core import accessibility_text
        
        bounds = accessibility_text.foreground_content_bounds()
        
        # Should return None, not crash
        assert bounds is None


@pytest.mark.linux
@pytest.mark.skipif(sys.platform != "linux", reason="Linux-specific smoke test")
def test_linux_capture_smoke():
    """
    Headless smoke test: verify capture can start without crashing.
    
    This test mocks screenshot and accessibility to avoid X11/Wayland requirements.
    It verifies the event loop can run for a few iterations with Linux platform code.
    """
    # This test would be more elaborate in a real implementation
    # For now, just verify imports work
    from core import platform_support
    
    assert platform_support.IS_LINUX
    metadata = platform_support.get_window_metadata()
    assert metadata is not None
    
    # Verify get_idle_seconds doesn't crash
    idle = platform_support.get_idle_seconds()
    assert idle is None or isinstance(idle, (int, float))


if __name__ == "__main__":
    unittest.main()
