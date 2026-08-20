"""
Write visible OCR crop previews so the chosen work-region can be inspected.

Modes:
  live                 grab the foreground window now, save shot + crop preview
  <screenshot.jpg>     preview the crop for one stored screenshot
  --latest N           preview the N newest stored screenshots

Stored screenshots keep their crop in <name>.ocr-crop.json. Frames captured
before that metadata existed fall back to the heuristic crop.

Usage (PowerShell):
  cd c:\\Users\\proto\\Clippy_Vision
  $env:PYTHONPATH = (Get-Location).Path
  python .\\scripts\\preview_ocr_crop.py live
  python .\\scripts\\preview_ocr_crop.py --latest 3
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image

from core.accessibility_text import foreground_content_bounds
from core.ocr_crop import (
    crop_screenshot_for_ocr,
    load_crop_metadata,
    save_crop_metadata,
)
from core.paths import get_screenshots_dir

PREVIEW_DIR = get_screenshots_dir() / "crop_previews"


def _preview_path(source: Path) -> Path:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    return PREVIEW_DIR / f"{source.stem}_crop.jpg"


def _write_preview(source: Path) -> None:
    metadata = load_crop_metadata(source)
    if not metadata:
        with Image.open(source) as image:
            width, height = image.size
        # No stored geometry (frame predates crop metadata) — heuristic only.
        metadata = save_crop_metadata(
            source,
            image_width=width,
            image_height=height,
            monitor={"left": 0, "top": 0, "width": width, "height": height},
            a11y_bounds=None,
        )
        if not metadata:
            print(f"{source.name}: could not build a crop")
            return

    target = _preview_path(source)
    result = crop_screenshot_for_ocr(source, target)
    if not result:
        print(f"{source.name}: crop failed")
        return
    with Image.open(target) as cropped:
        size = cropped.size
    print(f"{source.name}: source={result['source']} box={result['box']} size={size[0]}x{size[1]}")
    print(f"  preview: {target}")


def _grab_live() -> Path | None:
    try:
        import win32gui
        from PIL import ImageGrab
    except Exception as exc:
        print(f"live capture unavailable: {exc}")
        return None

    hwnd = win32gui.GetForegroundWindow()
    monitor = {"left": 0, "top": 0, "width": 0, "height": 0}
    if hwnd:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right > left and bottom > top:
            image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
            monitor = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        else:
            image = ImageGrab.grab(all_screens=True)
    else:
        image = ImageGrab.grab(all_screens=True)
    if not monitor["width"]:
        monitor = {"left": 0, "top": 0, "width": image.width, "height": image.height}

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    source = PREVIEW_DIR / f"live_{int(time.time())}.jpg"
    image.save(source, format="JPEG", quality=90)
    save_crop_metadata(
        source,
        image_width=image.width,
        image_height=image.height,
        monitor=monitor,
        a11y_bounds=foreground_content_bounds(),
    )
    print(f"live shot: {source}  ({image.width}x{image.height})")
    return source


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview OCR crops as images")
    parser.add_argument("target", nargs="?", help="'live' or a screenshot path")
    parser.add_argument("--latest", type=int, default=0, help="Preview N newest stored screenshots")
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds to wait before a live grab")
    args = parser.parse_args()

    if args.target == "live":
        print(f"Focus the window you want, waiting {args.delay:.0f}s…")
        time.sleep(max(0.0, args.delay))
        source = _grab_live()
        if source is not None:
            _write_preview(source)
        return

    if args.target:
        source = Path(args.target)
        if not source.exists():
            source = get_screenshots_dir() / args.target
        if not source.exists():
            print(f"not found: {args.target}")
            sys.exit(1)
        _write_preview(source)
        return

    count = args.latest or 3
    shots = [
        path
        for path in get_screenshots_dir().glob("*.jpg")
        if not path.stem.endswith("_crop")
    ]
    shots.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    if not shots:
        print("no stored screenshots yet")
        return
    for path in shots[:count]:
        _write_preview(path)


if __name__ == "__main__":
    main()
