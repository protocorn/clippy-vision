"""Controls a first-run user can see: watch list, search, delete, correction."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from api_server import app
from core.app_settings import set_capture_settings, should_watch_process
from core.privacy_settings import get_privacy_enabled, list_privacy_targets, set_privacy_enabled
from core.storage import conn, list_timeline_sessions, store_event, store_summary
from core.events import Event, WindowMetadata
from core.user_controls import delete_event, set_session_correction


def _event(event_id: str, process_name: str, summary: str) -> Event:
    stamp = time.time()
    return Event(
        event_id=event_id,
        session_id="controls-session",
        timestamp=stamp,
        event_type="context_change",
        window_context=WindowMetadata(
            timestamp=stamp,
            current_window_title="Controls window",
            active_url=None,
            process_name=process_name,
        ),
        previous_window_context=None,
        payload={},
        summary=summary,
        vector_embedding=None,
        image_embedding=None,
        image_embedding_model=None,
        screenshot_filename=None,
        interest_score=None,
        interest_reason=None,
        interesting=True,
    )


class UserControlTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.prefix = f"controls-{uuid4().hex}"
        set_capture_settings({"watch_mode": "all", "watch_apps": []})

    def tearDown(self):
        self.client.close()
        set_capture_settings({"watch_mode": "all", "watch_apps": []})
        conn.execute("DELETE FROM events WHERE event_id LIKE ?", (f"{self.prefix}%",))
        conn.execute("DELETE FROM sessions WHERE summary_id LIKE ?", (f"{self.prefix}%",))
        conn.commit()

    def test_selected_apps_skip_everything_else(self):
        set_capture_settings({"watch_mode": "selected", "watch_apps": ["chrome.exe"]})
        self.assertTrue(should_watch_process("chrome.exe"))
        self.assertTrue(should_watch_process("Google Chrome"))
        self.assertFalse(should_watch_process("Microsoft Edge"))
        self.assertFalse(should_watch_process("Code.exe"))
        kept = f"{self.prefix}-kept"
        dropped = f"{self.prefix}-dropped"
        store_event(_event(kept, "chrome.exe", "kept summary"))
        store_event(_event(dropped, "Code.exe", "dropped summary"))
        rows = conn.execute(
            "SELECT event_id FROM events WHERE event_id IN (?, ?)",
            (kept, dropped),
        ).fetchall()
        self.assertEqual([row[0] for row in rows], [kept])

    def test_private_browsing_is_on_by_default(self):
        original = get_privacy_enabled()
        try:
            self.assertTrue(original["private_browsing"])
            listed = {item["id"]: item for item in list_privacy_targets()}
            self.assertIn("Google Chrome", listed["private_browsing"]["description"])
            self.assertIn("Microsoft Edge", listed["private_browsing"]["description"])
            self.assertIn("Brave", listed["private_browsing"]["description"])
            set_privacy_enabled({"private_browsing": False})
            self.assertFalse(get_privacy_enabled()["private_browsing"])
            from core.private_windows import window_is_private
            self.assertFalse(window_is_private(1, "chrome.exe"))
        finally:
            set_privacy_enabled(original)

    def test_search_matches_session_text(self):
        summary_id = f"{self.prefix}-search"
        phrase = f"orchid-ledger-{uuid4().hex[:8]}"
        now = time.time()
        store_summary({
            "session_id": summary_id,
            "summary_id": summary_id,
            "created_at": now,
            "window_start": now - 60,
            "window_end": now,
            "summary": f"Worked on {phrase}",
            "active_task": "notes",
            "event_count": 1,
        })
        found = list_timeline_sessions(q=phrase, limit=10)
        ids = [item["summary_id"] for item in found["sessions"]]
        self.assertIn(summary_id, ids)
        missing = list_timeline_sessions(q=f"no-such-{uuid4().hex}", limit=10)
        self.assertNotIn(summary_id, [item["summary_id"] for item in missing["sessions"]])

    def test_delete_and_correction_round_trip(self):
        summary_id = f"{self.prefix}-fix"
        event_id = f"{self.prefix}-moment"
        now = time.time()
        store_event(_event(event_id, "notepad.exe", "a moment"))
        store_summary({
            "session_id": summary_id,
            "summary_id": summary_id,
            "created_at": now,
            "window_start": now - 30,
            "window_end": now + 30,
            "summary": "Wrong story",
            "active_task": "edit",
            "event_count": 1,
        })
        saved = set_session_correction(summary_id, "I was reading, not editing")
        self.assertEqual(saved["user_correction"], "I was reading, not editing")
        detail = self.client.get(f"/sessions/{summary_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("reading", detail.json()["user_correction"])
        self.assertTrue(delete_event(event_id))
        self.assertFalse(delete_event(event_id))
        gone = self.client.delete(f"/sessions/{summary_id}")
        self.assertEqual(gone.status_code, 200)
        self.assertEqual(self.client.get(f"/sessions/{summary_id}").status_code, 404)
