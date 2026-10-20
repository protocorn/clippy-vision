"""Linux active-tab URL: AT-SPI address bar, then title + history."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.linux_browser_url import (
    page_title_from_window,
    reset_linux_url_cache,
    resolve_active_url,
    sanitize_browser_url,
)


@pytest.fixture(autouse=True)
def _clear_url_cache():
    reset_linux_url_cache()
    yield
    reset_linux_url_cache()


def _write_chromium_history(path: Path, rows: list[tuple[str, str, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, "
        "last_visit_time INTEGER, hidden INTEGER DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO urls (url, title, last_visit_time, hidden) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _write_firefox_places(path: Path, rows: list[tuple[str, str, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT, "
        "last_visit_date INTEGER, hidden INTEGER DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO moz_places (url, title, last_visit_date, hidden) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


class TestTitleAndSanitize:
    def test_strips_chrome_and_profile_suffix(self):
        assert (
            page_title_from_window("Python 3.13.0 - Google Chrome - Person 1")
            == "Python 3.13.0"
        )

    def test_strips_firefox_em_dash(self):
        assert page_title_from_window("MDN Web Docs — Mozilla Firefox") == "MDN Web Docs"

    def test_accepts_full_url_and_host(self):
        assert sanitize_browser_url("https://nodejs.org/blog") == "https://nodejs.org/blog"
        assert sanitize_browser_url("adafruit.com/product/123") == "https://adafruit.com/product/123"

    def test_rejects_omnibox_search_text(self):
        assert sanitize_browser_url("python 3.13 release notes") is None
        assert sanitize_browser_url("") is None


class TestResolveActiveUrl:
    def test_non_browser_is_none(self, tmp_path: Path):
        assert (
            resolve_active_url("code", "main.py - Cursor", home=tmp_path, atspi_url=lambda: None)
            is None
        )

    def test_title_that_is_already_a_url(self, tmp_path: Path):
        url = resolve_active_url(
            "chrome",
            "https://docs.appimage.org/user-guide/troubleshooting/fuse.html - Google Chrome",
            home=tmp_path,
            atspi_url=lambda: None,
        )
        assert url == "https://docs.appimage.org/user-guide/troubleshooting/fuse.html"

    def test_incognito_title_is_not_recorded(self, tmp_path: Path):
        _write_chromium_history(
            tmp_path / ".config" / "google-chrome" / "Default" / "History",
            [("https://secret.example/page", "Secret", 99, 0)],
        )
        assert (
            resolve_active_url(
                "chrome",
                "Secret - Google Chrome (Incognito)",
                home=tmp_path,
                atspi_url=lambda: "https://secret.example/page",
            )
            is None
        )

    def test_atspi_address_bar_wins(self, tmp_path: Path):
        _write_chromium_history(
            tmp_path / ".config" / "google-chrome" / "Default" / "History",
            [("https://stale.example/", "Raspberry Pi 5", 50, 0)],
        )
        url = resolve_active_url(
            "google-chrome-stable",
            "Raspberry Pi 5 - Google Chrome",
            home=tmp_path,
            atspi_url=lambda: "https://www.adafruit.com/product/pi5",
        )
        assert url == "https://www.adafruit.com/product/pi5"

    def test_chrome_history_matches_window_title(self, tmp_path: Path):
        _write_chromium_history(
            tmp_path / ".config" / "google-chrome" / "Default" / "History",
            [
                ("https://old.example/pi", "Raspberry Pi 5", 10, 0),
                ("https://www.adafruit.com/product/2718", "Raspberry Pi 5", 20, 0),
                ("https://hidden.example/", "Raspberry Pi 5", 99, 1),
            ],
        )
        url = resolve_active_url(
            "chrome",
            "Raspberry Pi 5 - Google Chrome",
            home=tmp_path,
            atspi_url=lambda: None,
        )
        assert url == "https://www.adafruit.com/product/2718"

    def test_chromium_profile_history(self, tmp_path: Path):
        _write_chromium_history(
            tmp_path / ".config" / "chromium" / "Default" / "History",
            [("https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array/flatMap", "Array.prototype.flatMap()", 5, 0)],
        )
        url = resolve_active_url(
            "chromium-browser",
            "Array.prototype.flatMap() - Chromium",
            home=tmp_path,
            atspi_url=lambda: None,
        )
        assert url == (
            "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/"
            "Global_Objects/Array/flatMap"
        )

    def test_firefox_places_matches_window_title(self, tmp_path: Path):
        _write_firefox_places(
            tmp_path / ".mozilla" / "firefox" / "abcd.default-release" / "places.sqlite",
            [("https://nodejs.org/en/blog/release/v22.11.0", "Node.js 22.11.0 (LTS)", 8, 0)],
        )
        url = resolve_active_url(
            "firefox",
            "Node.js 22.11.0 (LTS) — Mozilla Firefox",
            home=tmp_path,
            atspi_url=lambda: None,
        )
        assert url == "https://nodejs.org/en/blog/release/v22.11.0"

    def test_missing_or_corrupt_history_returns_none(self, tmp_path: Path):
        hist = tmp_path / ".config" / "google-chrome" / "Default" / "History"
        hist.parent.mkdir(parents=True)
        hist.write_bytes(b"not a sqlite database")
        assert (
            resolve_active_url(
                "chrome",
                "Python 3.13.0 - Google Chrome",
                home=tmp_path,
                atspi_url=lambda: None,
            )
            is None
        )

    def test_empty_browser_window_is_none(self, tmp_path: Path):
        assert (
            resolve_active_url("chrome", "Google Chrome", home=tmp_path, atspi_url=lambda: None)
            is None
        )

    def test_atspi_exception_falls_through_to_history(self, tmp_path: Path):
        _write_chromium_history(
            tmp_path / ".config" / "google-chrome" / "Default" / "History",
            [("https://github.com/AppImage/AppImageKit/wiki/FUSE", "FUSE", 1, 0)],
        )

        def boom():
            raise RuntimeError("at-spi bus down")

        url = resolve_active_url(
            "chrome",
            "FUSE - Google Chrome",
            home=tmp_path,
            atspi_url=boom,
        )
        assert url == "https://github.com/AppImage/AppImageKit/wiki/FUSE"


class TestAtspiWalk:
    def test_walk_reads_named_address_bar(self):
        from core import linux_browser_url as mod

        text = MagicMock()
        text.characterCount = 26
        text.getText.return_value = "https://example.com/docs"

        bar = MagicMock()
        bar.name = "Address and search bar"
        bar.childCount = 0
        bar.queryText.return_value = text

        root = MagicMock()
        root.name = "Google Chrome"
        root.childCount = 1
        root.getChildAtIndex.return_value = bar

        found: list[str] = []
        mod._walk_address_bar(root, found, depth=0, visited=0, start=mod.time.monotonic())
        assert found == ["https://example.com/docs"]


@pytest.mark.linux
@patch("sys.platform", "linux")
def test_linux_metadata_records_chrome_url():
    """Capture metadata stores the resolved tab URL on Linux Chrome."""
    if "core.platform_support" in sys.modules:
        del sys.modules["core.platform_support"]

    with (
        patch("shutil.which", return_value="/usr/bin/xdotool"),
        patch("core.platform_support._run_command") as mock_run,
        patch("core.linux_browser_url.resolve_active_url", return_value="https://adafruit.com/pi"),
        patch("psutil.Process") as mock_process_cls,
    ):
        def run_cmd(args, **kwargs):
            if "getwindowname" in args:
                return "Raspberry Pi 5 - Google Chrome"
            if "getwindowpid" in args:
                return "4242"
            return ""

        mock_run.side_effect = run_cmd
        mock_process = MagicMock()
        mock_process.name.return_value = "chrome"
        mock_process_cls.return_value = mock_process

        from core import platform_support

        platform_support._linux_cache = None
        platform_support._linux_cache_at = 0.0
        metadata = platform_support.get_window_metadata()

    assert metadata is not None
    assert metadata["process_name"] == "chrome"
    assert metadata["current_window_title"] == "Raspberry Pi 5 - Google Chrome"
    assert metadata["active_url"] == "https://adafruit.com/pi"
