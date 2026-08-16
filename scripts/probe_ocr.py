"""
Probe OCR the same way enrichment does after weak accessibility text.

Every 5 seconds:
  - captures a screenshot of the foreground window (or full virtual screen)
  - runs accessibility extract (production-gated)
  - decides whether OCR would run
  - runs RapidOCR when a11y is not useful
  - shows choose_screen_text() final winner (a11y vs OCR)

Usage (PowerShell):
  cd c:\\Users\\proto\\Clippy_Vision
  $env:PYTHONPATH = (Get-Location).Path
  python .\\scripts\\probe_ocr.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.accessibility_text import foreground_content_bounds, is_useful_accessibility_text
from core.app_settings import get_capture_settings
from core.ocr_crop import load_crop_metadata, save_crop_metadata
from core.platform_support import get_window_metadata
from core.privacy_settings import is_clippy_window, should_redact_window
from core.screenshot_enrichment import choose_screen_text, extract_screenshot_ocr
from core.vision import _foreground_accessibility_text

POLL_SECS = 8


def _preview(text: str, limit: int = 1000) -> str:
    cleaned = text.replace("\r\n", "\n").strip()
    if not cleaned:
        return "(empty)"
    if len(cleaned) > limit:
        return cleaned[:limit] + f"\n... [{len(cleaned) - limit} more chars]"
    return cleaned


def _grab_screenshot(path: Path) -> tuple[bool, dict]:
    try:
        from PIL import ImageGrab
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        if hwnd:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if right > left and bottom > top:
                image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
                image.save(path, format="JPEG", quality=85)
                return True, {
                    "left": left,
                    "top": top,
                    "width": right - left,
                    "height": bottom - top,
                }
        image = ImageGrab.grab(all_screens=True)
        image.save(path, format="JPEG", quality=85)
        return True, {"left": 0, "top": 0, "width": image.width, "height": image.height}
    except Exception as exc:
        print(f"screenshot failed: {exc}")
        return False, {}


def main() -> None:
    settings = get_capture_settings()
    print("OCR probe — Ctrl+C to stop")
    print(f"Polling every {POLL_SECS}s  ocr_enabled={settings.get('ocr_enabled')}")
    print("Cascade: useful a11y → a11y only; else useful OCR → OCR only; else a11y fallback\n")

    while True:
        stamp = time.strftime("%H:%M:%S")
        meta = get_window_metadata() or {}
        process_name = str(meta.get("process_name") or "")
        title = str(meta.get("current_window_title") or "")

        print("=" * 72)
        print(f"[{stamp}] window: {process_name or '(unknown)'} — {title or '(no title)'}")

        if is_clippy_window(process_name, title) or should_redact_window(process_name, title):
            print("SKIP — redacted / Clippy window")
            print("=" * 72)
            print()
            time.sleep(POLL_SECS)
            continue

        a11y = _foreground_accessibility_text()
        a11y_useful = is_useful_accessibility_text(a11y) if a11y.strip() else False
        print("-" * 72)
        print(f"A11Y useful={a11y_useful} chars={len(a11y)}")
        print(_preview(a11y, 500))
        print("-" * 72)

        if not settings.get("ocr_enabled", True):
            final = choose_screen_text(a11y, "")
            print("OCR disabled in settings — final = choose_screen_text(a11y, '')")
            print(_preview(final))
            print("=" * 72)
            print()
            time.sleep(POLL_SECS)
            continue

        if a11y_useful:
            final = choose_screen_text(a11y, "")
            print("OCR SKIPPED — a11y alone is enough")
            print(f"FINAL SOURCE: a11y  chars={len(final)}")
            print(_preview(final))
        else:
            print("OCR RUNNING — a11y not useful enough…")
            with tempfile.TemporaryDirectory(prefix="clippy_ocr_probe_") as tmp:
                shot = Path(tmp) / "probe.jpg"
                grabbed, monitor = _grab_screenshot(shot)
                if not grabbed:
                    print("FINAL SOURCE: (screenshot failed)")
                else:
                    save_crop_metadata(
                        shot,
                        image_width=monitor["width"],
                        image_height=monitor["height"],
                        monitor=monitor,
                        a11y_bounds=foreground_content_bounds(),
                    )
                    crop = load_crop_metadata(shot)
                    print(f"OCR CROP: {crop.get('source', 'none')} {crop.get('box', '')}")
                    t0 = time.perf_counter()
                    ocr = extract_screenshot_ocr(shot)
                    ms = (time.perf_counter() - t0) * 1000
                    ocr_useful = is_useful_accessibility_text(ocr) if ocr.strip() else False
                    final = choose_screen_text(a11y, ocr)
                    if is_useful_accessibility_text(a11y):
                        source = "a11y"
                    elif ocr_useful:
                        source = "ocr"
                    elif a11y.strip():
                        source = "a11y-fallback"
                    else:
                        source = "ocr-crumbs" if ocr.strip() else "empty"
                    print(f"OCR useful={ocr_useful} chars={len(ocr)}  elapsed={ms:.0f}ms")
                    if not ocr.strip():
                        try:
                            from core.model_residency import can_run_ocr

                            if not can_run_ocr():
                                print("NOTE: OCR returned empty — memory gate can_run_ocr() is False")
                        except Exception:
                            pass
                    print(_preview(ocr, 500))
                    print("-" * 72)
                    print(f"FINAL SOURCE: {source}  chars={len(final)}")
                    print(_preview(final))

        print("=" * 72)
        print()
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
