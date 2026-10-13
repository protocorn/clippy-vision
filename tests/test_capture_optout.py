"""Tests for OS-level capture opt-out detection."""

from __future__ import annotations

import sys
import time
import unittest
from unittest.mock import patch

from core.capture_optout import (
    PASSWORD_MANAGERS,
    WDA_EXCLUDEFROMCAPTURE,
    WDA_MONITOR,
    WDA_NONE,
    _is_private_process,
    _linux_window_opted_out,
    _macos_window_opted_out,
    _normalize_process_name,
    _windows_window_opted_out,
    forget_optout_cache,
    foreground_should_skip_capture,
    should_skip_capture,
    window_opted_out,
)


class ProcessNameTests(unittest.TestCase):
    def test_normalize_process_name_lowercase(self):
        self.assertEqual(_normalize_process_name("Chrome.exe"), "chrome")
        self.assertEqual(_normalize_process_name("1Password.EXE"), "1password")

    def test_normalize_process_name_removes_extensions(self):
        self.assertEqual(_normalize_process_name("bitwarden.exe"), "bitwarden")
        self.assertEqual(_normalize_process_name("Dashlane.app"), "dashlane")

    def test_normalize_process_name_handles_empty(self):
        self.assertEqual(_normalize_process_name(""), "")
        self.assertEqual(_normalize_process_name(None), "")

    def test_is_private_process_password_managers(self):
        self.assertTrue(_is_private_process("1Password.exe"))
        self.assertTrue(_is_private_process("Bitwarden.exe"))
        self.assertTrue(_is_private_process("KeePassXC"))
        self.assertTrue(_is_private_process("LastPass"))
        self.assertTrue(_is_private_process("Dashlane.app"))
        self.assertTrue(_is_private_process("keeper"))
        self.assertTrue(_is_private_process("enpass"))

    def test_is_private_process_aliases(self):
        self.assertTrue(_is_private_process("1password-browser.exe"))
        self.assertTrue(_is_private_process("keepass2"))
        self.assertTrue(_is_private_process("1passwordnative"))
        self.assertTrue(_is_private_process("Keeper Password Manager"))

    def test_is_private_process_normal_apps(self):
        self.assertFalse(_is_private_process("Chrome.exe"))
        self.assertFalse(_is_private_process("Code.exe"))
        self.assertFalse(_is_private_process("Slack"))
        self.assertFalse(_is_private_process("Terminal"))

    def test_is_private_process_no_false_positives(self):
        self.assertFalse(_is_private_process("timekeeper"))
        self.assertFalse(_is_private_process("shopkeeper.exe"))
        self.assertFalse(_is_private_process("mintupdate"))
        self.assertFalse(_is_private_process("spotify"))
        self.assertFalse(_is_private_process("netflix"))
        self.assertFalse(_is_private_process("quicken"))
        self.assertFalse(_is_private_process("moneydance"))

    def test_list_is_password_managers_only(self):
        self.assertNotIn("spotify", PASSWORD_MANAGERS)
        self.assertNotIn("netflix", PASSWORD_MANAGERS)
        self.assertNotIn("mint", PASSWORD_MANAGERS)
        self.assertNotIn("quicken", PASSWORD_MANAGERS)


class WindowsOptoutTests(unittest.TestCase):
    def setUp(self):
        forget_optout_cache()

    def test_windows_optout_null_hwnd(self):
        self.assertFalse(_windows_window_opted_out(0))
        self.assertFalse(_windows_window_opted_out(None))

    def test_exclude_from_capture_affinity(self):
        with patch("core.capture_optout._read_window_display_affinity", return_value=WDA_EXCLUDEFROMCAPTURE):
            self.assertTrue(_windows_window_opted_out(12345))

    def test_monitor_affinity(self):
        with patch("core.capture_optout._read_window_display_affinity", return_value=WDA_MONITOR):
            self.assertTrue(_windows_window_opted_out(12345))

    def test_none_affinity(self):
        with patch("core.capture_optout._read_window_display_affinity", return_value=WDA_NONE):
            self.assertFalse(_windows_window_opted_out(12345))

    def test_api_failure_is_fail_safe(self):
        with patch("core.capture_optout._read_window_display_affinity", return_value=None):
            self.assertFalse(_windows_window_opted_out(12345))

    def test_changed_affinity_is_picked_up_after_ttl(self):
        with (
            patch("core.capture_optout.IS_WINDOWS", True),
            patch("core.capture_optout.IS_MACOS", False),
            patch("core.capture_optout.IS_LINUX", False),
            patch("core.capture_optout._CACHE_TTL_SECONDS", 0.1),
            patch("core.capture_optout._read_window_display_affinity") as mock_affinity,
        ):
            mock_affinity.return_value = WDA_NONE
            self.assertFalse(window_opted_out(12345, "notepad.exe"))
            self.assertEqual(mock_affinity.call_count, 1)

            self.assertFalse(window_opted_out(12345, "notepad.exe"))
            self.assertEqual(mock_affinity.call_count, 1)

            time.sleep(0.15)
            mock_affinity.return_value = WDA_EXCLUDEFROMCAPTURE
            self.assertTrue(window_opted_out(12345, "notepad.exe"))
            self.assertEqual(mock_affinity.call_count, 2)


