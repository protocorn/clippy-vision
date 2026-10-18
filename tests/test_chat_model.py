from core.chat_model import DEFAULT_CHAT_MODEL, LEGACY_DEFAULT_CHAT_MODEL, get_chat_model


def test_default_chat_model_is_small_llama():
    assert DEFAULT_CHAT_MODEL == "llama3.2:3b"


def test_get_chat_model_prefers_env(monkeypatch):
    monkeypatch.setenv("CLIPPY_CHAT_MODEL", "qwen3:8b")
    assert get_chat_model() == "qwen3:8b"


def test_existing_config_without_chat_model_keeps_legacy_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CLIPPY_CHAT_MODEL", raising=False)
    monkeypatch.setenv("CLIPPY_DATA_DIR", str(tmp_path))
    (tmp_path / "llm_config.json").write_text(
        '{"provider":"ollama","base_url":"http://127.0.0.1:11434"}',
        encoding="utf-8",
    )
    assert get_chat_model() == LEGACY_DEFAULT_CHAT_MODEL
    assert LEGACY_DEFAULT_CHAT_MODEL == "qwen3:8b"


def test_missing_config_uses_new_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CLIPPY_CHAT_MODEL", raising=False)
    monkeypatch.setenv("CLIPPY_DATA_DIR", str(tmp_path))
    assert get_chat_model() == DEFAULT_CHAT_MODEL
