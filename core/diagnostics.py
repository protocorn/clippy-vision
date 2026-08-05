from __future__ import annotations

import importlib.util
import platform
import sys

from core.capture_state import get_capture_status
from core.app_settings import get_capture_settings
from core.llm_config import get_llm_config, public_llm_config
from core.llm_gateway import gateway
from core.local_embeddings import embedding_status
from core.platform_support import IS_MACOS, get_window_metadata, platform_label
from core.storage import get_data_stats


_REQUIRED_IMPORTS = {
    "mss": "screen capture",
    "PIL": "image processing",
    "pynput": "keyboard capture",
    "psutil": "system metrics",
    "imagehash": "screenshot deduplication",
    "rapidocr": "local OCR",
    "onnxruntime": "OCR inference",
    "transformers": "bundled MiniLM embeddings",
    "torch": "bundled MiniLM embeddings",
    "sklearn": "router classifier",
}


def _package_status() -> list[dict]:
    return [
        {
            "name": name,
            "purpose": purpose,
            "installed": importlib.util.find_spec(name) is not None,
        }
        for name, purpose in _REQUIRED_IMPORTS.items()
    ]


def _permission_status() -> dict:
    if not IS_MACOS:
        return {"screen_recording": "not_required", "accessibility": "not_required"}
    metadata = get_window_metadata()
    return {
        "screen_recording": "manual_check",
        "accessibility": "available" if metadata else "needs_permission_or_no_front_window",
        "instructions": "System Settings → Privacy & Security → Screen Recording and Accessibility",
    }


def get_diagnostics() -> dict:
    config = get_llm_config()
    return {
        "platform": platform_label(),
        "python": sys.version.split()[0],
        "architecture": platform.machine(),
        "permissions": _permission_status(),
        "packages": _package_status(),
        "capture": get_capture_status(),
        "capture_settings": get_capture_settings(),
        "storage": get_data_stats(),
        "llm": public_llm_config(config),
        "provider": gateway.capabilities(config),
        "embeddings": embedding_status(),
    }
