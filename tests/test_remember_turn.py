"""remember_turn stores the user's words and leaves the model reply out."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-remember-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.memory import remember_turn


class RememberTurnTests(unittest.TestCase):
    def test_empty_message_is_not_stored(self):
        with patch("agent.memory.ingest_conversation") as ingest:
            self.assertEqual(remember_turn("  "), "No message to remember.")
        ingest.assert_not_called()

    def test_a_secret_message_is_refused(self):
        with patch("agent.memory.ingest_conversation") as ingest:
            result = remember_turn("ghp_" + "a" * 36)
        ingest.assert_not_called()
        self.assertIn("secret", result.casefold())

    def test_user_words_are_stored_and_the_reply_is_not_an_argument(self):
        with patch(
            "agent.memory.ingest_conversation",
            return_value={"facts": ["Lives in Paris"], "profile": ["location"]},
        ) as ingest:
            result = remember_turn("I live in Paris")
        ingest.assert_called_once_with("I live in Paris", "")
        self.assertIn("Lives in Paris", result)
        self.assertIn("location", result)

    def test_a_turn_with_no_personal_fact_is_dropped(self):
        with patch(
            "agent.memory.ingest_conversation",
            return_value={"facts": [], "profile": []},
        ):
            self.assertEqual(
                remember_turn("What is 2 plus 2?"),
                "No personal facts in that message.",
            )
