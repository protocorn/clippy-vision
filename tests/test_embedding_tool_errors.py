from __future__ import annotations

import unittest
from unittest.mock import patch

from agent.prefetch.memory_query import memory_query
from agent.tool_result import (
    ToolErrorStatus,
    ToolResult,
    finalize_tool_text,
    keyword_fallback_result,
)
from core.local_embeddings import EmbeddingUnavailable


class MissingModelResponseTests(unittest.TestCase):
    def test_finalize_tool_text_marks_error_and_keeps_fallback(self):
        with self.assertRaises(ToolErrorStatus) as ctx:
            finalize_tool_text(
                ToolResult(text="keyword hits here", error="MiniLM missing"),
                wrap=lambda text: f"WRAP:{text}",
            )
        self.assertIn("keyword hits here", str(ctx.exception))
        self.assertIn("WRAP:", str(ctx.exception))

    def test_finalize_tool_text_success_stays_plain(self):
        text = finalize_tool_text("all good")
        self.assertEqual(text, "all good")

    def test_keyword_fallback_result_is_error_with_hits(self):
        result = keyword_fallback_result(
            "time: 2026-01-01\nsummary: used vim as favorite editor",
            EmbeddingUnavailable("MiniLM model is missing"),
            notice="search_sessions: MiniLM model is missing or incomplete; results are keyword fallback only.",
        )
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.error)
        self.assertIn("keyword fallback", result.text)
        self.assertIn("favorite editor", result.text)
        self.assertIn("MiniLM model is missing", result.text)
        with self.assertRaises(ToolErrorStatus) as ctx:
            finalize_tool_text(result)
        self.assertIn("favorite editor", str(ctx.exception))

    def test_keyword_fallback_result_success_is_plain_text(self):
        result = keyword_fallback_result("plain hits", None)
        self.assertEqual(result, "plain hits")

    def test_memory_query_returns_error_status(self):
        with patch(
            "agent.prefetch.memory_query.embed_text",
            side_effect=EmbeddingUnavailable("MiniLM model is missing"),
        ):
            result = memory_query("favorite editor")
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.error)
        self.assertIn("embedding failed", result.text)


if __name__ == "__main__":
    unittest.main()
