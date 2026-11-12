"""Shared Ollama generation options for every Clippy inference call.

Ollama's default ``num_ctx`` is 2048 on many versions. Summary, search, and
SQL prompts are larger than that, so without an explicit context size the
instructions and the evidence get silently truncated. Every ``/api/chat`` and
``/api/generate`` call must send this value.

Embeddings stay on local MiniLM and do not go through Ollama, so they are
not covered here.

Override at runtime with ``CLIPPY_OLLAMA_NUM_CTX`` (positive integer).
"""
from __future__ import annotations

import os
from typing import Any

DEFAULT_NUM_CTX = 8192
# Coarse English heuristic used only for the overflow warning. Ollama counts
# tokens, not characters; this is a hint for operators, not a tokenizer.
_CHARS_PER_TOKEN = 4
_INFERENCE_PATHS = frozenset({"/api/generate", "/api/chat"})


def get_num_ctx() -> int:
    raw = (os.environ.get("CLIPPY_OLLAMA_NUM_CTX") or "").strip()
    if not raw:
        return DEFAULT_NUM_CTX
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_NUM_CTX
    if value < 1:
        return DEFAULT_NUM_CTX
    return value


def merge_options(options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Caller options plus the configured ``num_ctx``.

    ``num_ctx`` always comes from ``get_num_ctx()``. A value in ``options`` is
    overwritten so callers cannot silently pick a smaller window.
    """
    merged = dict(options or {})
    merged["num_ctx"] = get_num_ctx()
    return merged


def is_inference_path(path: str) -> bool:
    return path.rstrip("/") in _INFERENCE_PATHS


def apply_inference_options(body: dict[str, Any]) -> dict[str, Any]:
    """Copy a generate/chat body and attach ``options.num_ctx``."""
    applied = dict(body)
    existing = applied.get("options")
    applied["options"] = merge_options(existing if isinstance(existing, dict) else None)
    warn_if_prompt_exceeds(applied)
    return applied


def estimate_prompt_chars(body: dict[str, Any]) -> int:
    total = 0
    for key in ("prompt", "system"):
        value = body.get(key)
        if isinstance(value, str):
            total += len(value)
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        total += len(part["text"])
    return total


def warn_if_prompt_exceeds(body: dict[str, Any], *, num_ctx: int | None = None) -> None:
    ctx = get_num_ctx() if num_ctx is None else num_ctx
    if ctx < 1:
        return
    estimated_tokens = (estimate_prompt_chars(body) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN
    if estimated_tokens > ctx:
        print(
            f"[ollama] prompt is ~{estimated_tokens} tokens but num_ctx={ctx}; "
            "the model may truncate instructions or evidence"
        )
