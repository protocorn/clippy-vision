from core.chat_model import DEFAULT_CHAT_MODEL, get_chat_model


def test_default_chat_model_is_small_llama():
    assert DEFAULT_CHAT_MODEL == "llama3.2:3b"


def test_get_chat_model_prefers_env(monkeypatch):
    monkeypatch.setenv("CLIPPY_CHAT_MODEL", "qwen3:8b")
    assert get_chat_model() == "qwen3:8b"
