"""Sidecar files follow the capture timestamp, and leave with the image."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import imagehash
from PIL import Image, ImageDraw

from core.ocr_crop import crop_metadata_path
from core.screenshot_enrichment import extract_screenshot_ocr
from core.screenshot_processor import _collapse_saved_lookalikes, _compute_all_hashes
from core.screenshot_files import (
    PHASH_SIMILAR_DISTANCE,
    capture_stem,
    delete_screenshot_files,
    perceptual_hash,
    sweep_screenshot_sidecars,
)
from core.uia_worker import a11y_text_path


class ScreenshotArtifactTests(unittest.TestCase):
    def test_processed_name_still_finds_the_original_sidecars(self):
        image = Path("1789014069707_processed.jpg")
        self.assertEqual(capture_stem(image), "1789014069707")
        self.assertEqual(crop_metadata_path(image).name, "1789014069707.ocr-crop.json")
        self.assertEqual(a11y_text_path(image).name, "1789014069707.a11y.txt")

    def test_delete_removes_processed_image_and_original_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            names = [
                "1789014069707.jpg",
                "1789014069707_processed.jpg",
                "1789014069707.ocr-crop.json",
                "1789014069707.a11y.txt",
            ]
            for name in names:
                (folder / name).write_text("x", encoding="utf-8")
            delete_screenshot_files(folder / "1789014069707_processed.jpg")
            self.assertEqual(
                sorted(path.name for path in folder.iterdir()),
                ["1789014069707.a11y.txt"],
            )

    def test_sweep_keeps_accessibility_text_while_the_image_remains(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "100_processed.jpg").write_bytes(b"img")
            (folder / "100.ocr-crop.json").write_text("{}", encoding="utf-8")
            (folder / "100.a11y.txt").write_text("already stored", encoding="utf-8")
            (folder / "200.jpg").write_bytes(b"img")
            (folder / "200.ocr-crop.json").write_text("{}", encoding="utf-8")
            (folder / "200.a11y.txt").write_text("still waiting", encoding="utf-8")
            (folder / "300.ocr-crop.json").write_text("{}", encoding="utf-8")
            (folder / "300.a11y.txt").write_text("orphan", encoding="utf-8")
            sweep_screenshot_sidecars(folder)
            remaining = sorted(path.name for path in folder.iterdir())
            self.assertEqual(
                remaining,
                [
                    "100.a11y.txt",
                    "100_processed.jpg",
                    "200.a11y.txt",
                    "200.jpg",
                    "200.ocr-crop.json",
                    "300.a11y.txt",
                ],
            )

    def test_sweep_leaves_accessibility_text_until_cleanup_is_turned_on(self):
        from core.screenshot_files import _A11Y_SWEEP_BATCH

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for index in range(_A11Y_SWEEP_BATCH + 8):
                path = folder / f"{index}.a11y.txt"
                path.write_text("gone", encoding="utf-8")
                os.utime(path, (index, index))
            sweep_screenshot_sidecars(folder)
            self.assertEqual(len(list(folder.glob("*.a11y.txt"))), _A11Y_SWEEP_BATCH + 8)

    def test_collapse_keeps_the_newer_lookalike(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            older = folder / "1000.jpg"
            newer = folder / "2000.jpg"
            Image.new("RGB", (32, 32), "white").save(older)
            Image.new("RGB", (32, 32), "white").save(newer)
            paths = [older, newer]
            _collapse_saved_lookalikes(paths, _compute_all_hashes(paths))
            self.assertFalse(older.exists())
            self.assertTrue(newer.exists())

    def test_preview_hash_matches_the_full_image(self):
        image = Image.new("RGB", (960, 600), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 40, 900, 140), fill=(20, 20, 20))
        draw.rectangle((40, 180, 480, 540), fill=(180, 70, 40))
        draw.rectangle((520, 180, 900, 540), fill=(40, 90, 170))
        preview = perceptual_hash(image)
        self.assertEqual(preview, perceptual_hash(image))
        self.assertLessEqual(preview - imagehash.phash(image), PHASH_SIMILAR_DISTANCE)

    def test_near_full_crop_is_ocr_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._frame_with_crop(Path(tmp), [0, 0, 400, 190])
            with patch("core.screenshot_enrichment.extract_text", return_value="OK") as extract:
                text = extract_screenshot_ocr(path)
            self.assertEqual(text, "OK")
            self.assertEqual(extract.call_count, 1)

    def test_empty_crop_does_not_ocr_the_full_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._frame_with_crop(Path(tmp), [0, 0, 180, 100])
            with patch("core.screenshot_enrichment.extract_text", return_value="") as extract:
                self.assertEqual(extract_screenshot_ocr(path), "")
            self.assertEqual(extract.call_count, 1)

    def test_small_crop_with_unusable_text_still_reads_the_full_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._frame_with_crop(Path(tmp), [0, 0, 180, 100])
            with patch(
                "core.screenshot_enrichment.extract_text",
                side_effect=["OK", "A real paragraph of notes about the capture pipeline"],
            ) as extract:
                text = extract_screenshot_ocr(path)
            self.assertIn("paragraph", text)
            self.assertEqual(extract.call_count, 2)

    def test_field_scan_timeout_keeps_the_frame(self):
        from core import screenshot_scheduler as scheduler

        self._reset_redaction_cache(scheduler)
        with patch("core.screenshot_scheduler.collect_redaction_safe", return_value=None):
            redaction = scheduler._field_redaction(1, "browser\x1fgmail", 1.5)
        self.assertEqual(redaction["edit_rects"], [])
        self.assertEqual(redaction["secret_rects"], [])

    def test_same_window_reuses_the_field_scan(self):
        from core import screenshot_scheduler as scheduler

        self._reset_redaction_cache(scheduler)
        found = {"edit_rects": [(1, 2, 30, 40)], "secret_rects": [], "redacted_text": "secret"}
        with patch("core.screenshot_scheduler.collect_redaction_safe", return_value=found) as walk:
            self.assertEqual(scheduler._field_redaction(1, "same-window", 1.5), found)
            self.assertEqual(scheduler._field_redaction(1, "same-window", 1.5), found)
        self.assertEqual(walk.call_count, 1)

    def _frame_with_crop(self, folder: Path, box: list[int]) -> Path:
        path = folder / "1000.jpg"
        Image.new("RGB", (400, 200), "white").save(path, format="JPEG")
        crop_metadata_path(path).write_text(
            json.dumps({"version": 1, "source": "heuristic", "box": box, "image_size": [400, 200]}),
            encoding="utf-8",
        )
        return path

    def _reset_redaction_cache(self, scheduler) -> None:
        scheduler._last_redaction_key = ""
        scheduler._last_redaction = None
        scheduler._last_redaction_at = 0.0
