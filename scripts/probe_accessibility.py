"""
Probe foreground accessibility text the same way capture does.

Every 5 seconds prints:
  - active window
  - raw extract_accessibility_text() output
  - production-gated text (_foreground_accessibility_text)
  - whether that gated text would be stored with a screenshot
  - whether it counts as "useful" (skips OCR in enrichment)

Usage (PowerShell):
  cd c:\\Users\\proto\\Clippy_Vision
  $env:PYTHONPATH = (Get-Location).Path
  python .\\scripts\\probe_accessibility.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.accessibility_text import (
    MIN_USEFUL_CHARS,
    UI_CHROME_LINES,
    extract_accessibility_text,
    is_useful_accessibility_text,
    _best_region_text,
    _collect_content_walk,
    _find_best_document,
    _text_from_control,
)
from core.platform_support import get_window_metadata
from core.privacy_settings import is_clippy_window, should_redact_window
from core.screenshot_enrichment import choose_screen_text
from core.vision import _foreground_accessibility_text

POLL_SECS = 5


def _preview(text: str, limit: int = 1200) -> str:
    cleaned = text.replace("\r\n", "\n").strip()
    if not cleaned:
        return "(empty)"
    if len(cleaned) > limit:
        return cleaned[:limit] + f"\n... [{len(cleaned) - limit} more chars]"
    return cleaned


def _store_decision(production_text: str, process_name: str, title: str) -> tuple[bool, str]:
    """Mirror what capture actually persists for accessibility text."""
    if is_clippy_window(process_name, title):
        return False, "NO — Clippy's own window is redacted"
    if should_redact_window(process_name, title):
        return False, "NO — privacy-redacted window"
    if not production_text.strip():
        return False, "NO — production gate returned empty (focus changed, redact, chrome-only, or no UI text)"
    return True, "YES — would be cached with the screenshot and merged into vision_ocr_text"


def _strategy_hint() -> str:
    """Best-effort note about which extractor path would win for the FG window."""
    try:
        import uiautomation as auto
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return "none"
        root = auto.ControlFromHandle(hwnd)
        focused = auto.GetFocusedControl()
        if focused is not None and getattr(focused, "ControlTypeName", "") in {"EditControl", "DocumentControl"}:
            text = _text_from_control(focused)
            if is_useful_accessibility_text(text):
                return f"focused {focused.ControlTypeName}"
        document = _find_best_document(root)
        if document is not None:
            region = _best_region_text(document)
            if region.strip():
                return "document region (max-text)"
        window_region = _best_region_text(root)
        if window_region.strip():
            return "window region (max-text)"
        if _collect_content_walk(root).strip():
            return "content walk"
        return "empty/fallback"
    except Exception as exc:
        return f"error: {exc}"


def main() -> None:
    print("Accessibility probe — Ctrl+C to stop")
    print(f"Polling every {POLL_SECS}s using region max-text core/accessibility_text.py")
    print(f"Useful threshold: >= {MIN_USEFUL_CHARS} alnum chars and >= 4 words (nav-soup demoted)")
    print(f"Chrome string filters: {len(UI_CHROME_LINES)} lines\n")

    while True:
        stamp = time.strftime("%H:%M:%S")
        meta = get_window_metadata() or {}
        process_name = str(meta.get("process_name") or "")
        title = str(meta.get("current_window_title") or "")
        url = meta.get("active_url")

        strategy = _strategy_hint()
        raw = extract_accessibility_text()
        production = _foreground_accessibility_text()
        store, store_reason = _store_decision(production, process_name, title)
        useful = is_useful_accessibility_text(production) if production.strip() else False

        print("=" * 72)
        print(f"[{stamp}] window: {process_name or '(unknown)'} — {title or '(no title)'}")
        if url:
            print(f"         url: {url}")
        print(f"         strategy: {strategy}")
        print("-" * 72)
        print("EXTRACT (structure-first):")
        print(_preview(raw))
        print("-" * 72)
        print("PRODUCTION _foreground_accessibility_text():")
        print(_preview(production))
        print("-" * 72)
        print(f"STORE A11Y CACHE?  {store_reason}")
        if store:
            if useful:
                print("A11Y USEFUL?     YES — OCR skipped; final DB text = a11y")
                print(f"FINAL DB TEXT?   a11y only ({len(production)} chars)")
            else:
                print("A11Y USEFUL?     NO  — OCR would run; final text = OCR if useful else a11y")
                # Simulate OCR-empty choice so probe shows fallback without running OCR.
                final = choose_screen_text(production, "")
                print(
                    f"FINAL IF OCR EMPTY: a11y fallback ({len(final)} chars) — "
                    "use probe_ocr.py / preview_ocr_crop.py for OCR + crop"
                )
            words = len(production.split())
            alnum = sum(1 for ch in production if ch.isalnum())
            print(f"STATS         chars={len(production)}  words={words}  alnum={alnum}")
        print("=" * 72)
        print()
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
