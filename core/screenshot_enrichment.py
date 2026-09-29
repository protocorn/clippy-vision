from __future__ import annotations

import re
import threading
import tempfile
from pathlib import Path

from core.accessibility_text import (
    is_useful_accessibility_text,
    normalize_accessibility_text,
)
from core.app_settings import get_capture_settings
from core.image_embeddings import embed_image
from core.ocr import extract_text
from core.ocr_crop import crop_screenshot_for_ocr
from core.performance_metrics import increment, timed
from core.uia_worker import read_persisted_accessibility_text

_cache: dict[str, tuple[int, int, str, list[float] | None, str | None, bool, bool]] = {}
_accessibility_cache: dict[str, tuple[int, int, str]] = {}
_cache_lock = threading.Lock()
_cache_limit = 512
_MAX_SCREEN_CHARS = 4000
# A crop this large is the screen. OCR it once; a second full-image pass
# repeats the same work.
_CROP_FULL_FRAME = 0.85


def merge_ocr_text(*values: str | None) -> str:
    """Normalize/dedupe lines. Prefer choose_screen_text() for a11y vs OCR selection."""
    seen = set()
    lines = []
    for value in values:
        for line in str(value or "").splitlines():
            text = " ".join(line.split()).strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                lines.append(text)
    return "\n".join(lines)[:_MAX_SCREEN_CHARS]


_WORD_RE = re.compile(r"[a-z0-9']+")
_OCR_COVERAGE = 0.75
_A11Y_EXTRA = 1.15


def _significant_words(text: str) -> list[str]:
    return [word for word in _WORD_RE.findall((text or "").casefold()) if len(word) >= 4]


def ocr_coverage_in_accessibility(ocr_text: str, accessibility_text: str) -> float:
    """Share of on-screen words that also appear in the accessibility text."""
    words = _significant_words(ocr_text)
    if not words:
        return 0.0
    haystack = set(_significant_words(accessibility_text))
    return sum(1 for word in words if word in haystack) / len(words)


def choose_screen_text(accessibility_text: str = "", ocr_text: str = "") -> str:
    """Pick the stored screen text.

    OCR is what was visible. Accessibility text is kept when it still contains
    that visible text and is materially longer, so off-screen editor or page
    text is not thrown away. A tie, or an accessibility tree that missed the
    screen, stays with OCR.
    """
    a11y = normalize_accessibility_text(accessibility_text)
    ocr = merge_ocr_text(ocr_text)
    ocr_useful = is_useful_accessibility_text(ocr)
    a11y_useful = is_useful_accessibility_text(a11y)
    coverage = ocr_coverage_in_accessibility(ocr, a11y)
    a11y_covers_screen = (
        ocr_useful
        and a11y_useful
        and len(_significant_words(ocr)) >= 4
        and coverage >= _OCR_COVERAGE
        and len(a11y) > len(ocr) * _A11Y_EXTRA
    )
    if a11y_covers_screen:
        chosen = a11y
    elif ocr_useful:
        chosen = ocr
    elif a11y.strip():
        chosen = a11y
    else:
        chosen = ocr
    from core.secret_patterns import redact_secrets

    return redact_secrets(chosen)[:_MAX_SCREEN_CHARS]


def remember_accessibility_text(path: Path, text: str) -> None:
    stat = path.stat()
    with _cache_lock:
        _accessibility_cache[str(path)] = (
            stat.st_mtime_ns,
            stat.st_size,
            normalize_accessibility_text(text),
        )
        while len(_accessibility_cache) > _cache_limit:
            stale = next(iter(_accessibility_cache))
            _accessibility_cache.pop(stale, None)
            _cache.pop(stale, None)


def _captured_accessibility_text(path: Path, stat) -> str:
    with _cache_lock:
        cached = _accessibility_cache.get(str(path))
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
    # Capture and enrichment run in different OS processes, so the in-memory
    # cache above is usually empty here. Prefer the UIA worker's sidecar file.
    persisted = read_persisted_accessibility_text(path)
    if persisted.strip():
        return normalize_accessibility_text(persisted)
    return ""


def _crop_covers_frame(crop: dict) -> bool:
    box = crop.get("box") or []
    size = crop.get("image_size") or []
    if len(box) != 4 or len(size) != 2:
        return False
    width, height = float(size[0] or 0), float(size[1] or 0)
    if width <= 0 or height <= 0:
        return False
    area = max(0.0, float(box[2]) - float(box[0])) * max(0.0, float(box[3]) - float(box[1]))
    return area / (width * height) >= _CROP_FULL_FRAME


def extract_screenshot_ocr(path: Path) -> str:
    """
    OCR the content crop once. A second full-image pass runs only when the
    crop is a small slice and its text is present but not actually useful.
    A near-full crop, or a crop that produced no text, is not run again.
    """
    with tempfile.TemporaryDirectory(prefix="clippy_ocr_crop_") as tmp:
        cropped_path = Path(tmp) / "content.jpg"
        with timed("ocr.crop_prepare"):
            crop = crop_screenshot_for_ocr(path, cropped_path)
        if crop:
            with timed("ocr.crop_inference"):
                cropped_text = extract_text(cropped_path)
            useful = is_useful_accessibility_text(cropped_text)
            if useful or _crop_covers_frame(crop) or not str(cropped_text or "").strip():
                increment("ocr.crop_accepted" if useful else "ocr.skipped_second_pass")
                return cropped_text
    increment("ocr.full_fallback")
    with timed("ocr.full_inference"):
        return extract_text(path)


def enrich_screenshot(
    path: Path,
    *,
    include_image_embedding: bool = True,
) -> tuple[str, list[float] | None, str | None]:
    stat = path.stat()
    key = str(path)
    settings = get_capture_settings()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            ocr_cached = cached[5]
            embeddings_cached = cached[6]
            # Feature flags are part of cache validity: enabling OCR or image
            # embeddings later must enrich the file instead of returning gaps.
            if (not settings["ocr_enabled"] or ocr_cached) and (
                not settings["image_embeddings_enabled"] or not include_image_embedding or embeddings_cached
            ):
                return (
                    cached[2] if settings["ocr_enabled"] else "",
                    cached[3] if settings["image_embeddings_enabled"] and include_image_embedding else None,
                    cached[4] if settings["image_embeddings_enabled"] and include_image_embedding else None,
                )

    with timed("enrichment.total"):
        accessibility_text = _captured_accessibility_text(path, stat)
        a11y_useful = is_useful_accessibility_text(accessibility_text)
        should_run_ocr = settings["ocr_enabled"] and not a11y_useful
        if settings["ocr_enabled"] and a11y_useful:
            increment("ocr.skipped_accessibility")
        ocr_text = extract_screenshot_ocr(path) if should_run_ocr else ""
        captured_text = choose_screen_text(accessibility_text, ocr_text)
        # CLIP/image embeddings: gated by image_embeddings_enabled (default off;
        # parked pending contributor keep/remove decision — see image_embeddings.py).
        should_embed_image = settings["image_embeddings_enabled"] and include_image_embedding
        image_embedding, image_embedding_model = (embed_image(path) if should_embed_image else (None, None))
        result = (captured_text, image_embedding, image_embedding_model)
    with _cache_lock:
        _cache[key] = (
            stat.st_mtime_ns,
            stat.st_size,
            *result,
            bool(settings["ocr_enabled"]),
            bool(should_embed_image),
        )
        while len(_cache) > _cache_limit:
            stale = next(iter(_cache))
            _cache.pop(stale)
            _accessibility_cache.pop(stale, None)
    return result
