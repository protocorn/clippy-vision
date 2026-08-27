from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from core.ocr_crop import (
    crop_screenshot_for_ocr,
    heuristic_content_crop,
    load_crop_metadata,
    normalize_a11y_bounds,
    save_crop_metadata,
)


class OcrCropTests(unittest.TestCase):
    def test_a11y_bounds_map_to_screenshot_pixels(self):
        box = normalize_a11y_bounds(
            (200, 100, 800, 500),
            monitor={"left": 0, "top": 0, "width": 1000, "height": 600},
            image_width=2000,
            image_height=1200,
        )
        self.assertEqual(box, (400, 200, 1600, 1000))

    def test_heuristic_crop_removes_window_edges(self):
        self.assertEqual(heuristic_content_crop(1000, 600), (140, 48, 980, 564))

    def test_saved_crop_can_be_materialized_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "capture.jpg"
            target = Path(tmp) / "crop.jpg"
            Image.new("RGB", (1000, 600), "white").save(source)
            metadata = save_crop_metadata(
                source,
                image_width=1000,
                image_height=600,
                monitor={"left": 0, "top": 0, "width": 1000, "height": 600},
                a11y_bounds=(200, 100, 800, 500),
            )
            self.assertEqual(metadata["source"], "a11y-region")
            self.assertEqual(load_crop_metadata(source)["box"], [200, 100, 800, 500])
            result = crop_screenshot_for_ocr(source, target)
            self.assertEqual(result["source"], "a11y-region")
            with Image.open(target) as cropped:
                self.assertEqual(cropped.size, (600, 400))


if __name__ == "__main__":
    unittest.main()
