"""Real endpoint tests for chat with FastAPI TestClient.

Tests the actual FastAPI endpoints with mocked gateway, no Ollama required.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create test client for the API."""
    # Import here to avoid startup issues
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    with patch("core.performance_metrics.start_performance_monitor"), \
         patch("core.intro_builder.start_intro_rebuild_daemon"), \
         patch("core.background_jobs.start_background_jobs"), \
         patch("core.app_settings.get_capture_settings", return_value={"rag_enabled": False}):
        from api_server import app
        return TestClient(app)


def test_chat_stream_full_flow(client):
    """Test /chat/stream emits status, content deltas, done, and persists both turns."""
    with patch("core.chat_context.build_chat_messages") as mock_build, \
         patch("core.llm_gateway.gateway.chat_stream") as mock_stream, \
         patch("agent.conversation.save_chat") as mock_save:
        
        # Mock message building
        mock_build.return_value = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"}
        ]
        
        # Mock streaming chunks
        mock_stream.return_value = [
            {"message": {"content": "Hi "}},
            {"message": {"content": "there"}},
            {"message": {"content": "!"}}
        ]
        
        response = client.post(
            "/chat/stream",
            json={"message": "Hello", "conversation_id": "test123"}
        )
        
        assert response.status_code == 200
        content = response.text
        
        # Check SSE structure
        assert "data: " in content
        
        # Parse SSE events
        events = []
        for line in content.split("\n"):
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
        
        # Should have: status messages, content deltas, done
        event_types = [e.get("type") for e in events]
        assert "status" in event_types
        assert "content" in event_types
        assert "done" in event_types
        
        # Check content was accumulated
        content_events = [e for e in events if e.get("type") == "content"]
        assert len(content_events) == 3
        assert content_events[0]["delta"] == "Hi "
        assert content_events[1]["delta"] == "there"
        assert content_events[2]["delta"] == "!"
        
        # Check done event has full result
        done_events = [e for e in events if e.get("type") == "done"]
        assert len(done_events) == 1
        assert done_events[0]["result"] == "Hi there!"
        assert done_events[0]["conversation_id"] == "test123"
        
        # Check both turns were saved
        assert mock_save.call_count == 2
        calls = mock_save.call_args_list
        assert calls[0][0] == ("test123", "user", "Hello")
        assert calls[1][0] == ("test123", "assistant", "Hi there!")


def test_chat_stream_ollama_unavailable(client):
    """Test /chat/stream returns error event with ollama_unavailable when Ollama is down."""
    from core.ollama_client import OllamaUnavailable
    
    with patch("core.chat_context.build_chat_messages") as mock_build, \
         patch("core.llm_gateway.gateway.chat_stream") as mock_stream:
        
        mock_build.return_value = [{"role": "user", "content": "Test"}]
        mock_stream.side_effect = OllamaUnavailable("not reachable")
        
        response = client.post(
            "/chat/stream",
            json={"message": "Test", "conversation_id": "test123"}
        )
        
        assert response.status_code == 200  # SSE always returns 200
        content = response.text
        
        # Parse events
        events = []
        for line in content.split("\n"):
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
        
        # Should have error event
        error_events = [e for e in events if e.get("type") == "error"]
        assert len(error_events) >= 1
        assert error_events[0].get("ollama_unavailable") is True
        assert "Ollama is not running" in error_events[0]["message"]


def test_chat_ollama_unavailable(client):
    """Test /chat returns 503 when Ollama is down."""
    from core.ollama_client import OllamaUnavailable
    
    with patch("core.chat_context.build_chat_messages") as mock_build, \
         patch("core.llm_gateway.gateway.chat") as mock_chat:
        
        mock_build.return_value = [{"role": "user", "content": "Test"}]
        mock_chat.side_effect = OllamaUnavailable("not reachable")
        
        response = client.post(
            "/chat",
            json={"message": "Test", "conversation_id": "test123"}
        )
        
        assert response.status_code == 503
        assert "Ollama is not running" in response.json()["detail"]


