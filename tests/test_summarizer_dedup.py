"""Regression tests for summarizer deduplication."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.storage import conn, get_unsummarized_events, store_summary
from core.summarizer import _group_events_by_time_window


class SummarizerDedupTests(unittest.TestCase):
    def setUp(self):
        self.prefix = f"summarizer-dedup-{uuid4().hex}"
        self.capture_sid = f"{self.prefix}-capture"
        self.api_sid = f"{self.prefix}-api"

    def tearDown(self):
        conn.execute("DELETE FROM events WHERE event_id LIKE ?", (f"{self.prefix}%",))
        conn.execute("DELETE FROM sessions WHERE summary_id LIKE ?", (f"{self.prefix}%",))
        conn.commit()

    def _insert_event(self, label: str, session_id: str, timestamp: float) -> None:
        conn.execute(
            """INSERT INTO events (
                event_id, session_id, timestamp, event_type, summary, expires_at
            ) VALUES (?, ?, ?, 'context_change', ?, ?)""",
            (f"{self.prefix}-{label}", session_id, timestamp, f"Event {label}", timestamp + 86400),
        )
        conn.commit()

    def test_get_unsummarized_events_ignores_session_id_mismatch(self):
        base = 1_000_000.0
        for i in range(3):
            self._insert_event(f"evt-{i}", self.capture_sid, base + i)

        store_summary(
            {
                "session_id": self.api_sid,
                "summary_id": f"{self.prefix}-summary",
                "created_at": base + 10,
                "window_start": base,
                "window_end": base + 2,
                "summary": "Already summarized",
                "active_task": "testing",
                "event_count": 3,
            },
            vision_enriched=True,
        )

        unsummarized = get_unsummarized_events(base - 1)
        self.assertEqual(
            [event["event_id"] for event in unsummarized if event["event_id"].startswith(self.prefix)],
            [],
        )

    def test_store_summary_skips_duplicate_window(self):
        base = 2_000_000.0
        first = {
            "session_id": self.capture_sid,
            "summary_id": f"{self.prefix}-first",
            "created_at": base,
            "window_start": base,
            "window_end": base + 60,
            "summary": "First",
            "active_task": "first task",
            "event_count": 5,
        }
        second = {
            **first,
            "summary_id": f"{self.prefix}-second",
            "summary": "Second",
            "active_task": "second task",
        }
        store_summary(first, vision_enriched=False)
        store_summary(second, vision_enriched=False)

        rows = conn.execute(
            "SELECT summary_id FROM sessions WHERE summary_id LIKE ?",
            (f"{self.prefix}%",),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], first["summary_id"])

    def test_group_events_by_time_window_merges_process_session_ids(self):
        base = 3_000_000.0
        events = [
            {"session_id": self.capture_sid, "timestamp": base},
            {"session_id": self.api_sid, "timestamp": base + 30},
            {"session_id": self.capture_sid, "timestamp": base + 60},
            {"session_id": self.capture_sid, "timestamp": base + 3600},
        ]
        groups = _group_events_by_time_window(events, gap_seconds=600)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[0]), 3)
        self.assertEqual(len(groups[1]), 1)

    def test_group_events_by_time_window_respects_max_duration(self):
        base = 4_000_000.0
        events = [
            {"session_id": self.capture_sid, "timestamp": base + offset}
            for offset in (0, 300, 600, 900, 1200, 1500, 1801)
        ]
        groups = _group_events_by_time_window(
            events,
            gap_seconds=600,
            max_duration_secs=1800,
        )
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[0]), 6)
        self.assertEqual(len(groups[1]), 1)

    def test_delete_oversized_sessions_removes_mega_windows(self):
        from core.storage import delete_oversized_sessions

        base = 5_000_000.0
        store_summary(
            {
                "session_id": self.capture_sid,
                "summary_id": f"{self.prefix}-mega",
                "created_at": base,
                "window_start": base,
                "window_end": base + (34 * 3600),
                "summary": "Mega",
                "active_task": "debugging code",
                "event_count": 11,
            },
            vision_enriched=True,
        )
        removed = delete_oversized_sessions(30 * 60)
        self.assertEqual(removed, 1)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE summary_id = ?",
            (f"{self.prefix}-mega",),
        ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_list_session_events_filters_to_summarizer_types(self):
        from core.storage import list_session_events

        base = 6_000_000.0
        summary_id = f"{self.prefix}-detail"
        store_summary(
            {
                "session_id": self.capture_sid,
                "summary_id": summary_id,
                "created_at": base,
                "window_start": base,
                "window_end": base + 60,
                "summary": "Detail filter",
                "active_task": "testing",
                "event_count": 2,
            },
            vision_enriched=True,
        )
        for label, event_type, summary in (
            ("keep", "context_change", "Switched apps"),
            ("drop-type", "mouse_burst", "Mouse burst"),
            ("drop-empty", "typing_burst", ""),
        ):
            conn.execute(
                """INSERT INTO events (
                    event_id, session_id, timestamp, event_type, summary, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    f"{self.prefix}-{label}",
                    self.capture_sid,
                    base + 10,
                    event_type,
                    summary,
                    base + 86400,
                ),
            )
        conn.commit()

        detail = list_session_events(summary_id)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["event_count"], 2)
        self.assertEqual([event["event_id"] for event in detail["events"]], [f"{self.prefix}-keep"])


if __name__ == "__main__":
    unittest.main()
