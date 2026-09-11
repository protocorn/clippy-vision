"""Single source of truth for which local Ollama chat model Clippy uses.

Electron writes the user's setup-wizard/settings choice to
``core/data/llm_config.json`` and passes it to every Python child process as
the ``CLIPPY_CHAT_MODEL`` environment variable (see
electron-ui/electron/lib/api-spawn.js). Every module that calls the chat/
reasoning model (summarizer, distiller, ReAct agent, classifiers, router,
residency warmup) must resolve the model through ``get_chat_model()`` here
instead of hardcoding a name. Hardcoding causes Ollama to silently pull the
hardcoded model even when the user picked something else in setup — see
GitHub report from @proton_skull (still downloads qwen3:8b with a different
model configured).
"""
from __future__ import annotations

import os

# Only used when nothing else is configured (fresh env, no env var, no config
# file) — mirrors electron-ui/electron/lib/paths.js DEFAULT_LLM_CONFIG.chat_model.
DEFAULT_CHAT_MODEL = "qwen3:8b"


def get_chat_model() -> str:
    """Return the chat model to use for this process.

    Resolution order:
    1. ``CLIPPY_CHAT_MODEL`` env var (set by Electron from llm_config.json).
    2. ``core/data/llm_config.json`` directly, for Python entry points that
       run outside Electron (tests, CLI scripts, ``uvicorn`` reload workers).
    3. ``DEFAULT_CHAT_MODEL``.
    """
    env_model = (os.environ.get("CLIPPY_CHAT_MODEL") or "").strip()
    if env_model:
        return env_model

    try:
        from core.paths import get_data_dir
    except ImportError:
        from paths import get_data_dir

    try:
        import json

        config_path = get_data_dir() / "llm_config.json"
        data = json.loads(config_path.read_text(encoding="utf-8"))
        configured = str(data.get("chat_model") or "").strip()
        if configured:
            return configured
    except Exception:
        pass

    return DEFAULT_CHAT_MODEL
