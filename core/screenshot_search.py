from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

from core.app_settings import get_capture_settings
from core.image_embeddings import embed_text as embed_image_text
from core.local_embeddings import embed_text
from core.paths import get_screenshots_dir
from core.storage import conn

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]{2,}", re.IGNORECASE)
_STOPWORDS = {
    "the", "and", "this", "that", "what", "when", "where", "with", "from",
    "your", "you", "did", "was", "for", "about", "into", "how", "which",
    "who", "a", "an", "to", "of", "on", "in", "at", "it", "is", "are",
}


def _decode_vector(value) -> list[float] | None:
    if not value:
        return None
    try:
        vector = json.loads(value) if isinstance(value, str) else value
        return [float(item) for item in vector] if isinstance(vector, list) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _cosine(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    if not size:
        return 0.0
    dot = sum(left[index] * right[index] for index in range(size))
    left_norm = math.sqrt(sum(value * value for value in left[:size]))
    right_norm = math.sqrt(sum(value * value for value in right[:size]))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _terms(query: str) -> list[str]:
    return [
        token.lower()
        for token in _WORD_RE.findall(query or "")
        if token.lower() not in _STOPWORDS
    ]


def _keyword_score(query: str, text: str) -> float:
    terms = _terms(query)
    if not terms:
        return 0.0
    lowered = text.lower()
    return sum(term in lowered for term in terms) / len(terms)


def resolve_screenshot_filename(timestamp: float, preferred: str | None = None) -> str | None:
    root = get_screenshots_dir()
    if preferred:
        safe = Path(preferred).name
        if safe == preferred and (root / safe).is_file():
            return safe
        if safe == preferred:
            preferred_stamp = safe.split("_", 1)[0].split(".", 1)[0]
            if preferred_stamp.isdigit():
                matches = sorted(root.glob(f"{preferred_stamp}*.jpg"))
                if matches:
                    processed = [path for path in matches if "_processed" in path.stem]
                    return (processed or matches)[-1].name
    stamp = str(int(float(timestamp) * 1000))
    matches = sorted(root.glob(f"{stamp}*.jpg"))
    if matches:
        processed = [path for path in matches if "_processed" in path.stem]
        return (processed or matches)[-1].name
    return None


def _rows(start_ts: float | None = None, end_ts: float | None = None) -> list[dict]:
    filters = [
        "(event_type = 'screenshot_analysis' OR vision_ocr_text IS NOT NULL "
        "OR screenshot_filename IS NOT NULL OR image_embedding IS NOT NULL)"
    ]
    params = []
    if start_ts is not None:
        filters.append("timestamp >= ?")
        params.append(start_ts)
    if end_ts is not None:
        filters.append("timestamp < ?")
        params.append(end_ts)
    # Apply temporal filters before the safety cap so older exact-time lookups
    # are not hidden by newer screenshots.
    records = conn.execute(
        f"""SELECT event_id, timestamp, event_type, process_name,
                  current_window_title, active_url, summary, interest_reason,
                  vision_ocr_text, vision_activity, vision_suggested_action,
                  vector_embedding, image_embedding, image_embedding_model,
                  screenshot_filename,
                  interesting, interest_score
           FROM events
           WHERE {' AND '.join(filters)}
           ORDER BY timestamp DESC
           LIMIT 3000""",
        params,
    ).fetchall()
    return [
        {
            "event_id": row[0],
            "timestamp": row[1],
            "event_type": row[2],
            "process_name": row[3],
            "current_window_title": row[4],
            "active_url": row[5],
            "summary": row[6],
            "interest_reason": row[7],
            "vision_ocr_text": row[8],
            "vision_activity": row[9],
            "vision_suggested_action": row[10],
            "vector_embedding": row[11],
            "image_embedding": row[12],
            "image_embedding_model": row[13],
            "screenshot_filename": row[14],
            "interesting": bool(row[15]),
            "interest_score": row[16],
        }
        for row in records
    ]


def _display_record(row: dict, score: float | None = None) -> dict:
    filename = resolve_screenshot_filename(row["timestamp"], row.get("screenshot_filename"))
    ocr = (row.get("vision_ocr_text") or "").strip()
    return {
        "event_id": row["event_id"],
        "timestamp": row["timestamp"],
        "event_type": row["event_type"],
        "process_name": row["process_name"],
        "current_window_title": row["current_window_title"],
        "active_url": row["active_url"],
        "summary": row["summary"],
        "interest_reason": row["interest_reason"],
        "vision_ocr_text": ocr[:1200],
        "vision_activity": row["vision_activity"],
        "vision_suggested_action": row["vision_suggested_action"],
        "interesting": row["interesting"],
        "interest_score": row["interest_score"],
        "relevance": round(score, 4) if score is not None else None,
        "screenshot_filename": filename,
        "screenshot_url": f"/screenshots/{filename}" if filename else None,
    }


def search_screenshots(
    query: str = "",
    *,
    start_ts: float | None = None,
    end_ts: float | None = None,
    limit: int = 40,
    offset: int = 0,
) -> dict:
    try:
        limit = min(max(int(limit), 1), 100)
    except (TypeError, ValueError):
        limit = 40
    try:
        offset = max(int(offset), 0)
    except (TypeError, ValueError):
        offset = 0
    rows = _rows(start_ts, end_ts)
    query = (query or "").strip()
    if not query:
        selected = rows[offset: offset + limit]
        return {"screenshots": [_display_record(row) for row in selected], "total": len(rows), "query": ""}

    settings = get_capture_settings()
    query_vector = None
    # PARKED: rag_enabled / image_embeddings_enabled — see app_settings defaults.
    if settings["rag_enabled"]:
        try:
            query_vector = embed_text(query)
        except Exception as exc:
            print(f"[screenshot-search] text search unavailable: {exc}")
    image_query = None
    if settings["image_embeddings_enabled"]:
        try:
            image_query = embed_image_text(query)
        except Exception as exc:
            print(f"[screenshot-search] visual search unavailable: {exc}")

    now = time.time()
    scored = []
    for row in rows:
        searchable = " | ".join(
            str(row.get(key) or "")
            for key in (
                "process_name", "current_window_title", "active_url", "summary",
                "interest_reason", "vision_ocr_text", "vision_activity",
                "vision_suggested_action",
            )
        )
        keyword = _keyword_score(query, searchable)
        semantic = _cosine(query_vector, _decode_vector(row.get("vector_embedding"))) if query_vector and row.get("vector_embedding") else 0.0
        visual = _cosine(image_query, _decode_vector(row.get("image_embedding"))) if image_query and str(row.get("image_embedding_model") or "").startswith("clip:") else 0.0
        if not keyword and not semantic and not visual:
            continue
        age_days = max(0.0, (now - float(row["timestamp"])) / 86400.0)
        recency = math.exp(-math.log(2) * age_days / 30.0)
        # OCR/metadata and MiniLM carry most of the signal; CLIP and recency
        # refine ordering when several frames describe the same activity.
        score = (keyword * 0.35) + (max(0.0, semantic) * 0.45) + (max(0.0, visual) * 0.20) + (recency * 0.04)
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = scored[offset: offset + limit]
    return {
        "screenshots": [_display_record(row, score) for score, row in selected],
        "total": len(scored),
        "query": query,
    }
