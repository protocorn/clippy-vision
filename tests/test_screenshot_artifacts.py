"""Sidecar files follow the capture timestamp, and leave with the image."""

import json
import os
import sys
import tempfile
import threading
import time
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
    adopt_processed_filename,
    capture_stem,
    delete_screenshot_files,
    perceptual_hash,
    record_accessibility_text,
    resolve_screenshot_file,
    retarget_screenshot_filename,
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

    def _release_walk(self, scheduler, redaction: dict) -> None:
        slot = redaction.get("slot") if isinstance(redaction, dict) else None
        if slot is not None:
            slot["saved"].set()
        for _ in range(100):
            if scheduler._walk_lock.acquire(blocking=False):
                scheduler._walk_lock.release()
                return
            time.sleep(0.01)

    def test_field_scan_failure_does_not_invent_text(self):
        from core import screenshot_scheduler as scheduler

        with patch("core.screenshot_scheduler.collect_redaction", return_value=None):
            redaction = scheduler._field_redaction(1, "browser\x1fgmail", 1.5)
        self.addCleanup(lambda: self._release_walk(scheduler, redaction))
        self.assertEqual(redaction["redacted_text"], "")
        self.assertFalse(redaction.get("busy"))

    def test_field_scan_failure_keeps_the_frame_rects_empty(self):
        from core import screenshot_scheduler as scheduler

        with patch("core.screenshot_scheduler.collect_redaction", return_value=None):
            redaction = scheduler._field_redaction(1, "browser\x1fgmail", 1.5)
        self.addCleanup(lambda: self._release_walk(scheduler, redaction))
        self.assertEqual(redaction["edit_rects"], [])
        self.assertEqual(redaction["secret_rects"], [])

    def test_same_window_walks_again_instead_of_reusing_text(self):
        from core import screenshot_scheduler as scheduler

        found = {"edit_rects": [(1, 2, 30, 40)], "secret_rects": [], "redacted_text": "secret"}
        with patch("core.screenshot_scheduler.collect_redaction", return_value=found) as walk:
            first = scheduler._field_redaction(1, "same-window", 1.5)
            self._release_walk(scheduler, first)
            second = scheduler._field_redaction(1, "same-window", 1.5)
            self._release_walk(scheduler, second)
        self.assertEqual(first["redacted_text"], "secret")
        self.assertEqual(second["redacted_text"], "secret")
        self.assertEqual(walk.call_count, 2)

    def test_a_busy_walk_does_not_copy_text_onto_the_next_frame(self):
        from core import screenshot_scheduler as scheduler

        started = threading.Event()
        release = threading.Event()

        def _slow(_hwnd):
            started.set()
            release.wait(2)
            return {"edit_rects": [], "secret_rects": [], "redacted_text": "first document", "content_bounds": None}

        with patch("core.screenshot_scheduler.collect_redaction", side_effect=_slow):
            holder: dict = {}

            def _run():
                holder["first"] = scheduler._field_redaction(1, "same-window", 2.0)

            worker = threading.Thread(target=_run)
            worker.start()
            self.assertTrue(started.wait(1))
            second = scheduler._field_redaction(1, "same-window", 0.2)
            release.set()
            worker.join(3)
        self.addCleanup(lambda: self._release_walk(scheduler, holder.get("first") or {}))
        self.assertTrue(second.get("busy"))
        self.assertEqual(second["redacted_text"], "")
        self.assertIn("first document", holder["first"]["redacted_text"])

    def test_walk_skips_text_when_the_window_changed(self):
        from core import screenshot_scheduler as scheduler

        job = {"hwnd": 5, "title": "ChatGPT", "process": "chrome.exe"}
        with patch.object(scheduler, "_foreground_hwnd", return_value=9):
            self.assertFalse(scheduler._walk_still_matches(job))
        with patch.object(scheduler, "_foreground_hwnd", return_value=5), patch.object(
            scheduler, "_hwnd_title", return_value="A job posting"
        ):
            self.assertFalse(scheduler._walk_still_matches(job))
        with patch.object(scheduler, "_foreground_hwnd", return_value=5), patch.object(
            scheduler, "_hwnd_title", return_value="ChatGPT"
        ):
            self.assertTrue(scheduler._walk_still_matches(job))
        self.assertTrue(scheduler._walk_still_matches({"hwnd": 1}))

    def test_queued_walks_keep_a_jpeg_for_each_frame(self):
        from core import screenshot_scheduler as scheduler
        from core.uia_worker import read_persisted_accessibility_text

        started = threading.Event()
        release = threading.Event()

        def _slow(hwnd):
            if hwnd == 1:
                started.set()
                release.wait(2)
            return {
                "edit_rects": [],
                "secret_rects": [],
                "redacted_text": "first document" if hwnd == 1 else "second document",
                "content_bounds": None,
            }

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            first = folder / "1000.jpg"
            second = folder / "2000.jpg"
            Image.new("RGB", (40, 40), "white").save(first, format="JPEG")
            Image.new("RGB", (40, 40), "white").save(second, format="JPEG")
            monitor = {"left": 0, "top": 0, "width": 40, "height": 40}
            with patch("core.screenshot_scheduler.collect_redaction", side_effect=_slow):
                scheduler._enqueue_walk(
                    {"hwnd": 1, "path": first, "key": "one", "monitor": monitor, "image_size": (40, 40)}
                )
                self.assertTrue(started.wait(1))
                scheduler._enqueue_walk(
                    {"hwnd": 2, "path": second, "key": "two", "monitor": monitor, "image_size": (40, 40)}
                )
                release.set()
                deadline = time.time() + 10
                while time.time() < deadline:
                    if "second document" in read_persisted_accessibility_text(second):
                        break
                    time.sleep(0.05)
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            self.assertIn("first document", read_persisted_accessibility_text(first))
            self.assertIn("second document", read_persisted_accessibility_text(second))

    def _frame_with_crop(self, folder: Path, box: list[int]) -> Path:
        path = folder / "1000.jpg"
        Image.new("RGB", (400, 200), "white").save(path, format="JPEG")
        crop_metadata_path(path).write_text(
            json.dumps({"version": 1, "source": "heuristic", "box": box, "image_size": [400, 200]}),
            encoding="utf-8",
        )
        return path

    def test_late_accessibility_text_fills_an_empty_event(self):
        from core.paths import get_screenshots_dir
        from core.storage import conn

        event_id = "late-a11y-fill"
        conn.execute(
            """INSERT INTO events (
                   event_id, session_id, timestamp, event_type, summary,
                   screenshot_filename, vision_ocr_text, interest_reason,
                   expires_at, classification_status
               ) VALUES (?, 'test-session', 1, 'screenshot_analysis', 'shot',
                         '1790710686569.jpg', '', 'No accessibility or OCR text was available',
                         9999999999, 'done')""",
            (event_id,),
        )
        conn.commit()
        self.addCleanup(lambda: (conn.execute("DELETE FROM events WHERE event_id=?", (event_id,)), conn.commit()))
        folder = get_screenshots_dir()
        path = folder / "1790710686569.jpg"
        text = "okay it works. the agent message should be stored with the screenshot."
        record_accessibility_text(path, text)
        row = conn.execute(
            "SELECT vision_ocr_text, interest_reason, screenshot_filename FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        self.assertEqual(row[0], text)
        self.assertEqual(row[1], "Local accessibility and OCR text capture completed")

        adopt_processed_filename("1790710686569.jpg", "1790710686569_processed.jpg")
        renamed = conn.execute(
            "SELECT screenshot_filename FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        self.assertEqual(renamed[0], "1790710686569_processed.jpg")
        record_accessibility_text(folder / "1790710686569_processed.jpg", text + " more of the same walk")
        grown = conn.execute(
            "SELECT vision_ocr_text FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        self.assertEqual(grown[0], text + " more of the same walk")
        record_accessibility_text(path, "Review")
        kept = conn.execute(
            "SELECT vision_ocr_text FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        self.assertEqual(kept[0], text + " more of the same walk")

    def test_processed_lookalike_takes_the_kept_sidecar_text(self):
        from core.storage import conn

        event_id = "lookalike-retarget"
        conn.execute(
            """INSERT INTO events (
                   event_id, session_id, timestamp, event_type, summary,
                   screenshot_filename, vision_ocr_text, interest_reason,
                   expires_at, classification_status
               ) VALUES (?, 'test-session', 2, 'screenshot_analysis', 'shot',
                         '100.jpg', 'old frame text that should not stay',
                         'Local accessibility and OCR text capture completed',
                         9999999999, 'done')""",
            (event_id,),
        )
        conn.commit()
        self.addCleanup(lambda: (conn.execute("DELETE FROM events WHERE event_id=?", (event_id,)), conn.commit()))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            kept = folder / "200_processed.jpg"
            (folder / "200.a11y.txt").write_text("the kept frame text", encoding="utf-8")
            with patch("core.paths.get_screenshots_dir", return_value=folder):
                retarget_screenshot_filename(["100.jpg", "100_processed.jpg"], kept.name)
        row = conn.execute(
            "SELECT screenshot_filename, vision_ocr_text FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        self.assertEqual(row, (kept.name, "the kept frame text"))

    def test_resolve_screenshot_file_accepts_the_processed_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            image = folder / "1790710876077_processed.jpg"
            Image.new("RGB", (2, 2), "white").save(image, format="JPEG")
            self.assertEqual(resolve_screenshot_file(folder, "1790710876077.jpg"), image)
            self.assertIsNone(resolve_screenshot_file(folder, "../secret.jpg"))

    def test_covered_text_is_marked_and_deleted_on_a_later_sweep(self):
        from core import screenshot_scheduler as scheduler

        scheduler._kept_frames.clear()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            earlier = folder / "100.jpg"
            later = folder / "200.jpg"
            Image.new("RGB", (8, 8), "white").save(earlier)
            Image.new("RGB", (8, 8), "white").save(later)
            tab = "editor.exe\x1fnotes\x1f"
            draft = "hello there friend this is the draft"
            scheduler._note_frame(earlier, tab, draft, pending=False)
            scheduler._note_frame(later, tab, draft + " and the rest of the paragraph", pending=False)
            scheduler._mark_covered_frames()
            self.assertIsNotNone(scheduler._kept_frames[0]["marked_at"])
            self.assertTrue(earlier.exists())
            scheduler._kept_frames[0]["marked_at"] = time.time() - scheduler.DISCARD_AFTER_SECONDS - 1
            scheduler.sweep_discarded_frames()
            self.assertFalse(earlier.exists())
            self.assertTrue(later.exists())
        scheduler._kept_frames.clear()
