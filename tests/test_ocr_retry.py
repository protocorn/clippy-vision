"""OCR pressure-gate skip logging and deferred retry."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-ocr-retry-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from core.events import Event, WindowMetadata
from core.ocr import extract_text_detail
from core.ocr_retry import (
    OCR_RETRY_BATCH,
    OCR_RETRY_BACKOFF_MAX_SECS,
    OCR_RETRY_BACKOFF_SECS,
    clear_ocr_retry,
    ocr_retry_path,
    pending_ocr_retries,
    retry_skipped_ocr,
    schedule_ocr_retry,
)
from core.screenshot_enrichment import enrich_screenshot, remember_accessibility_text
from core.screenshot_files import delete_screenshot_files
from core.storage import conn, store_event


def _jpeg(folder: Path, name: str) -> Path:
    path = folder / name
    Image.new("RGB", (8, 8), "white").save(path, format="JPEG")
    return path


def _event_with_screenshot(event_id: str, filename: str, text: str = "") -> Event:
    stamp = time.time()
    event = Event(
        event_id=event_id,
        session_id="test-session",
        timestamp=stamp,
        event_type="screenshot_analysis",
        window_context=WindowMetadata(
            timestamp=stamp,
            current_window_title="Docs",
            active_url=None,
            process_name="chrome",
        ),
        previous_window_context=None,
        payload={},
        summary="frame",
        vector_embedding=None,
        image_embedding=None,
        image_embedding_model=None,
        screenshot_filename=filename,
        interest_score=None,
        interest_reason="No accessibility or OCR text was available",
        interesting=None,
    )
    store_event(event)
    conn.execute(
        """UPDATE events
           SET screenshot_filename=?, vision_ocr_text=?,
               interest_reason='No accessibility or OCR text was available',
               classification_status='done'
           WHERE event_id=?""",
        (filename, text, event_id),
    )
    conn.commit()
    return event


class OcrSkipReasonTests(unittest.TestCase):
    def test_ocr_unavailable_reason_names_cpu(self):
        from core import model_residency as residency

        with patch.object(residency, "_available", return_value=int(8 * residency._GB)), patch.object(
            residency, "_commit_pressured", return_value=False
        ), patch.object(residency, "_cpu_pressured", return_value=True):
            reason = residency.ocr_unavailable_reason()
            self.assertIsNotNone(reason)
            self.assertIn("CPU", reason)
            self.assertFalse(residency.can_run_ocr())

    def test_ocr_unavailable_reason_names_memory_floor(self):
        from core import model_residency as residency

        with patch.object(residency, "_available", return_value=int(0.2 * residency._GB)), patch.object(
            residency, "_commit_pressured", return_value=False
        ), patch.object(residency, "_cpu_pressured", return_value=False):
            reason = residency.ocr_unavailable_reason()
            self.assertIn("memory", reason)
            self.assertIn("OCR floor", reason)
            self.assertFalse(residency.can_run_ocr())

    def test_ocr_unavailable_reason_names_commit_pressure(self):
        from core import model_residency as residency

        with patch.object(residency, "_available", return_value=int(8 * residency._GB)), patch.object(
            residency, "_commit_pressured", return_value=True
        ), patch.object(residency, "_cpu_pressured", return_value=False):
            reason = residency.ocr_unavailable_reason()
            self.assertIn("memory", reason)
            self.assertFalse(residency.can_run_ocr())

    def test_pressure_gate_thresholds_are_unchanged(self):
        from core import model_residency as residency

        self.assertEqual(residency._CPU_PRESSURE_PCT, 80.0)
        self.assertEqual(residency._OCR_FLOOR, 0.5 * residency._GB)
        self.assertEqual(residency._COMMIT_PRESSURE_PCT, 75.0)

    def test_extract_logs_cpu_skip_and_does_not_run_engine(self):
        with patch(
            "core.model_residency.ocr_unavailable_reason",
            return_value="CPU: system usage >= 80%",
        ), patch("core.ocr._get_engine") as engine:
            with patch("sys.stdout", new=io.StringIO()) as out:
                detail = extract_text_detail(Path("office-assistant.jpg"))
        self.assertTrue(detail["gated"])
        self.assertIn("CPU", detail["gate_reason"])
        self.assertEqual(detail["text"], "")
        self.assertIn("[ocr] skipped office-assistant.jpg — CPU: system usage >= 80%", out.getvalue())
        engine.assert_not_called()

    def test_extract_logs_memory_skip(self):
        with patch(
            "core.model_residency.ocr_unavailable_reason",
            return_value="memory: free RAM 0.3GB is below the 0.5GB OCR floor",
        ), patch("core.ocr._get_engine") as engine:
            with patch("sys.stdout", new=io.StringIO()) as out:
                detail = extract_text_detail(Path("qwen3-library.jpg"))
        self.assertTrue(detail["gated"])
        self.assertIn("memory", detail["gate_reason"])
        self.assertIn("[ocr] skipped qwen3-library.jpg — memory:", out.getvalue())
        engine.assert_not_called()


class OcrRetryQueueTests(unittest.TestCase):
    def tearDown(self):
        conn.execute("DELETE FROM events WHERE event_id LIKE 'ocr-retry-%'")
        conn.commit()

    def test_enrich_queues_retry_when_ocr_is_gated(self):
        folder = Path(tempfile.mkdtemp(prefix="clippy-ocr-enrich-"))
        path = _jpeg(folder, "1000.jpg")
        with patch(
            "core.model_residency.ocr_unavailable_reason",
            return_value="CPU: system usage >= 80%",
        ), patch(
            "core.screenshot_enrichment.get_capture_settings",
            return_value={"ocr_enabled": True, "image_embeddings_enabled": False},
        ):
            with patch("sys.stdout", new=io.StringIO()) as out:
                text, _, _ = enrich_screenshot(path)
        self.assertEqual(text, "")
        sidecar = ocr_retry_path(path)
        self.assertTrue(sidecar.exists())
        log = out.getvalue()
        self.assertIn("skipped 1000.jpg", log)
        self.assertIn("CPU", log)
        self.assertIn("queued retry for 1000.jpg", log)

    def test_useful_accessibility_does_not_queue_ocr_retry(self):
        folder = Path(tempfile.mkdtemp(prefix="clippy-ocr-a11y-"))
        path = _jpeg(folder, "2000.jpg")
        remember_accessibility_text(
            path,
            "Project settings\nConfigure local capture and privacy controls for this session",
        )
        with patch(
            "core.model_residency.ocr_unavailable_reason",
            return_value="CPU: system usage >= 80%",
        ), patch(
            "core.screenshot_enrichment.get_capture_settings",
            return_value={"ocr_enabled": True, "image_embeddings_enabled": False},
        ), patch("core.screenshot_enrichment.extract_text") as extract:
            enrich_screenshot(path)
        extract.assert_not_called()
        self.assertFalse(ocr_retry_path(path).exists())

    def test_skipped_frame_is_retried_when_pressure_clears(self):
        folder = Path(tempfile.mkdtemp(prefix="clippy-ocr-fill-"))
        path = _jpeg(folder, "3000.jpg")
        _event_with_screenshot("ocr-retry-fill", path.name)
        schedule_ocr_retry(path, "CPU: system usage >= 80%", now=time.time() - 60)

        recovered = (
            "AppImages require FUSE to run. Use --appimage-extract if FUSE is missing."
        )
        with patch("core.model_residency.can_run_ocr", return_value=True), patch(
            "core.screenshot_enrichment.extract_screenshot_ocr", return_value=recovered
        ), patch("core.ocr.last_gate_reason", return_value=None):
            filled = retry_skipped_ocr(folder, now=time.time(), limit=5)

        self.assertEqual(filled, 1)
        self.assertFalse(ocr_retry_path(path).exists())
        row = conn.execute(
            "SELECT vision_ocr_text, interest_reason FROM events WHERE event_id=?",
            ("ocr-retry-fill",),
        ).fetchone()
        self.assertIn("AppImages require FUSE", row[0])
        self.assertEqual(row[1], "Local accessibility and OCR text capture completed")

    def test_retry_does_not_run_while_pressured(self):
        folder = Path(tempfile.mkdtemp(prefix="clippy-ocr-hold-"))
        path = _jpeg(folder, "4000.jpg")
        _event_with_screenshot("ocr-retry-hold", path.name)
        schedule_ocr_retry(path, "CPU: system usage >= 80%", now=time.time() - 60)

        with patch("core.model_residency.can_run_ocr", return_value=False), patch(
            "core.screenshot_enrichment.extract_screenshot_ocr"
        ) as extract:
            filled = retry_skipped_ocr(folder, now=time.time(), limit=5)

        self.assertEqual(filled, 0)
        extract.assert_not_called()
        self.assertTrue(ocr_retry_path(path).exists())

    def test_retry_batch_is_capped_under_sustained_clearing(self):
        folder = Path(tempfile.mkdtemp(prefix="clippy-ocr-batch-"))
        paths = []
        for index in range(5):
            path = _jpeg(folder, f"{5000 + index}.jpg")
            _event_with_screenshot(f"ocr-retry-batch-{index}", path.name)
            schedule_ocr_retry(path, "CPU: system usage >= 80%", now=time.time() - 60)
            paths.append(path)

        with patch("core.model_residency.can_run_ocr", return_value=True), patch(
            "core.screenshot_enrichment.extract_screenshot_ocr",
            return_value="Recovered page text from OCR",
        ) as extract, patch("core.ocr.last_gate_reason", return_value=None):
            filled = retry_skipped_ocr(folder, now=time.time())

        self.assertEqual(OCR_RETRY_BATCH, 2)
        self.assertEqual(filled, 2)
        self.assertEqual(extract.call_count, 2)
        remaining = [path for path in paths if ocr_retry_path(path).exists()]
        self.assertEqual(len(remaining), 3)

    def test_backoff_doubles_and_is_capped(self):
        folder = Path(tempfile.mkdtemp(prefix="clippy-ocr-backoff-"))
        path = _jpeg(folder, "6000.jpg")
        first = schedule_ocr_retry(path, "CPU: system usage >= 80%", now=1000.0)
        self.assertEqual(first["attempts"], 0)
        self.assertEqual(first["next_at"], 1000.0 + OCR_RETRY_BACKOFF_SECS)
        second = schedule_ocr_retry(
            path, "CPU: system usage >= 80%", attempted=True, now=1000.0
        )
        self.assertEqual(second["attempts"], 1)
        self.assertEqual(second["next_at"], 1000.0 + OCR_RETRY_BACKOFF_SECS * 2)
        for _ in range(8):
            schedule_ocr_retry(path, "CPU: system usage >= 80%", attempted=True, now=1000.0)
        later = schedule_ocr_retry(
            path, "CPU: system usage >= 80%", attempted=True, now=1000.0
        )
        self.assertEqual(later["next_at"], 1000.0 + OCR_RETRY_BACKOFF_MAX_SECS)

    def test_not_yet_due_frames_are_left_alone(self):
        folder = Path(tempfile.mkdtemp(prefix="clippy-ocr-wait-"))
        path = _jpeg(folder, "7000.jpg")
        schedule_ocr_retry(path, "memory: commit/swap >= 75%", now=time.time())
        self.assertEqual(pending_ocr_retries(folder, now=time.time(), limit=10), [])
        clear_ocr_retry(path)

    def test_delete_removes_retry_sidecar(self):
        folder = Path(tempfile.mkdtemp(prefix="clippy-ocr-del-"))
        path = _jpeg(folder, "8000.jpg")
        schedule_ocr_retry(path, "CPU: system usage >= 80%", now=time.time())
        self.assertTrue(ocr_retry_path(path).exists())
        delete_screenshot_files(path)
        self.assertFalse(path.exists())
        self.assertFalse(ocr_retry_path(path).exists())


if __name__ == "__main__":
    unittest.main()