def test_chat_conversation_history_included(client):
    """Test that conversation history from prior turns is included in messages sent to gateway."""
    with patch("core.chat_context.build_chat_messages") as mock_build, \
         patch("core.llm_gateway.gateway.chat") as mock_chat, \
         patch("agent.conversation.save_chat") as mock_save:
        
        # Mock build_chat_messages to return history + new message
        mock_build.return_value = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Previous question"},
            {"role": "assistant", "content": "Previous answer"},
            {"role": "user", "content": "Follow up"}
        ]
        
        mock_chat.return_value = {"message": {"content": "Response"}}
        
        response = client.post(
            "/chat",
            json={"message": "Follow up", "conversation_id": "test123"}
        )
        
        assert response.status_code == 200
        
        # Verify build_chat_messages was called with conversation ID
        mock_build.assert_called_once()
        args = mock_build.call_args[0]
        assert args[0] == "Follow up"
        assert args[1] == "test123"
        
        # Verify gateway.chat received the history
        mock_chat.assert_called_once()
        messages = mock_chat.call_args[0][0]
        assert len(messages) == 4
        assert messages[1]["content"] == "Previous question"
        assert messages[2]["content"] == "Previous answer"
        assert messages[3]["content"] == "Follow up"


def test_chat_empty_message_rejected(client):
    """Test that empty messages are rejected with 400."""
    response = client.post(
        "/chat",
        json={"message": "", "conversation_id": "test"}
    )
    
    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


def test_chat_long_message_rejected(client):
    """Test that too-long messages are rejected with 400."""
    long_msg = "x" * 5000  # Over 4000 char limit
    
    response = client.post(
        "/chat",
        json={"message": long_msg, "conversation_id": "test"}
    )
    
    assert response.status_code == 400
    assert "long" in response.json()["detail"].lower()


def test_chat_generates_conversation_id_when_missing(client):
    """Test that a UUID conversation_id is generated when not provided."""
    with patch("core.chat_context.build_chat_messages") as mock_build, \
         patch("core.llm_gateway.gateway.chat") as mock_chat, \
         patch("agent.conversation.save_chat"):
        
        mock_build.return_value = [{"role": "user", "content": "Test"}]
        mock_chat.return_value = {"message": {"content": "Response"}}
        
        response = client.post(
            "/chat",
            json={"message": "Test", "conversation_id": ""}
        )
        
        assert response.status_code == 200
        result = response.json()
        
        # Should have a UUID-like conversation_id (36 chars with dashes)
        assert "conversation_id" in result
        conv_id = result["conversation_id"]
        assert len(conv_id) == 36
        assert conv_id.count("-") == 4


def test_chat_non_stream_saves_both_turns(client):
    """Test /chat (non-streaming) saves both user and assistant messages."""
    with patch("core.chat_context.build_chat_messages") as mock_build, \
         patch("core.llm_gateway.gateway.chat") as mock_chat, \
         patch("agent.conversation.save_chat") as mock_save:
        
        mock_build.return_value = [{"role": "user", "content": "Question"}]
        mock_chat.return_value = {"message": {"content": "Answer"}}
        
        response = client.post(
            "/chat",
            json={"message": "Question", "conversation_id": "test456"}
        )
        
        assert response.status_code == 200
        
        # Both turns should be saved
        assert mock_save.call_count == 2
        calls = mock_save.call_args_list
        assert calls[0][0] == ("test456", "user", "Question")
        assert calls[1][0] == ("test456", "assistant", "Answer")


def _sse_events(response_text: str) -> list[dict]:
    events = []
    for line in response_text.split("\n"):
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


def test_chat_model_missing(client):
    """Test /chat returns a clear 503 when the chat model is not installed."""
    from core.ollama_client import OllamaError

    with patch("core.chat_context.build_chat_messages") as mock_build, \
         patch("core.llm_gateway.gateway.chat") as mock_chat:

        mock_build.return_value = [{"role": "user", "content": "Test"}]
        mock_chat.side_effect = OllamaError(
            "ollama chat failed — HTTP 404: model 'qwen3:8b' not found"
        )

        response = client.post(
            "/chat",
            json={"message": "Test", "conversation_id": "test123"},
        )

        assert response.status_code == 503
        assert "not installed" in response.json()["detail"].lower()
        assert "HTTP 404" not in response.json()["detail"]


def test_chat_stream_model_missing(client):
    """Test /chat/stream emits a user-facing model-missing error event."""
    from core.ollama_client import OllamaError

    with patch("core.chat_context.build_chat_messages") as mock_build, \
         patch("core.llm_gateway.gateway.chat_stream") as mock_stream:

        mock_build.return_value = [{"role": "user", "content": "Test"}]
        mock_stream.side_effect = OllamaError(
            "ollama chat failed — HTTP 404: model 'qwen3:8b' not found"
        )

        response = client.post(
            "/chat/stream",
            json={"message": "Test", "conversation_id": "test123"},
        )

        assert response.status_code == 200
        error_events = [e for e in _sse_events(response.text) if e.get("type") == "error"]
        assert error_events
        assert error_events[0].get("model_missing") is True
        assert "not installed" in error_events[0]["message"].lower()
        assert error_events[0].get("ollama_unavailable") is not True
