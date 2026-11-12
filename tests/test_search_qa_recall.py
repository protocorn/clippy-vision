"""Regression tests from the v2.1.1 Linux recall run (Q16, Q18).

Q16: search_events treated "5:50 to 6:10 PM" as 05:50–06:10 AM.
Q18: search_sessions emitted invalid SQL for "around 6 PM" and did not fall back.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.helpers.time_resolver import resolve_temporal_range
from agent.retrieval import search_events, search_sessions
from core.events import Event, WindowMetadata
from core.storage import conn, store_event, store_summary

QA_NOW = datetime(2026, 10, 18, 18, 59, 0)
Q16 = "What was I doing between 5:50 and 6:10 PM today?"
Q18 = "Which technical documentation or release notes did I read around 6 PM?"


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return QA_NOW


def _ts(hour: int, minute: int) -> float:
    return datetime(2026, 10, 18, hour, minute).timestamp()


def _make_event(event_id: str, timestamp: float, summary: str) -> Event:
    return Event(
        event_id=event_id,
        session_id="qa-recall-session",
        timestamp=timestamp,
        event_type="screenshot_analysis",
        window_context=WindowMetadata(
            timestamp=timestamp,
            current_window_title=summary,
            active_url=None,
            process_name="chrome",
        ),
        previous_window_context=None,
        payload={},
        summary=summary,
        vector_embedding=None,
        image_embedding=None,
        image_embedding_model=None,
        screenshot_filename=None,
        interest_score=8,
        interest_reason="qa",
        interesting=True,
    )


class ClockRangeResolverTests(unittest.TestCase):
    def test_q16_pm_applies_across_the_range(self):
        result = resolve_temporal_range(Q16, now=QA_NOW)
        self.assertIsNotNone(result)
        self.assertEqual(result.start_ts, _ts(17, 50))
        self.assertEqual(result.end_ts, _ts(18, 10))

    def test_trailing_pm_applies_to_start(self):
        result = resolve_temporal_range("5:50 to 6:10 PM", now=QA_NOW)
        self.assertIsNotNone(result)
        self.assertEqual(result.start_ts, _ts(17, 50))
        self.assertEqual(result.end_ts, _ts(18, 10))

    def test_missing_ampm_uses_evening_when_now_is_evening(self):
        result = resolve_temporal_range("between 5:50 and 6:10", now=QA_NOW)
        self.assertIsNotNone(result)
        self.assertEqual(result.start_ts, _ts(17, 50))
        self.assertEqual(result.end_ts, _ts(18, 10))

    def test_missing_ampm_uses_morning_when_now_is_morning(self):
        morning = datetime(2026, 10, 18, 10, 0, 0)
        result = resolve_temporal_range("between 5:50 and 6:10", now=morning)
        self.assertIsNotNone(result)
        self.assertEqual(result.start_ts, _ts(5, 50))
        self.assertEqual(result.end_ts, _ts(6, 10))

    def test_q18_around_6_pm_covers_6_pm(self):
        result = resolve_temporal_range(Q18, now=QA_NOW)
        self.assertIsNotNone(result)
        six = _ts(18, 0)
        self.assertLessEqual(result.start_ts, six)
        self.assertGreater(result.end_ts, six)
        self.assertGreaterEqual(six - result.start_ts, 15 * 60)
        self.assertLessEqual(result.end_ts - result.start_ts, 2 * 3600)


class SearchQaRecallTests(unittest.TestCase):
    def setUp(self):
        self.prefix = f"qa-recall-{uuid4().hex}"

    def tearDown(self):
        conn.execute("DELETE FROM events WHERE event_id LIKE ?", (f"{self.prefix}%",))
        conn.execute("DELETE FROM sessions WHERE summary_id LIKE ?", (f"{self.prefix}%",))
        conn.commit()

    def _store_event(self, suffix: str, timestamp: float, summary: str, ocr: str = "") -> None:
        event_id = f"{self.prefix}-{suffix}"
        store_event(_make_event(event_id, timestamp, summary))
        if ocr:
            conn.execute(
                "UPDATE events SET vision_ocr_text=? WHERE event_id=?",
                (ocr, event_id),
            )
            conn.commit()

    def _store_session(self, suffix: str, timestamp: float, summary: str) -> None:
        store_summary(
            {
                "session_id": f"{self.prefix}-{suffix}-session",
                "summary_id": f"{self.prefix}-{suffix}",
                "created_at": timestamp,
                "window_start": timestamp,
                "window_end": timestamp + 180,
                "summary": summary,
                "active_task": "reading docs",
                "entities": ["MDN", "Node.js", "AppImage"],
                "event_count": 3,
            },
            vision_enriched=True,
        )

    def test_q16_search_events_finds_evening_not_morning(self):
        evening_marker = "EVENING_RASPBERRY_PI_ADAFRUIT"
        morning_marker = "MORNING_ONLY_PI_PRICE"
        self._store_event("evening", _ts(17, 56), "Adafruit Raspberry Pi 5", evening_marker)
        self._store_event("morning", _ts(5, 56), "Morning leftover page", morning_marker)
        am_sql = (
            "SELECT summary, datetime(timestamp,'unixepoch','localtime') as time "
            "FROM events WHERE timestamp >= CAST(strftime('%s', date("
            f"{int(QA_NOW.timestamp())},'unixepoch','localtime','start of day')) "
            "AS INTEGER) + 21000 AND timestamp < CAST(strftime('%s', date("
            f"{int(QA_NOW.timestamp())},'unixepoch','localtime','start of day')) "
            "AS INTEGER) + 22200 LIMIT 20"
        )
        with patch("agent.helpers.time_resolver.datetime", _FrozenDateTime), patch(
            "agent.retrieval.search_event_rag", return_value=None
        ), patch("agent.retrieval._generate_sql", return_value=am_sql):
            result = search_events(Q16)

        self.assertIn(evening_marker, result)
        self.assertNotIn(morning_marker, result)
        self.assertNotIn("SQL error", result)

    def test_q16_sql_prompt_gets_resolved_evening_timestamps(self):
        captured: list[str] = []

        def fake_generate(_prompt: str, user_content: str) -> str:
            captured.append(user_content)
            return "SELECT summary FROM events LIMIT 1"

        with patch("agent.helpers.time_resolver.datetime", _FrozenDateTime), patch(
            "agent.retrieval.search_event_rag", return_value=None
        ), patch("agent.retrieval._fts_event_search", return_value=None), patch(
            "agent.retrieval._direct_event_time_search", return_value=None
        ), patch("agent.retrieval._generate_sql", side_effect=fake_generate), patch(
            "agent.retrieval._run_sql", return_value=(["summary: prompt-only"], 1)
        ):
            search_events(Q16)

        self.assertTrue(captured)
        hint = captured[0]
        self.assertIn(str(int(_ts(17, 50))), hint)
        self.assertIn(str(int(_ts(18, 10))), hint)
        self.assertIn("Resolved time window", hint)

    def test_q18_invalid_around_6pm_sql_falls_back_to_keywords(self):
        marker = "NODEJS_22110_RELEASE_NOTES_MDN_FLATMAP"
        self._store_session(
            "docs",
            _ts(18, 2),
            "Read MDN Array.prototype.flatMap documentation, the AppImageKit "
            f"wiki FUSE page, and Node.js 22.11.0 release notes. {marker}",
        )
        invalid_sql = (
            "SELECT summary, active_task, entities, "
            "datetime(window_start,'unixepoch','localtime') as time "
            "FROM sessions WHERE )"
        )
        with patch("agent.helpers.time_resolver.datetime", _FrozenDateTime), patch(
            "agent.retrieval._generate_sql", return_value=invalid_sql
        ):
            result = search_sessions(Q18)

        self.assertNotIn("SQL error", result)
        self.assertNotIn("syntax error", result)
        self.assertIn(marker, result)

    def test_q18_empty_structured_result_falls_back_to_keywords(self):
        marker = "APPIMAGEKIT_WIKI_FUSE_DOCS"
        self._store_session(
            "empty-sql",
            _ts(18, 3),
            f"Technical documentation and release notes including {marker}.",
        )
        empty_sql = (
            "SELECT summary, active_task, entities "
            "FROM sessions WHERE window_start > 9999999999 "
            "AND summary IS NOT NULL AND summary != ''"
        )
        with patch("agent.helpers.time_resolver.datetime", _FrozenDateTime), patch(
            "agent.retrieval._generate_sql", return_value=empty_sql
        ), patch("agent.retrieval._semantic_sessions", return_value=([], 0)):
            result = search_sessions(Q18)

        self.assertNotIn("no matching session summaries", result)
        self.assertIn(marker, result)


if __name__ == "__main__":
    unittest.main()
