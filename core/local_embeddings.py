from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from collections.abc import Iterable
from pathlib import Path

# The bundled model is the preferred path. The deterministic hash encoder keeps
# keyword-like retrieval available when PyTorch or the model cannot be loaded.
MODEL_ID = "local:sentence-transformers/all-MiniLM-L6-v2"
MODEL_DIMENSION = 384
MAX_TOKENS = 256
_MODEL_META_KEY = "embeddings.model"
_TOKEN_RE = re.compile(r"[\w][\w./:-]*", re.UNICODE)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_MODEL_DIR = _PROJECT_ROOT / "models" / "embeddings" / "all-MiniLM-L6-v2"

_bundle = None
_load_error: Exception | None = None
_load_lock = threading.Lock()
_migration_lock = threading.Lock()
_migrated_model_id: str | None = None


def model_dir() -> Path:
    override = os.environ.get("CLIPPY_EMBEDDING_MODEL_DIR", "").strip()
    return Path(override).expanduser() if override else _DEFAULT_MODEL_DIR


def _load_bundle():
    global _bundle, _load_error
    # Cache both success and failure so every embedding call does not retry an
    # expensive model import after a known startup failure.
    if _bundle is not None or _load_error is not None:
        return _bundle
    with _load_lock:
        if _bundle is not None or _load_error is not None:
            return _bundle
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer

            path = model_dir()
            if not (path / "config.json").is_file() or not (path / "model.safetensors").is_file():
                raise FileNotFoundError(f"Bundled MiniLM model is missing from {path}")
            tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
            model = AutoModel.from_pretrained(path, local_files_only=True)
            if int(model.config.hidden_size) != MODEL_DIMENSION:
                raise ValueError(f"Expected {MODEL_DIMENSION}-dimensional MiniLM, got {model.config.hidden_size}")
            # MiniLM is fast on CPU, and a CUDA context here would cost VRAM the
            # chat model needs as one contiguous block. GPU stays opt-in.
            requested_device = os.environ.get("CLIPPY_EMBED_DEVICE", "cpu").strip().lower()
            if requested_device == "auto":
                if torch.cuda.is_available():
                    requested_device = "cuda"
                elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                    requested_device = "mps"
                else:
                    requested_device = "cpu"
            device = torch.device(requested_device)
            model.to(device)
            model.eval()
            _bundle = (torch, tokenizer, model, device)
        except Exception as exc:
            _load_error = exc
            print(f"[local-embeddings] MiniLM unavailable; using local hash fallback: {exc}")
    return _bundle


def _hash_embedding(text: str) -> list[float]:
    vector = [0.0] * MODEL_DIMENSION
    tokens = _TOKEN_RE.findall(text.casefold())
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
        index = int.from_bytes(digest[:4], "big") % MODEL_DIMENSION
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    for left, right in zip(tokens, tokens[1:]):
        digest = hashlib.blake2b(f"{left} {right}".encode(), digest_size=16).digest()
        index = int.from_bytes(digest[:4], "big") % MODEL_DIMENSION
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += 0.5 * sign
    magnitude = math.sqrt(sum(value * value for value in vector))
    return [value / magnitude for value in vector] if magnitude else vector


def _encode_with_bundle(bundle, texts: list[str]) -> list[list[float]]:
    torch, tokenizer, model, device = bundle
    vectors: list[list[float]] = []
    for start in range(0, len(texts), 32):
        batch = texts[start:start + 32]
        encoded = tokenizer(
            batch,
            padding=True,
            truncation=True,
            max_length=MAX_TOKENS,
            return_tensors="pt",
        )
        encoded = {key: value.to(device) for key, value in encoded.items()}
        with torch.inference_mode():
            output = model(**encoded).last_hidden_state
            # MiniLM returns token embeddings; masked mean pooling matches the
            # sentence-transformers model's expected sentence representation.
            mask = encoded["attention_mask"].unsqueeze(-1).expand(output.size()).float()
            pooled = (output * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)
            normalized = torch.nn.functional.normalize(pooled, p=2, dim=1)
        vectors.extend(normalized.detach().cpu().tolist())
    return vectors


