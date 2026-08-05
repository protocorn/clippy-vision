from __future__ import annotations

import re
import threading
from pathlib import Path


OCR_MIN_CONFIDENCE = 0.55
OCR_MAX_CHARS = 4000

_engine = None
_engine_lock = threading.Lock()
_engine_error = None
_space_re = re.compile(r"\s+")


def _get_engine():
    global _engine, _engine_error
    if _engine is not None or _engine_error is not None:
        return _engine
    with _engine_lock:
        if _engine is not None or _engine_error is not None:
            return _engine
        try:
            try:
                from rapidocr import RapidOCR
            except ImportError:
                from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
        except Exception as exc:
            _engine_error = exc
            print(f"[ocr] unavailable: {exc}")
    return _engine


def _parts(result):
    if result is None:
        return [], []
    if isinstance(result, tuple) and len(result) >= 3:
        return result[1] or [], result[2] or []
    if isinstance(result, list) and len(result) >= 3 and not isinstance(result[0], str):
        return result[1] or [], result[2] or []
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if texts is not None:
        return texts or [], scores or []
    if isinstance(result, dict):
        return result.get("txts") or result.get("texts") or [], result.get("scores") or []
    return [], []


def _clean_text(value: object) -> str:
    text = _space_re.sub(" ", str(value or "")).strip()
    return text if len(text) >= 2 else ""


def extract_text(path: Path) -> str:
    engine = _get_engine()
    if engine is None:
        return ""
    try:
        result = engine(str(path))
        texts, scores = _parts(result)
        accepted = []
        for index, value in enumerate(texts):
            score = float(scores[index]) if index < len(scores) else 1.0
            text = _clean_text(value)
            if text and score >= OCR_MIN_CONFIDENCE:
                accepted.append(text)
        seen = set()
        unique = []
        for text in accepted:
            key = text.casefold()
            if key not in seen:
                seen.add(key)
                unique.append(text)
        return "\n".join(unique)[:OCR_MAX_CHARS]
    except Exception as exc:
        print(f"[ocr] failed for {path.name}: {exc}")
        return ""
