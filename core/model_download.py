"""Download Clippy Vision ML weights from Hugging Face on first use.

Embeddings use the public sentence-transformers MiniLM. The query router is
hosted on our HF model repo (fine-tuned checkpoint + tokenizer).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

EMBEDDING_REPO = os.environ.get(
    "CLIPPY_EMBEDDING_HF_REPO",
    "sentence-transformers/all-MiniLM-L6-v2",
).strip()
ROUTER_REPO = os.environ.get(
    "CLIPPY_ROUTER_HF_REPO",
    "sahil2412/clippy-vision-router",
).strip()

_DEFAULT_EMBEDDING_DIR = _PROJECT_ROOT / "models" / "embeddings" / "all-MiniLM-L6-v2"
_DEFAULT_ROUTER_DIR = _PROJECT_ROOT / "models" / "router_classifier" / "best"

_embedding_lock = threading.Lock()
_router_lock = threading.Lock()


def embedding_model_dir() -> Path:
    override = os.environ.get("CLIPPY_EMBEDDING_MODEL_DIR", "").strip()
    return Path(override).expanduser() if override else _DEFAULT_EMBEDDING_DIR


def router_model_dir() -> Path:
    override = os.environ.get("CLIPPY_ROUTER_MODEL_DIR", "").strip()
    return Path(override).expanduser() if override else _DEFAULT_ROUTER_DIR


def embedding_ready(path: Path | None = None) -> bool:
    root = path or embedding_model_dir()
    return (root / "config.json").is_file() and (
        (root / "model.safetensors").is_file() or (root / "pytorch_model.bin").is_file()
    )


def router_ready(path: Path | None = None) -> bool:
    root = path or router_model_dir()
    return (root / "model.pt").is_file() and (root / "tokenizer.json").is_file()


def _snapshot(repo_id: str, local_dir: Path) -> Path:
    from huggingface_hub import snapshot_download

    local_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repo_id,
        local_dir=str(local_dir),
    )
    return local_dir


def ensure_embedding_model(*, force: bool = False) -> Path:
    """Ensure official MiniLM weights exist under models/embeddings/…"""
    path = embedding_model_dir()
    with _embedding_lock:
        if not force and embedding_ready(path):
            return path
        print(f"[models] Downloading embeddings from {EMBEDDING_REPO} -> {path}")
        _snapshot(EMBEDDING_REPO, path)
        if not embedding_ready(path):
            raise FileNotFoundError(
                f"Downloaded {EMBEDDING_REPO} but weights are missing under {path}"
            )
        print(f"[models] Embeddings ready at {path}")
        return path


def ensure_router_model(*, force: bool = False) -> Path:
    """Ensure fine-tuned router checkpoint exists under models/router_classifier/best."""
    path = router_model_dir()
    with _router_lock:
        if not force and router_ready(path):
            return path
        print(f"[models] Downloading router from {ROUTER_REPO} -> {path}")
        _snapshot(ROUTER_REPO, path)
        if not router_ready(path):
            raise FileNotFoundError(
                f"Downloaded {ROUTER_REPO} but model.pt/tokenizer are missing under {path}"
            )
        print(f"[models] Router ready at {path}")
        return path


def ensure_all(*, force: bool = False) -> dict[str, str]:
    """Download both local ML packs. Safe to call from setup or API startup."""
    embedding = ensure_embedding_model(force=force)
    router = ensure_router_model(force=force)
    return {
        "embedding_repo": EMBEDDING_REPO,
        "embedding_dir": str(embedding),
        "router_repo": ROUTER_REPO,
        "router_dir": str(router),
    }


if __name__ == "__main__":
    info = ensure_all()
    for key, value in info.items():
        print(f"{key}={value}")