class MacOSOptoutTests(unittest.TestCase):
    def test_sharing_none_on_frontmost_window(self):
        windows = [
            {
                "kCGWindowLayer": 0,
                "kCGWindowOwnerPID": 1000,
                "kCGWindowSharingState": 0,
            }
        ]
        with patch("core.capture_optout._macos_copy_window_info", return_value=windows):
            self.assertTrue(_macos_window_opted_out((50, 50, 900, 700)))

    def test_sharing_allowed(self):
        windows = [
            {
                "kCGWindowLayer": 0,
                "kCGWindowOwnerPID": 1000,
                "kCGWindowSharingState": 1,
            }
        ]
        with patch("core.capture_optout._macos_copy_window_info", return_value=windows):
            self.assertFalse(_macos_window_opted_out((50, 50, 900, 700)))

    def test_overlapping_background_window_does_not_trigger(self):
        """A background layer-0 window that overlaps must not trigger opt-out."""
        windows = [
            {
                "kCGWindowLayer": 0,
                "kCGWindowOwnerPID": 1000,
                "kCGWindowBounds": {"X": 100, "Y": 100, "Width": 800, "Height": 600},
                "kCGWindowSharingState": 1,
            },
            {
                "kCGWindowLayer": 0,
                "kCGWindowOwnerPID": 2000,
                "kCGWindowBounds": {"X": 150, "Y": 150, "Width": 700, "Height": 500},
                "kCGWindowSharingState": 0,
            },
        ]
        with patch("core.capture_optout._macos_copy_window_info", return_value=windows):
            self.assertFalse(_macos_window_opted_out((50, 50, 900, 700)))

    def test_frontmost_window_of_foreground_app(self):
        windows = [
            {
                "kCGWindowLayer": 0,
                "kCGWindowOwnerPID": 1000,
                "kCGWindowSharingState": 0,
            },
            {
                "kCGWindowLayer": 0,
                "kCGWindowOwnerPID": 1000,
                "kCGWindowSharingState": 1,
            },
        ]
        with patch("core.capture_optout._macos_copy_window_info", return_value=windows):
            self.assertTrue(_macos_window_opted_out((50, 50, 900, 700)))

    def test_wrong_layer_ignored(self):
        windows = [
            {
                "kCGWindowLayer": 5,
                "kCGWindowOwnerPID": 1000,
                "kCGWindowSharingState": 0,
            }
        ]
        with patch("core.capture_optout._macos_copy_window_info", return_value=windows):
            self.assertFalse(_macos_window_opted_out((50, 50, 900, 700)))

    def test_empty_and_null_window_list(self):
        with patch("core.capture_optout._macos_copy_window_info", return_value=[]):
            self.assertFalse(_macos_window_opted_out((100, 100, 500, 500)))
        with patch("core.capture_optout._macos_copy_window_info", return_value=None):
            self.assertFalse(_macos_window_opted_out((100, 100, 500, 500)))

    def test_exception_is_fail_safe(self):
        with patch("core.capture_optout._macos_copy_window_info", side_effect=RuntimeError("quartz")):
            self.assertFalse(_macos_window_opted_out((100, 100, 500, 500)))

    def test_null_bounds_still_checks_frontmost(self):
        windows = [
            {
                "kCGWindowLayer": 0,
                "kCGWindowOwnerPID": 1000,
                "kCGWindowSharingState": 0,
            }
        ]
        with patch("core.capture_optout._macos_copy_window_info", return_value=windows):
            self.assertTrue(_macos_window_opted_out(None))


