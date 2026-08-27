"""Optional CLIP / pixel image embeddings for screenshots.

PARKED / under review with the contributor who added this. Default is off
(``image_embeddings_enabled``). Ask them whether to keep CLIP and why —
Clippy today retrieves by text (a11y/OCR, titles, sessions, router/prefetch),
not by visual similarity. Enabling this loads Torch/CLIP (or falls back to a
non-text-searchable pixel signature) and adds per-frame RAM/CPU cost.

When on, vectors are stored on events and only used by screenshot_search /
event RAG visual scoring if the model id starts with ``clip:``.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from PIL import Image, ImageOps

from core.paths import get_data_dir

DEFAULT_IMAGE_MODEL = "openai/clip-vit-base-patch32"
# Enabling the capture setting should be sufficient to attempt semantic image
# embeddings. The default stays local-only and never downloads CLIP weights.
IMAGE_EMBEDDING_MODE = os.environ.get("CLIPPY_IMAGE_EMBEDDINGS", "cached").strip().lower()
IMAGE_EMBEDDING_MODEL = os.environ.get("CLIPPY_IMAGE_EMBEDDING_MODEL", DEFAULT_IMAGE_MODEL).strip()
PIXEL_EMBEDDING_MODEL = "visual-signature-v1"

_clip = None
_clip_error = None
_clip_lock = threading.Lock()


def _normalize(values) -> list[float]:
    magnitude = sum(value * value for value in values) ** 0.5
    if magnitude <= 0:
        return [0.0 for _ in values]
    return [round(value / magnitude, 7) for value in values]


def _pixel_signature(path: Path) -> list[float]:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        luminance = ImageOps.grayscale(ImageOps.fit(image, (16, 16))).resize((16, 16))
        color = ImageOps.fit(image, (8, 8))
        values = []
        values.extend((pixel - 127.5) / 127.5 for pixel in luminance.getdata())
        for pixel in color.getdata():
            values.extend((channel - 127.5) / 127.5 for channel in pixel)
        return _normalize(values)


def _get_clip():
    global _clip, _clip_error
    if _clip is not None or _clip_error is not None:
        return _clip
    if IMAGE_EMBEDDING_MODE in {"off", "fallback", "pixel"}:
        return None
    with _clip_lock:
        if _clip is not None or _clip_error is not None:
            return _clip
        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor

            cache_dir = get_data_dir() / "models" / "image_embeddings"
            cache_dir.mkdir(parents=True, exist_ok=True)
            local_only = IMAGE_EMBEDDING_MODE in {"cached", "local"}
            processor = CLIPProcessor.from_pretrained(
                IMAGE_EMBEDDING_MODEL,
                cache_dir=str(cache_dir),
                local_files_only=local_only,
            )
            model = CLIPModel.from_pretrained(
                IMAGE_EMBEDDING_MODEL,
                cache_dir=str(cache_dir),
                local_files_only=local_only,
            )
            model.eval()
            _clip = (torch, processor, model)
        except Exception as exc:
            _clip_error = exc
            print(f"[image-embeddings] CLIP unavailable: {exc}")
    return _clip


def _tensor_values(value) -> list[float]:
    if hasattr(value, "image_embeds"):
        value = value.image_embeds
    elif hasattr(value, "text_embeds"):
        value = value.text_embeds
    if isinstance(value, (tuple, list)):
        value = value[0]
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "tolist"):
        value = value.tolist()
    if value and isinstance(value[0], list):
        value = value[0]
    return _normalize([float(item) for item in value])


def embed_image(path: Path) -> tuple[list[float], str]:
    bundle = _get_clip()
    if bundle is None:
        # The pixel signature supports image-to-image deduplication only. Its
        # model id prevents it from being compared with CLIP text embeddings.
        return _pixel_signature(path), PIXEL_EMBEDDING_MODEL
    torch, processor, model = bundle
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
        inputs = processor(images=image, return_tensors="pt")
        with torch.inference_mode():
            output = model.get_image_features(**inputs)
        return _tensor_values(output), f"clip:{IMAGE_EMBEDDING_MODEL}"
    except Exception as exc:
        print(f"[image-embeddings] image encode failed for {path.name}: {exc}")
        return _pixel_signature(path), PIXEL_EMBEDDING_MODEL


def embed_text(text: str) -> list[float] | None:
    bundle = _get_clip()
    if bundle is None or not text.strip():
        return None
    torch, processor, model = bundle
    try:
        inputs = processor(text=[text], return_tensors="pt", padding=True, truncation=True)
        with torch.inference_mode():
            output = model.get_text_features(**inputs)
        return _tensor_values(output)
    except Exception as exc:
        print(f"[image-embeddings] text encode failed: {exc}")
        return None
