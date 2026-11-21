"""Best-effort active-tab URL for Linux browsers.

Windows reads the address bar through UI Automation. Linux has no equivalent
that Chrome exposes without an assistive-tech session, so capture tries, in
order:

1. The window title, when it already is a URL
2. The AT-SPI address bar (same control names Windows uses), if present
3. The browser's on-disk session/history, matched to the page title

None of those require launching the browser with extra flags. A miss returns
None and the rest of capture continues.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from core.process_names import process_key
from core.private_windows import root_name_is_private

_BROWSER_KEYS = frozenset(
    {"chrome", "chromium", "firefox", "brave", "edge", "opera", "vivaldi"}
)
_CHROMIUM_KEYS = frozenset({"chrome", "chromium", "brave", "edge", "opera", "vivaldi"})

# Same captions Windows looks up on the address bar. "find" is not an address.
_ADDRESS_BAR_NAMES = frozenset(
    {
        "address and search bar",
        "address bar",
        "search or enter address",
        "search with google or enter address",
    }
)

_EMPTY_TITLES = frozenset(
    {
        "",
        "google chrome",
        "chromium",
        "mozilla firefox",
        "firefox",
        "brave",
        "microsoft edge",
        "opera",
        "vivaldi",
        "new tab",
    }
)

_TITLE_SUFFIXES = (
    re.compile(r"\s+-\s+Google Chrome(?:\s+-.*)?$", re.IGNORECASE),
    re.compile(r"\s+-\s+Chromium(?:\s+-.*)?$", re.IGNORECASE),
    re.compile(r"\s+-\s+Microsoft Edge(?:\s+-.*)?$", re.IGNORECASE),
    re.compile(r"\s+-\s+Brave(?:\s+-.*)?$", re.IGNORECASE),
    re.compile(r"\s+-\s+Opera(?:\s+-.*)?$", re.IGNORECASE),
    re.compile(r"\s+-\s+Vivaldi(?:\s+-.*)?$", re.IGNORECASE),
    re.compile(r"\s+[—–-]\s+Mozilla Firefox(?:\s+-.*)?$", re.IGNORECASE),
    re.compile(r"\s+[—–-]\s+Firefox(?:\s+-.*)?$", re.IGNORECASE),
)

_SCHEME_RE = re.compile(
    r"^(?:https?|ftp|file|chrome|edge|brave|about|chrome-extension):",
    re.IGNORECASE,
)
_HOST_RE = re.compile(
    r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#].*)?$",
    re.IGNORECASE,
)

_LOOKUP_TTL_SECONDS = 2.0
_ATSPI_TIMEOUT_SECONDS = 0.5
_ATSPI_MAX_NODES = 80
_ATSPI_MAX_DEPTH = 12

_cache_lock = threading.Lock()
_lookup_cache: dict[tuple[str, str], tuple[float, str | None]] = {}


def reset_linux_url_cache() -> None:
    with _cache_lock:
        _lookup_cache.clear()


def page_title_from_window(window_title: str) -> str:
    title = (window_title or "").strip()
    for pattern in _TITLE_SUFFIXES:
        title = pattern.sub("", title).strip()
    return title


def sanitize_browser_url(value: str | None) -> str | None:
    """Keep real page URLs; drop omnibox search text."""
    raw = (value or "").strip()
    if not raw or any(ch.isspace() for ch in raw):
        return None
    if _SCHEME_RE.match(raw):
        return raw[:2048]
    if _HOST_RE.match(raw):
        return ("https://" + raw)[:2048]
    return None


def resolve_active_url(
    process_name: str,
    window_title: str,
    *,
    home: Path | None = None,
    atspi_url: Callable[[], str | None] | None = None,
) -> str | None:
    """Return the frontmost tab URL, or None when it cannot be found."""
    key = process_key(process_name)
    if key not in _BROWSER_KEYS:
        return None
    title = (window_title or "").strip()
    if not title:
        return None
    if root_name_is_private(title, process_name):
        return None

    page_title = page_title_from_window(title)
    cache_key = (key, page_title.casefold())
    now = time.monotonic()
    with _cache_lock:
        cached = _lookup_cache.get(cache_key)
        if cached is not None and now - cached[0] < _LOOKUP_TTL_SECONDS:
            return cached[1]

    url = _resolve_uncached(
        key,
        page_title,
        home=home or Path.home(),
        atspi_url=atspi_url if atspi_url is not None else _atspi_address_bar_url,
    )
    with _cache_lock:
        _lookup_cache[cache_key] = (time.monotonic(), url)
        while len(_lookup_cache) > 64:
            _lookup_cache.pop(next(iter(_lookup_cache)))
    return url


def _resolve_uncached(
    key: str,
    page_title: str,
    *,
    home: Path,
    atspi_url: Callable[[], str | None],
) -> str | None:
    from_title = sanitize_browser_url(page_title)
    if from_title:
        return from_title
    if page_title.casefold() in _EMPTY_TITLES:
        return None

    try:
        found = sanitize_browser_url(atspi_url())
        if found:
            return found
    except Exception:
        pass

    return _url_from_history(key, page_title, home)


def _url_from_history(key: str, page_title: str, home: Path) -> str | None:
    best_url: str | None = None
    best_visit = -1
    for path, kind in _history_dbs(key, home):
        url, visit = _lookup_title_in_db(path, kind, page_title)
        if url and visit >= best_visit:
            best_url = url
            best_visit = visit
    return best_url


def _history_dbs(key: str, home: Path) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    if key in _CHROMIUM_KEYS:
        for root in _chromium_roots(home, key):
            if not root.is_dir():
                continue
            for hist in sorted(root.glob("*/History")):
                profile = hist.parent.name.casefold()
                if profile in {"system profile", "guest profile"}:
                    continue
                if hist.is_file():
                    found.append((hist, "chromium"))
    if key == "firefox":
        for root in _firefox_roots(home):
            if not root.is_dir():
                continue
            for places in sorted(root.glob("*/places.sqlite")):
                if places.is_file():
                    found.append((places, "firefox"))
    return found


def _chromium_roots(home: Path, key: str) -> list[Path]:
    chrome = [
        home / ".config" / "google-chrome",
        home / ".config" / "google-chrome-stable",
        home / ".var" / "app" / "com.google.Chrome" / "config" / "google-chrome",
    ]
    chromium = [
        home / ".config" / "chromium",
        home / ".config" / "chromium-browser",
        home / ".var" / "app" / "org.chromium.Chromium" / "config" / "chromium",
        home / "snap" / "chromium" / "common" / "chromium",
    ]
    extra = {
        "brave": [
            home / ".config" / "BraveSoftware" / "Brave-Browser",
            home / ".var" / "app" / "com.brave.Browser" / "config" / "BraveSoftware" / "Brave-Browser",
        ],
        "edge": [
            home / ".config" / "microsoft-edge",
            home / ".config" / "microsoft-edge-stable",
        ],
        "opera": [home / ".config" / "opera"],
        "vivaldi": [home / ".config" / "vivaldi"],
    }
    if key in {"chrome", "chromium"}:
        return chrome + chromium
    return extra.get(key, [])


def _firefox_roots(home: Path) -> list[Path]:
    return [
        home / ".mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
        home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
    ]


def _lookup_title_in_db(path: Path, kind: str, page_title: str) -> tuple[str | None, int]:
    if kind == "firefox":
        queries = (
            (
                "SELECT url, COALESCE(last_visit_date, 0) FROM moz_places "
                "WHERE title = ? AND COALESCE(hidden, 0) = 0 "
                "ORDER BY COALESCE(last_visit_date, 0) DESC LIMIT 3",
                (page_title,),
            ),
            (
                "SELECT url, COALESCE(last_visit_date, 0) FROM moz_places "
                "WHERE title = ? COLLATE NOCASE AND COALESCE(hidden, 0) = 0 "
                "ORDER BY COALESCE(last_visit_date, 0) DESC LIMIT 3",
                (page_title,),
            ),
        )
    else:
        queries = (
            (
                "SELECT url, last_visit_time FROM urls "
                "WHERE title = ? AND COALESCE(hidden, 0) = 0 "
                "ORDER BY last_visit_time DESC LIMIT 3",
                (page_title,),
            ),
            (
                "SELECT url, last_visit_time FROM urls "
                "WHERE title = ? COLLATE NOCASE AND COALESCE(hidden, 0) = 0 "
                "ORDER BY last_visit_time DESC LIMIT 3",
                (page_title,),
            ),
        )
        if len(page_title) >= 12:
            queries = queries + (
                (
                    "SELECT url, last_visit_time FROM urls "
                    "WHERE title LIKE ? AND COALESCE(hidden, 0) = 0 "
                    "ORDER BY last_visit_time DESC LIMIT 3",
                    (page_title + "%",),
                ),
            )

    rows: list[tuple[object, object]] = []
    for sql, params in queries:
        rows = _query_sqlite(path, sql, params)
        if rows:
            break
    best_url = None
    best_visit = -1
    for url, visit in rows:
        cleaned = sanitize_browser_url(str(url) if url is not None else None)
        try:
            visit_n = int(visit or 0)
        except (TypeError, ValueError):
            visit_n = 0
        if cleaned and visit_n >= best_visit:
            best_url = cleaned
            best_visit = visit_n
    return best_url, best_visit


def _query_sqlite(path: Path, sql: str, params: tuple) -> list[tuple[object, object]]:
    try:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.2)
        try:
            return list(conn.execute(sql, params))
        finally:
            conn.close()
    except sqlite3.Error:
        return _query_sqlite_copied(path, sql, params)
    except OSError:
        return []


def _query_sqlite_copied(path: Path, sql: str, params: tuple) -> list[tuple[object, object]]:
    tmpdir = tempfile.mkdtemp(prefix="clippy-hist-")
    try:
        dest = Path(tmpdir) / "db"
        shutil.copy2(path, dest)
        for suffix in ("-wal", "-shm"):
            extra = Path(str(path) + suffix)
            if extra.is_file():
                try:
                    shutil.copy2(extra, Path(str(dest) + suffix))
                except OSError:
                    pass
        conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True, timeout=0.2)
        try:
            return list(conn.execute(sql, params))
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _atspi_address_bar_url() -> str | None:
    try:
        import pyatspi
    except ImportError:
        return None
    try:
        try:
            if hasattr(pyatspi, "set_timeout"):
                pyatspi.set_timeout(300, 600)
        except Exception:
            pass
        desktop = pyatspi.Registry.getDesktop(0)
        if desktop is None:
            return None
        active_app = None
        for app in desktop:
            if app is None:
                continue
            try:
                if pyatspi.STATE_ACTIVE in app.getState():
                    active_app = app
                    break
            except Exception:
                continue
        if active_app is None:
            return None
        found: list[str] = []
        _walk_address_bar(active_app, found, depth=0, visited=0, start=time.monotonic())
        return found[0] if found else None
    except Exception:
        return None


def _walk_address_bar(accessible, found: list[str], depth: int, visited: int, start: float) -> int:
    if found or accessible is None:
        return visited
    if visited >= _ATSPI_MAX_NODES or depth >= _ATSPI_MAX_DEPTH:
        return visited
    if time.monotonic() - start > _ATSPI_TIMEOUT_SECONDS:
        return visited
    visited += 1
    try:
        name = str(getattr(accessible, "name", "") or "").strip().casefold()
        if name in _ADDRESS_BAR_NAMES:
            value = _accessible_value(accessible)
            cleaned = sanitize_browser_url(value)
            if cleaned:
                found.append(cleaned)
                return visited
    except Exception:
        pass
    try:
        child_count = accessible.childCount
        for i in range(min(int(child_count or 0), 40)):
            try:
                child = accessible.getChildAtIndex(i)
            except Exception:
                continue
            visited = _walk_address_bar(child, found, depth + 1, visited, start)
            if found or visited >= _ATSPI_MAX_NODES:
                break
    except Exception:
        pass
    return visited


def _accessible_value(accessible) -> str:
    try:
        text_iface = accessible.queryText()
        if text_iface and text_iface.characterCount:
            text = text_iface.getText(0, min(text_iface.characterCount, 2048))
            if text and text.strip():
                return text.strip()
    except Exception:
        pass
    try:
        value_iface = accessible.queryValue()
        if value_iface is not None and value_iface.currentValue is not None:
            return str(value_iface.currentValue).strip()
    except Exception:
        pass
    return ""
