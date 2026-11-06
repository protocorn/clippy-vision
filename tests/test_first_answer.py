"""First-day question on the session timeline: eligibility and wording."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.first_answer import (
    GENERIC_QUESTION,
    MIN_CAPTURE_SECONDS,
    build_suggested_question,
    first_answer_from_inputs,
    friendly_app_name,
    get_first_answer_prompt,
    shorten_phrase,
)
from core.storage import conn, store_summary

try:
    from fastapi.testclient import TestClient
    from api_server import app as _api_app
except ImportError:  # pragma: no cover - CI installs requirements.txt
    TestClient = None
    _api_app = None


class FirstAnswerQuestionTests(unittest.TestCase):
    def test_generic_question_when_nothing_specific(self):
        self.assertEqual(build_suggested_question(), GENERIC_QUESTION)
        self.assertEqual(build_suggested_question(active_task="  "), GENERIC_QUESTION)

    def test_uses_active_task_when_present(self):
        question = build_suggested_question(active_task="Fix login timeout")
        self.assertIn("Fix login timeout", question)
        self.assertTrue(question.startswith("What was I doing during"))

    def test_private_sessions_never_leak_task_text(self):
        question = build_suggested_question(
            active_task="Banking in Safari",
            process_name="Safari",
            private=True,
        )
        self.assertEqual(question, GENERIC_QUESTION)
        self.assertNotIn("Banking", question)

    def test_falls_back_to_friendly_app_name(self):
        self.assertEqual(
            build_suggested_question(process_name="chrome.exe"),
            "What was I working on in Google Chrome?",
        )
        self.assertEqual(friendly_app_name("cursor"), "Cursor")

    def test_long_task_is_truncated(self):
        long_task = "A" * 200
        question = build_suggested_question(active_task=long_task)
        self.assertLess(len(question), 120)
        self.assertIn("...", question)
        self.assertEqual(shorten_phrase("one   two", 80), "one two")


class FirstAnswerPromptTests(unittest.TestCase):
    def setUp(self):
        self.prefix = f"first-answer-{uuid4().hex}"
        self.client = TestClient(_api_app) if TestClient and _api_app else None

    def tearDown(self):
        if self.client is not None:
            self.client.close()
        conn.execute("DELETE FROM events WHERE event_id LIKE ?", (f"{self.prefix}%",))
        conn.execute("DELETE FROM sessions WHERE summary_id LIKE ?", (f"{self.prefix}%",))
        conn.commit()

    def _event(self, label: str, timestamp: float, process_name: str = "Cursor") -> None:
        conn.execute(
            """INSERT INTO events (
                    event_id, session_id, timestamp, event_type, process_name,
                    current_window_title, summary, payload, expires_at
               ) VALUES (?, ?, ?, 'context_change', ?, 'Window', 'summary', '{}', ?)""",
            (f"{self.prefix}-{label}", f"{self.prefix}-session", timestamp, process_name, timestamp + 86400),
        )
        conn.commit()

    def _session(self, label: str, start: float, end: float, task: str, private: int = 0) -> str:
        summary_id = f"{self.prefix}-{label}"
        store_summary(
            {
                "session_id": f"session-{summary_id}",
                "summary_id": summary_id,
                "created_at": end + 1,
                "window_start": start,
                "window_end": end,
                "summary": f"Summary for {label}",
                "active_task": task,
                "event_count": 3,
            }
        )
        conn.execute("UPDATE sessions SET private = ? WHERE summary_id = ?", (private, summary_id))
        conn.commit()
        return summary_id

    def test_not_eligible_with_empty_history(self):
        prompt = first_answer_from_inputs(None, 0.0)
        self.assertFalse(prompt["eligible"])
        self.assertEqual(prompt["question"], GENERIC_QUESTION)
        self.assertTrue(prompt["generic"])
        self.assertIsNone(prompt["source"])

    def test_span_just_under_thirty_minutes_is_not_enough_without_a_session(self):
        prompt = first_answer_from_inputs(None, MIN_CAPTURE_SECONDS - 1, "Code")
        self.assertFalse(prompt["eligible"])
        self.assertEqual(prompt["source"], None)

    def test_eligible_when_first_session_exists(self):
        start = time.time() + 1_000_000_000
        self._event("e1", start, "chrome.exe")
        self._session("s1", start, start + 200, "Read hotel listings")
        prompt = get_first_answer_prompt()
        self.assertTrue(prompt["eligible"])
        self.assertEqual(prompt["source"], "session")
        self.assertIn("Read hotel listings", prompt["question"])
        self.assertFalse(prompt["generic"])

    def test_eligible_after_roughly_thirty_minutes_without_a_summary(self):
        prompt = first_answer_from_inputs(None, MIN_CAPTURE_SECONDS, "Code")
        self.assertTrue(prompt["eligible"])
        self.assertEqual(prompt["source"], "capture_span")
        self.assertEqual(prompt["question"], "What was I working on in Visual Studio Code?")

    def test_live_span_helper_counts_inserted_events(self):
        start = 80_000_000
        self._event("span-a", start, "Code")
        self._event("span-b", start + MIN_CAPTURE_SECONDS, "Code")
        prompt = get_first_answer_prompt()
        self.assertGreaterEqual(prompt["capture_span_seconds"], MIN_CAPTURE_SECONDS)

    def test_private_session_stays_eligible_but_generic(self):
        start = time.time() + 1_000_000_000
        self._session("priv", start, start + 100, "Taxes in browser", private=1)
        prompt = get_first_answer_prompt()
        self.assertTrue(prompt["eligible"])
        self.assertEqual(prompt["question"], GENERIC_QUESTION)
        self.assertNotIn("Taxes", prompt["question"])

    def test_http_endpoint_matches_helper(self):
        if self.client is None:
            self.skipTest("FastAPI test client is not available")
        start = time.time() + 1_000_000_000
        self._session("http", start, start + 400, "Write the README")
        response = self.client.get("/timeline/first-answer")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["eligible"])
        self.assertIn("Write the README", body["question"])
        self.assertEqual(body["min_capture_seconds"], MIN_CAPTURE_SECONDS)
