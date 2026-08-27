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
            # RapidOCR renamed its distribution; support both package layouts
            # without forcing users onto one runtime version.
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
    # RapidOCR has returned tuples, lists, objects, and dictionaries across
    # releases. Normalize those shapes before confidence filtering.
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
    detail = extract_text_detail(path)
    return detail["text"]


def extract_text_detail(
    path: Path,
    *,
    bypass_memory_gate: bool = False,
    min_confidence: float | None = None,
) -> dict:
    """
    Run OCR and return structured debug info.
    Used by probes; production uses extract_text() which only needs the joined string.
    """
    from core.model_residency import can_run_ocr

    threshold = OCR_MIN_CONFIDENCE if min_confidence is None else float(min_confidence)
    detail = {
        "text": "",
        "lines": [],  # list[{text, score, accepted}]
        "gated": False,
        "engine_error": None,
        "elapsed_ms": 0.0,
    }
    if not bypass_memory_gate and not can_run_ocr():
        detail["gated"] = True
        return detail

    engine = _get_engine()
    if engine is None:
        detail["engine_error"] = str(_engine_error or "engine unavailable")
        return detail

    import time

    started = time.perf_counter()
    try:
        result = engine(str(path))
        texts, scores = _parts(result)
        accepted_unique: list[str] = []
        seen = set()
        for index, value in enumerate(texts):
            score = float(scores[index]) if index < len(scores) else 1.0
            text = _clean_text(value)
            if not text:
                continue
            ok = score >= threshold
            detail["lines"].append({"text": text, "score": score, "accepted": ok})
            if ok:
                key = text.casefold()
                if key not in seen:
                    seen.add(key)
                    accepted_unique.append(text)
        detail["text"] = "\n".join(accepted_unique)[:OCR_MAX_CHARS]
    except Exception as exc:
        print(f"[ocr] failed for {path.name}: {exc}")
        detail["engine_error"] = str(exc)
    detail["elapsed_ms"] = (time.perf_counter() - started) * 1000
    return detail
