"""Retry and re-queue behavior when the summarizer skips a real page."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.summarizer import (
    RETRY_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    SkipWindow,
    summarize_window,
)


def _reading_event() -> dict:
    return {
        "timestamp": 1_700_000_000.0,
        "session_id": "sid-reading",
        "event_type": "screenshot_analysis",
        "window_title": "FUSE - AppImageKit wiki - Google Chrome",
        "summary": "Background screenshot of chrome",
        "vision_ocr_text": (
            "AppImages require FUSE to run.\n"
            "On Ubuntu 24.04 install the libfuse2t64 package.\n"
            "See https://github.com/AppImage/AppImageKit/wiki/FUSE"
        ),
    }


def _model_payload(*, skip: bool, summary: str = "") -> dict:
    return {
        "message": {
            "content": json.dumps({
                "skip": skip,
                "summary": summary,
                "active_task": "" if skip else "Reading AppImage FUSE docs",
                "entities": [] if skip else ["libfuse2t64"],
            })
        }
    }


class SummarizerSkipRetryTests(unittest.TestCase):
    @patch("core.summarizer.has_recent_content_hash", return_value=False)
    @patch("core.summarizer.embed_text", return_value=None)
    @patch("core.summarizer.gateway.chat")
    def test_reading_page_is_retried_after_model_skip(self, chat, _embed, _dup):
        chat.side_effect = [
            _model_payload(skip=True),
            _model_payload(skip=False, summary="The user read the AppImageKit FUSE wiki."),
        ]

        result = summarize_window([_reading_event()], "sid-reading")

        self.assertNotIsInstance(result, SkipWindow)
        self.assertEqual(result["summary"], "The user read the AppImageKit FUSE wiki.")
        self.assertEqual(chat.call_count, 2)
        first_system = chat.call_args_list[0].kwargs["messages"][0]["content"]
        second_system = chat.call_args_list[1].kwargs["messages"][0]["content"]
        self.assertEqual(first_system, SYSTEM_PROMPT)
        self.assertEqual(second_system, RETRY_SYSTEM_PROMPT)
        self.assertIn("readable page or document text", second_system)

    @patch("core.summarizer.has_recent_content_hash", return_value=False)
    @patch("core.summarizer.gateway.chat")
    def test_second_model_skip_still_returns_skip_window(self, chat, _dup):
        chat.return_value = _model_payload(skip=True)

        result = summarize_window([_reading_event()], "sid-reading")

        self.assertIsInstance(result, SkipWindow)
        self.assertEqual(result.reason, "model skipped")
        self.assertEqual(chat.call_count, 2)

    @patch("core.summarizer.has_recent_content_hash", return_value=False)
    @patch("core.summarizer.embed_text", return_value=None)
    @patch("core.summarizer.gateway.chat")
    def test_accepted_reading_page_is_not_retried(self, chat, _embed, _dup):
        chat.return_value = _model_payload(
            skip=False,
            summary="The user read the AppImageKit FUSE wiki.",
        )

        result = summarize_window([_reading_event()], "sid-reading")

        self.assertNotIsInstance(result, SkipWindow)
        self.assertEqual(chat.call_count, 1)


if __name__ == "__main__":
    unittest.main()
