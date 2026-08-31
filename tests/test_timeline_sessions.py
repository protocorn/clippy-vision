"""Regression tests for the timeline session listing API."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from api_server import app
from core.storage import conn, store_summary


class TimelineSessionsTests(unittest.TestCase):
    """Verify the paginated session-listing contract exposed to the timeline."""

    def setUp(self):
        self.client = TestClient(app)
        self.summary_id_prefix = f"timeline-sessions-{uuid4().hex}"

    def tearDown(self):
        self.client.close()
        conn.execute(
            "DELETE FROM sessions WHERE summary_id LIKE ?",
            (f"{self.summary_id_prefix}%",),
        )
        conn.commit()

    def _store_session(
        self,
        label: str,
        window_start: float,
        window_end: float,
        created_at: float,
    ) -> str:
        summary_id = f"{self.summary_id_prefix}-{label}"
        store_summary(
            {
                "session_id": f"session-{summary_id}",
                "summary_id": summary_id,
                "created_at": created_at,
                "window_start": window_start,
                "window_end": window_end,
                "summary": f"Summary for {label}",
                "active_task": f"Task for {label}",
                "event_count": 3,
            },
            embedding=[0.25, 0.75],
        )
        return summary_id

    def test_endpoint_returns_newest_sessions_with_pagination_without_embeddings(self):
        oldest_id = self._store_session("oldest", 10, 20, 21)
        middle_id = self._store_session("middle", 30, 40, 41)
        newest_id = self._store_session("newest", 50, 60, 61)

        first_response = self.client.get(
            "/timeline/sessions",
            params={"limit": 2, "offset": 0},
        )
        second_response = self.client.get(
            "/timeline/sessions",
            params={"limit": 2, "offset": 2},
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        first_page = first_response.json()
        second_page = second_response.json()
        self.assertEqual(first_page["limit"], 2)
        self.assertEqual(first_page["offset"], 0)
        self.assertEqual(
            [session["summary_id"] for session in first_page["sessions"]],
            [newest_id, middle_id],
        )
        self.assertEqual(
            [session["summary_id"] for session in second_page["sessions"]],
            [oldest_id],
        )
        for session in first_page["sessions"]:
            self.assertNotIn("summary_embedding", session)
            self.assertEqual(session["event_count"], 3)

    def test_endpoint_filters_sessions_by_overlapping_time_window(self):
        before_id = self._store_session("before", 10, 20, 21)
        overlapping_id = self._store_session("overlapping", 90, 110, 111)
        after_id = self._store_session("after", 200, 220, 221)

        http_response = self.client.get(
            "/timeline/sessions",
            params={"since": 100, "until": 200, "limit": 40, "offset": 0},
        )

        self.assertEqual(http_response.status_code, 200)
        response = http_response.json()
        self.assertEqual(
            [session["summary_id"] for session in response["sessions"]],
            [overlapping_id],
        )
        self.assertNotIn(before_id, [session["summary_id"] for session in response["sessions"]])
        self.assertNotIn(after_id, [session["summary_id"] for session in response["sessions"]])

    def test_endpoint_returns_an_empty_page_when_no_sessions_match(self):
        http_response = self.client.get(
            "/timeline/sessions",
            params={"since": 0, "until": 1, "limit": 40, "offset": 0},
        )

        self.assertEqual(http_response.status_code, 200)
        self.assertEqual(
            http_response.json(),
            {"sessions": [], "limit": 40, "offset": 0},
        )

    def test_endpoint_rejects_invalid_pagination(self):
        response = self.client.get("/timeline/sessions", params={"limit": 0})

        self.assertEqual(response.status_code, 422)