class LinuxOptoutTests(unittest.TestCase):
    def test_linux_os_check_is_false(self):
        self.assertFalse(_linux_window_opted_out())


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        forget_optout_cache()

    def test_window_opted_out_private_process(self):
        self.assertTrue(window_opted_out(None, "1Password.exe", None))
        self.assertTrue(window_opted_out(None, "bitwarden", None))

    def test_window_opted_out_normal_process_on_linux(self):
        with (
            patch("core.capture_optout.IS_WINDOWS", False),
            patch("core.capture_optout.IS_MACOS", False),
            patch("core.capture_optout.IS_LINUX", True),
        ):
            self.assertFalse(window_opted_out(None, "chrome.exe", None))

    def test_windows_cache_hit(self):
        with (
            patch("core.capture_optout.IS_WINDOWS", True),
            patch("core.capture_optout.IS_MACOS", False),
            patch("core.capture_optout.IS_LINUX", False),
            patch("core.capture_optout._windows_window_opted_out", return_value=True) as mock_check,
        ):
            self.assertTrue(window_opted_out(12345, "notepad.exe"))
            self.assertTrue(window_opted_out(12345, "notepad.exe"))
            self.assertEqual(mock_check.call_count, 1)

    def test_should_skip_capture_alias(self):
        with patch("core.capture_optout.window_opted_out", return_value=True) as mock_check:
            self.assertTrue(should_skip_capture(12345, "test.exe", None))
            mock_check.assert_called_once_with(12345, "test.exe", None)

    def test_forget_optout_cache(self):
        with (
            patch("core.capture_optout.IS_WINDOWS", True),
            patch("core.capture_optout._windows_window_opted_out", return_value=True),
        ):
            window_opted_out(12345, "app.exe")
            from core.capture_optout import _cache

            self.assertGreater(len(_cache), 0)
            forget_optout_cache()
            self.assertEqual(len(_cache), 0)


class CapturePipelineTests(unittest.TestCase):
    def setUp(self):
        forget_optout_cache()

    def test_screenshot_skipped_when_opted_out(self):
        with (
            patch("core.capture_optout.IS_WINDOWS", True),
            patch("core.capture_optout.IS_MACOS", False),
            patch("core.capture_optout.IS_LINUX", False),
            patch("core.capture_optout._windows_window_opted_out", return_value=True),
        ):
            self.assertTrue(should_skip_capture(12345, "notepad.exe", None))

    def test_screenshot_allowed_when_not_opted_out(self):
        with (
            patch("core.capture_optout.IS_WINDOWS", True),
            patch("core.capture_optout.IS_MACOS", False),
            patch("core.capture_optout.IS_LINUX", False),
            patch("core.capture_optout._windows_window_opted_out", return_value=False),
        ):
            self.assertFalse(should_skip_capture(12345, "notepad.exe", None))

    def test_clipboard_path_on_non_mac(self):
        """The clipboard helper must not import a mac-only bounds API."""
        with (
            patch("core.capture_optout.IS_WINDOWS", False),
            patch("core.capture_optout.IS_MACOS", False),
            patch("core.capture_optout.IS_LINUX", True),
        ):
            self.assertTrue(foreground_should_skip_capture(None, "1password"))
            self.assertFalse(foreground_should_skip_capture(None, "firefox"))

        from core.platform_support import get_foreground_window_bounds

        if sys.platform != "darwin":
            self.assertIsNone(get_foreground_window_bounds())
            self.assertFalse(foreground_should_skip_capture(None, "notepad.exe"))

    def test_clipboard_path_does_not_call_mac_bounds_off_macos(self):
        with (
            patch("core.capture_optout.IS_WINDOWS", True),
            patch("core.capture_optout.IS_MACOS", False),
            patch("core.capture_optout.IS_LINUX", False),
            patch("core.capture_optout._mac_optout_bounds") as mock_bounds,
            patch("core.capture_optout._windows_window_opted_out", return_value=False),
        ):
            self.assertFalse(foreground_should_skip_capture(99, "notepad.exe"))
            mock_bounds.assert_not_called()


if __name__ == "__main__":
    unittest.main()
