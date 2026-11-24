"""Every Ollama generate/chat call must send an explicit num_ctx."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-num-ctx-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.retrieval import _generate_sql, search_events, search_sessions
from classifier.tier_two_classifier import classify_with_llm
from core.llm_gateway import LLMGateway, Priority, gateway
from core.model_residency import _warm
from core.ollama_client import post_json
from core.ollama_options import (
    DEFAULT_NUM_CTX,
    apply_inference_options,
    get_num_ctx,
    merge_options,
    warn_if_prompt_exceeds,
)
from core.storage import conn, store_summary
from core.summarizer import summarize_window


def _assert_num_ctx(payload: dict, expected: int = DEFAULT_NUM_CTX) -> None:
    options = payload.get("options") or {}
    assert options.get("num_ctx") == expected, payload


class FakeOllamaResponse:
    def __init__(self, body: dict | None = None, stream_lines: list[dict] | None = None):
        self._body = body if body is not None else {"message": {"content": "{}"}}
        encoded = [json.dumps(line).encode() + b"\n" for line in (stream_lines or [self._body])]
        self._lines = encoded + [b""]
        self._index = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._body).encode()

    def readline(self):
        if self._index >= len(self._lines):
            return b""
        line = self._lines[self._index]
        self._index += 1
        return line

    def close(self):
        return None


@contextmanager
def _capture_gateway_payloads(response_body: dict | None = None):
    captured: list[dict] = []

    def fake_open(*args):
        job = args[-1]
        captured.append(job.payload)
        return FakeOllamaResponse(response_body)

    with patch.object(LLMGateway, "_open", fake_open), patch(
        "core.llm_gateway.can_load_text", return_value=True
    ), patch.object(gateway, "_health_gate", lambda *args, **kwargs: None), patch(
        "core.llm_gateway._BG_INTER_JOB_SLEEP", 0
    ):
        yield captured


@contextmanager
def _capture_client_payloads():
    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        captured.append({
            "url": getattr(req, "full_url", ""),
            "body": json.loads(req.data.decode()) if req.data else None,
        })
        return FakeOllamaResponse({})

    with patch("core.ollama_client.urllib.request.urlopen", fake_urlopen):
        yield captured


class OllamaOptionsTests(unittest.TestCase):
    def test_default_num_ctx_is_8192(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLIPPY_OLLAMA_NUM_CTX", None)
            self.assertEqual(get_num_ctx(), 8192)
            self.assertEqual(DEFAULT_NUM_CTX, 8192)

    def test_env_override_and_invalid_values(self):
        with patch.dict(os.environ, {"CLIPPY_OLLAMA_NUM_CTX": "16384"}):
            self.assertEqual(get_num_ctx(), 16384)
        with patch.dict(os.environ, {"CLIPPY_OLLAMA_NUM_CTX": "nope"}):
            self.assertEqual(get_num_ctx(), DEFAULT_NUM_CTX)
        with patch.dict(os.environ, {"CLIPPY_OLLAMA_NUM_CTX": "0"}):
            self.assertEqual(get_num_ctx(), DEFAULT_NUM_CTX)

    def test_merge_options_keeps_caller_keys(self):
        merged = merge_options({"temperature": 0, "num_ctx": 2048})
        self.assertEqual(merged["temperature"], 0)
        self.assertEqual(merged["num_ctx"], DEFAULT_NUM_CTX)

    def test_warns_when_prompt_likely_exceeds_context(self):
        body = apply_inference_options({
            "model": "x",
            "prompt": "word " * 50_000,
        })
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            warn_if_prompt_exceeds(body, num_ctx=128)
        self.assertIn("num_ctx=128", buf.getvalue())
        self.assertIn("truncate", buf.getvalue())


class GatewayAndGenerateTests(unittest.TestCase):
    def test_gateway_chat_sends_num_ctx(self):
        with _capture_gateway_payloads() as captured:
            gateway.chat(
                [{"role": "user", "content": "hi"}],
                "test-model",
                options={"temperature": 0},
                priority=Priority.INTERACTIVE,
            )
        self.assertEqual(len(captured), 1)
        _assert_num_ctx(captured[0])
        self.assertEqual(captured[0]["options"]["temperature"], 0)
        self.assertFalse(captured[0]["stream"])

    def test_env_override_reaches_chat_and_generate_payloads(self):
        with patch.dict(os.environ, {"CLIPPY_OLLAMA_NUM_CTX": "4096"}):
            with _capture_gateway_payloads() as captured:
                gateway.chat(
                    [{"role": "user", "content": "hi"}],
                    "test-model",
                    priority=Priority.INTERACTIVE,
                )
            with _capture_client_payloads() as client_captured:
                post_json("/api/generate", {"model": "test-model", "prompt": "ping", "stream": False})
        _assert_num_ctx(captured[0], 4096)
        _assert_num_ctx(client_captured[0]["body"], 4096)

    def test_gateway_chat_stream_sends_num_ctx(self):
        with _capture_gateway_payloads({"message": {"content": "ok"}}) as captured:
            list(gateway.chat_stream(
                [{"role": "user", "content": "hi"}],
                "test-model",
                options={"temperature": 0},
                priority=Priority.INTERACTIVE,
            ))
        self.assertEqual(len(captured), 1)
        _assert_num_ctx(captured[0])
        self.assertTrue(captured[0]["stream"])

    def test_post_json_generate_and_chat_send_num_ctx(self):
        with _capture_client_payloads() as captured:
            post_json("/api/generate", {"model": "test-model", "prompt": "ping", "stream": False})
            post_json("/api/chat", {
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "options": {"temperature": 0},
            })
        self.assertEqual(len(captured), 2)
        self.assertIn("/api/generate", captured[0]["url"])
        self.assertIn("/api/chat", captured[1]["url"])
        _assert_num_ctx(captured[0]["body"])
        _assert_num_ctx(captured[1]["body"])
        self.assertEqual(captured[1]["body"]["options"]["temperature"], 0)

    def test_model_residency_warmup_sends_num_ctx(self):
        with _capture_client_payloads() as captured:
            _warm("test-model")
        self.assertEqual(len(captured), 1)
        self.assertIn("/api/generate", captured[0]["url"])
        _assert_num_ctx(captured[0]["body"])
        self.assertEqual(captured[0]["body"]["prompt"], "ping")

    def test_embeddings_do_not_call_ollama(self):
        with _capture_client_payloads() as captured:
            vector = gateway.embed("provider-independent embedding")
        self.assertEqual(captured, [])
        self.assertTrue(vector)


class CallPathTests(unittest.TestCase):
    def setUp(self):
        self.prefix = f"num-ctx-{uuid4().hex}"

    def tearDown(self):
        conn.execute("DELETE FROM events WHERE event_id LIKE ?", (f"{self.prefix}%",))
        conn.execute("DELETE FROM sessions WHERE summary_id LIKE ?", (f"{self.prefix}%",))
        conn.commit()

    def test_summarizer_sends_num_ctx(self):
        events = [{
            "event_type": "screenshot_analysis",
            "summary": "Editing retrieval SQL generation inside Cursor",
            "vision_ocr_text": (
                "def generate_sql(question: str) -> str:\n"
                "    prompt = build_long_search_prompt(question)\n"
                "    return gateway.chat(messages=prompt)\n"
            ),
            "timestamp": time.time(),
            "window_title": "retrieval.py - Cursor",
        }]
        summary_json = {
            "skip": False,
            "summary": "Edited retrieval SQL generation in Cursor.",
            "active_task": "SQL generation",
            "entities": ["Cursor"],
        }
        with patch("core.summarizer.has_recent_content_hash", return_value=False):
            with _capture_gateway_payloads({"message": {"content": json.dumps(summary_json)}}) as captured:
                result = summarize_window(events, f"{self.prefix}-session")
        self.assertEqual(len(captured), 1)
        _assert_num_ctx(captured[0])
        self.assertEqual(captured[0]["options"]["temperature"], 0)
        self.assertIsInstance(result, dict)
        self.assertIn("Edited retrieval", result["summary"])

    def test_question_to_sql_sends_num_ctx(self):
        sql_json = {"sql_query": "SELECT summary FROM sessions LIMIT 1"}
        with _capture_gateway_payloads({"message": {"content": json.dumps(sql_json)}}) as captured:
            sql = _generate_sql("You generate SQLite SELECT queries.", "Question: what did I do today?")
        self.assertEqual(len(captured), 1)
        _assert_num_ctx(captured[0])
        self.assertEqual(sql, "SELECT summary FROM sessions LIMIT 1")

    def test_mcp_search_sessions_sends_num_ctx(self):
        stamp = time.time()
        store_summary(
            {
                "session_id": f"{self.prefix}-session",
                "summary_id": f"{self.prefix}-summary",
                "created_at": stamp,
                "window_start": stamp,
                "window_end": stamp + 30,
                "summary": "Wrote Ollama num_ctx handling for long search prompts.",
                "active_task": "num_ctx",
                "entities": ["Ollama"],
                "event_count": 2,
            },
            vision_enriched=True,
        )
        sql_json = {
            "sql_query": (
                "SELECT summary, active_task, entities, window_start "
                f"FROM sessions WHERE summary_id = '{self.prefix}-summary'"
            )
        }
        with _capture_gateway_payloads({"message": {"content": json.dumps(sql_json)}}) as captured:
            result = search_sessions("what did I work on with Ollama?")
        self.assertEqual(len(captured), 1)
        _assert_num_ctx(captured[0])
        self.assertIn("Ollama num_ctx handling", result)

    def test_mcp_search_events_sends_num_ctx(self):
        stamp = time.time()
        conn.execute(
            """INSERT INTO events (
                event_id, session_id, timestamp, event_type, summary, expires_at
            ) VALUES (?, ?, ?, 'paste', ?, ?)""",
            (f"{self.prefix}-evt", f"{self.prefix}-session", stamp, "Pasted Ollama num_ctx notes", stamp + 86400),
        )
        conn.commit()
        sql_json = {
            "sql_query": (
                "SELECT summary, event_type, timestamp FROM events "
                f"WHERE event_id = '{self.prefix}-evt'"
            )
        }
        with patch("agent.retrieval.search_event_rag", return_value=None), patch(
            "agent.retrieval._fts_event_search", return_value=None
        ):
            with _capture_gateway_payloads({"message": {"content": json.dumps(sql_json)}}) as captured:
                result = search_events("find the pasted Ollama notes")
        self.assertEqual(len(captured), 1)
        _assert_num_ctx(captured[0])
        self.assertIn("Pasted Ollama num_ctx notes", result)

    def test_classifier_sends_num_ctx(self):
        verdict = {"verdict": "interesting", "score": 8, "reason": "new tool"}
        with _capture_gateway_payloads({"message": {"content": json.dumps(verdict)}}) as captured:
            result = classify_with_llm(
                "Configured Ollama context size",
                "context_change",
                {"process_name": "cursor.exe", "current_window_title": "ollama_options.py"},
                priority=Priority.INTERACTIVE,
            )
        self.assertEqual(len(captured), 1)
        _assert_num_ctx(captured[0])
        self.assertEqual(result["verdict"], "interesting")


if __name__ == "__main__":
    unittest.main()