def _recompute_memory_centroids(conn) -> None:
    rows = conn.execute(
        "SELECT cluster_id, vector_embedding FROM memory_facts WHERE valid_to IS NULL"
    ).fetchall()
    grouped: dict[str, list[list[float]]] = {}
    for cluster_id, encoded in rows:
        try:
            vector = json.loads(encoded)
            if isinstance(vector, list) and vector:
                grouped.setdefault(cluster_id, []).append([float(item) for item in vector])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    cluster_rows = conn.execute("SELECT cluster_id FROM memory_clusters").fetchall()
    for (cluster_id,) in cluster_rows:
        vectors = grouped.get(cluster_id, [])
        if vectors:
            dimension = min(len(vector) for vector in vectors)
            centroid = [
                sum(vector[index] for vector in vectors) / len(vectors)
                for index in range(dimension)
            ]
            fact_count = len(vectors)
        else:
            centroid = [0.0] * MODEL_DIMENSION
            fact_count = 0
        conn.execute(
            "UPDATE memory_clusters SET centroid = ?, updated_at = ?, fact_count = ? WHERE cluster_id = ?",
            (json.dumps(centroid), time.time(), fact_count, cluster_id),
        )


def _clear_identity_embeddings(conn) -> None:
    rows = conn.execute(
        "SELECT key, value FROM memory_meta WHERE key LIKE 'identity.%'"
    ).fetchall()
    for key, encoded in rows:
        try:
            data = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("field_embedding") is None:
            continue
        data["field_embedding"] = None
        conn.execute(
            "UPDATE memory_meta SET value = ? WHERE key = ?",
            (json.dumps(data), key),
        )


def _ensure_storage_model(model_id: str, bundle) -> None:
    global _migrated_model_id
    if _migrated_model_id == model_id:
        return
    with _migration_lock:
        if _migrated_model_id == model_id:
            return
        from core.storage import conn

        row = conn.execute("SELECT value FROM memory_meta WHERE key = ?", (_MODEL_META_KEY,)).fetchone()
        if row and row[0] == model_id:
            _migrated_model_id = model_id
            return

        try:
            fact_rows = conn.execute("SELECT fact_id, text FROM memory_facts").fetchall()
            fact_vectors = []
            if fact_rows:
                fact_texts = [str(text or "") for _, text in fact_rows]
                fact_vectors = (
                    _encode_with_bundle(bundle, fact_texts)
                    if bundle is not None
                    else [_hash_embedding(text) for text in fact_texts]
                )

            # Vectors from different encoders cannot be compared safely. Keep
            # facts usable by rebuilding them now and lazily backfill bulk data.
            conn.execute("UPDATE events SET vector_embedding = NULL")
            conn.execute("UPDATE sessions SET summary_embedding = NULL")
            conn.execute("UPDATE conversations SET vector_embedding = NULL")
            for (fact_id, _), vector in zip(fact_rows, fact_vectors):
                conn.execute(
                    "UPDATE memory_facts SET vector_embedding = ? WHERE fact_id = ?",
                    (json.dumps(vector), fact_id),
                )
            _clear_identity_embeddings(conn)
            _recompute_memory_centroids(conn)
            conn.execute(
                "INSERT OR REPLACE INTO memory_meta (key, value) VALUES (?, ?)",
                (_MODEL_META_KEY, model_id),
            )
            conn.commit()
            _migrated_model_id = model_id
        except Exception:
            conn.rollback()
            raise


def embed_texts(texts: Iterable[str]) -> list[list[float]]:
    if isinstance(texts, str):
        texts = [texts]
    values = [str(text or "") for text in texts]
    if not values:
        return []
    bundle = _load_bundle()
    active_model_id = MODEL_ID if bundle is not None else "local:hash-384-v1"
    _ensure_storage_model(active_model_id, bundle)
    if bundle is None:
        return [_hash_embedding(text) for text in values]
    return _encode_with_bundle(bundle, values)


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]


def embedding_status() -> dict:
    bundle = _bundle
    bundled = (model_dir() / "model.safetensors").is_file()
    active_model = MODEL_ID if bundled and _load_error is None else "local:hash-384-v1"
    return {
        "provider": "bundled",
        "model": active_model,
        "dimension": MODEL_DIMENSION,
        "model_path": str(model_dir()),
        "bundled": bundled,
        "loaded": bundle is not None,
        "error": str(_load_error) if _load_error else None,
    }
