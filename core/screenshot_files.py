"""Screenshot image and sidecar paths.

Crop and accessibility files are named from the capture timestamp.
``1789014069707_processed.jpg`` still owns ``1789014069707.ocr-crop.json``
and ``1789014069707.a11y.txt``. Deleting the JPEG has to use that stem;
``Path.with_suffix`` on the processed name looks for a file that was never written.
"""

from __future__ import annotations

from pathlib import Path

import imagehash
from PIL import Image

# pHash already shrinks to a tiny grid. Doing that from a full desktop
# screenshot is the slow part. A 512px preview hashes the same for a
# real screen (distance 0 versus the full image) and is much faster.
_HASH_LONG_EDGE = 512

# 64-bit pHash. 0 is the same frame. About 6 still looks like the same screen
# (cursor, clock, scrollbar). A different page is usually well above that.
PHASH_SIMILAR_DISTANCE = 6
# Background shots land about once a minute. Collapse lookalikes across a few
# of those intervals and keep the newest. A later return to a similar screen
# stays as its own frame.
SIMILAR_FRAME_WINDOW_MS = 180_000

_PROCESSED = "_processed"


def perceptual_hash(image: Image.Image):
    """Hash a screen-sized image from a small preview."""
    width, height = image.size
    long_edge = max(width, height)
    if long_edge > _HASH_LONG_EDGE:
        scale = _HASH_LONG_EDGE / long_edge
        image = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.BILINEAR,
        )
    return imagehash.phash(image)


def capture_stem(path: Path) -> str:
    stem = path.stem
    if stem.endswith(_PROCESSED):
        return stem[: -len(_PROCESSED)]
    return stem


def screenshot_stamp_ms(path: Path) -> int | None:
    try:
        return int(capture_stem(path).split("_", 1)[0])
    except ValueError:
        return None


def image_names(stem: str) -> tuple[str, str]:
    return f"{stem}.jpg", f"{stem}_processed.jpg"


def artifact_paths(path: Path) -> list[Path]:
    folder = path.parent
    stem = capture_stem(path)
    raw, processed = image_names(stem)
    return [
        folder / raw,
        folder / processed,
        folder / f"{stem}.a11y.txt",
        folder / f"{stem}.a11y.tmp",
        folder / f"{stem}.ocr-crop.json",
        folder / f"{stem}.tmp",
    ]


# Oldest orphaned accessibility files removed on one sweep. A sweep runs
# about once a minute, so this drains leftovers without a delete per frame
# and without clearing the folder in one pass.
_A11Y_SWEEP_BATCH = 32
# Leave the text files in place so a capture can be read after the image
# moves or a session is deleted. The batch sweep below stays for later.
KEEP_ACCESSIBILITY_TEXT = True


def delete_screenshot_files(path: Path, *, include_text: bool = True) -> None:
    """Remove the image and its sidecars.

    ``include_text=False`` keeps the accessibility file. Capture uses that
    when a newer lookalike replaces the image, so the text stays readable
    and is cleared later with the other leftovers.
    """
    skip = set()
    if KEEP_ACCESSIBILITY_TEXT or not include_text:
        stem = capture_stem(path)
        skip.add(path.parent / f"{stem}.a11y.txt")
    for candidate in artifact_paths(path):
        if candidate in skip:
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


def release_text_sidecars(path: Path) -> None:
    """Drop the crop once its text is stored. Keep the image and the accessibility text."""
    folder = path.parent
    stem = capture_stem(path)
    for name in (f"{stem}.a11y.tmp", f"{stem}.ocr-crop.json"):
        try:
            (folder / name).unlink(missing_ok=True)
        except OSError:
            pass


def retarget_screenshot_filename(old_names: list[str], new_name: str) -> None:
    names = [name for name in old_names if name and name != new_name]
    if not names or not new_name:
        return
    try:
        from core.storage import conn
    except ImportError:
        from storage import conn
    placeholders = ",".join("?" for _ in names)
    conn.execute(
        f"UPDATE events SET screenshot_filename = ? WHERE screenshot_filename IN ({placeholders})",
        [new_name, *names],
    )
    conn.commit()


def sweep_screenshot_sidecars(directory: Path) -> None:
    """Delete leftover crop files, and a batch of orphaned accessibility text.

    A crop goes when its image is already gone, or when the frame has been
    processed. Accessibility text stays while its image is still there, so a
    capture can be read after it is stored. Once the image is gone, only the
    oldest batch is removed on this pass.
    """
    if not directory.is_dir():
        return
    live: set[str] = set()
    processed: set[str] = set()
    for path in directory.glob("*.jpg"):
        stem = capture_stem(path)
        live.add(stem)
        if path.stem.endswith(_PROCESSED):
            processed.add(stem)
    for path in directory.glob("*.ocr-crop.json"):
        stem = path.name[: -len(".ocr-crop.json")]
        if stem not in live or stem in processed:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    if KEEP_ACCESSIBILITY_TEXT:
        return
    orphans: list[Path] = []
    for pattern in ("*.a11y.txt", "*.a11y.tmp"):
        for path in directory.glob(pattern):
            stem = path.name.split(".a11y", 1)[0]
            if stem not in live:
                orphans.append(path)
    orphans.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0)
    for path in orphans[:_A11Y_SWEEP_BATCH]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
