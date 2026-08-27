"""Optional hybrid RAG index for raw activity events.

PARKED / under review with the contributor who added this. Default is off
(``rag_enabled``). Decide keep-or-remove with them and document the reason.

This is NOT the primary chat retrieval path. Router + prefetch already cover
most recall via session summaries, time windows, artifact SQL/FTS, and memory
facts. When enabled it only adds fuzzy semantic search over raw events
(OCR/titles/payloads) for:
  - search_events first try (falls back to LLM→SQL when off/empty)
  - topic_search fallback when no session summaries exist

Useful events are embedded with local MiniLM, stored in
``events.vector_embedding``, and ranked by cosine + keyword score. SQLite
stays the source of truth; there is no separate vector DB.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Iterable

from core.app_settings import get_capture_settings
from core.image_embeddings import embed_text as embed_image_text
from core.llm_gateway import Priority
from core.local_embeddings import embed_text, embed_texts
from core.screenshot_search import resolve_screenshot_filename
from core.storage import conn

MAX_CANDIDATES = 900
INLINE_BACKFILL = 32
INDEX_BATCH = 16
INDEX_INTERVAL_SECONDS = 15
_STOPWORDS = {
    "the", "and", "that", "this", "what", "when", "where", "were", "with",
    "from", "have", "about", "into", "your", "you", "did", "was", "for",
    "then", "than", "they", "them", "i", "me", "my", "a", "an", "to", "of",
    "on", "in", "at", "it", "is", "are", "or", "how", "which", "who",
}
_INDEXABLE_TYPES = {
    "context_change",
    "paste",
    "clipboard_change",
    "screenshot_analysis",
    "deviation",
    "typing_burst",
    "mouse_burst",
}
_INDEXABLE_TYPE_PARAMS = tuple(sorted(_INDEXABLE_TYPES))
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]{2,}", re.IGNORECASE)

_stop = threading.Event()
_thread: threading.Thread | None = None
_start_lock = threading.Lock()


def _event_text(row: dict) -> str:
    values = [
        row.get("process_name"),
        row.get("current_window_title"),
        row.get("active_url"),
        row.get("summary"),
        row.get("interest_reason"),
        row.get("vision_activity"),
        row.get("vision_ocr_text"),
        row.get("vision_suggested_action"),
    ]
    payload = row.get("payload")
    if payload:
        values.append(payload)
    return " | ".join(str(value).strip() for value in values if value).strip()[:3000]


def _decode_vector(value) -> list[float] | None:
    if not value:
        return None
    try:
        vector = json.loads(value) if isinstance(value, str) else value
        if not isinstance(vector, list):
            return None
        return [float(item) for item in vector]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _cosine(left: list[float], right: list[float]) -> float:
    size = min(len(left), len(right))
    if size == 0:
        return 0.0
    dot = sum(left[index] * right[index] for index in range(size))
    left_norm = math.sqrt(sum(value * value for value in left[:size]))
    right_norm = math.sqrt(sum(value * value for value in right[:size]))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _query_terms(query: str) -> list[str]:
    return [
        token.lower()
        for token in _WORD_RE.findall(query or "")
        if token.lower() not in _STOPWORDS
    ]


def _keyword_score(query: str, text: str) -> float:
    terms = _query_terms(query)
    if not terms:
        return 0.0
    lowered = text.lower()
    hits = sum(1 for term in terms if term in lowered)
    return hits / len(terms)


def _row_dict(row) -> dict:
    return {
        "event_id": row[0],
        "timestamp": row[1],
        "event_type": row[2],
        "process_name": row[3],
        "current_window_title": row[4],
        "active_url": row[5],
        "summary": row[6],
        "payload": row[7],
        "interest_reason": row[8],
        "vision_ocr_text": row[9],
        "vision_activity": row[10],
        "vision_suggested_action": row[11],
        "vector_embedding": row[12],
        "image_embedding": row[13],
        "image_embedding_model": row[14],
        "screenshot_filename": row[15],
    }


def _candidate_rows(start_ts: float | None, end_ts: float | None) -> list[dict]:
    filters = [
        "(interesting = 1 OR event_type IN ({types}))".format(
            types=", ".join("?" for _ in _INDEXABLE_TYPE_PARAMS)
        ),
    ]
    params: list = list(_INDEXABLE_TYPE_PARAMS)
    if start_ts is not None:
        filters.append("timestamp >= ?")
        params.append(start_ts)
    if end_ts is not None:
        filters.append("timestamp < ?")
        params.append(end_ts)
    sql = f"""
        SELECT event_id, timestamp, event_type,
               process_name, current_window_title, active_url,
               summary, payload, interest_reason,
               vision_ocr_text, vision_activity, vision_suggested_action,
               vector_embedding, image_embedding, image_embedding_model,
               screenshot_filename
        FROM events
        WHERE {' AND '.join(filters)}
        ORDER BY timestamp DESC
        LIMIT ?
    """
    params.append(MAX_CANDIDATES)
    return [_row_dict(row) for row in conn.execute(sql, params).fetchall()]


def _store_embeddings(rows: list[dict], vectors: Iterable[list]) -> None:
    updates = []
    for row, vector in zip(rows, vectors):
        if vector:
            updates.append((json.dumps(vector), row["event_id"]))
    if not updates:
        return
    conn.executemany("UPDATE events SET vector_embedding = ? WHERE event_id = ?", updates)
    conn.commit()


def _backfill_rows(rows: list[dict], *, priority: int) -> None:
    pending = [row for row in rows if not row.get("vector_embedding") and _event_text(row)]
    if not pending:
        return
    # Interactive recall embeds only a bounded slice to avoid making the first
    # query wait for the entire historical index.
    pending = pending[:INLINE_BACKFILL if priority == Priority.INTERACTIVE else INDEX_BATCH]
    try:
        vectors = embed_texts([_event_text(row) for row in pending])
        _store_embeddings(pending, vectors)
        for row, vector in zip(pending, vectors):
            if vector:
                row["vector_embedding"] = json.dumps(vector)
    except Exception as exc:
        print(f"[rag] embedding backfill skipped: {exc}")


def search_event_rag(
    query: str,
    *,
    start_ts: float | None = None,
    end_ts: float | None = None,
    limit: int = 20,
) -> tuple[list[str], int] | None:
    """Return hybrid text and visual event matches."""

    if not get_capture_settings()["rag_enabled"]:
        return None

    rows = _candidate_rows(start_ts, end_ts)
    if not rows:
        return [], 0

    text_scores = {}
    query_vector = None
    try:
        query_vector = embed_text(query)
    except Exception as exc:
        print(f"[rag] query embedding unavailable: {exc}")
    if query_vector:
        _backfill_rows(rows, priority=Priority.INTERACTIVE)
    for row in rows:
        vector = _decode_vector(row.get("vector_embedding"))
        text = _event_text(row)
        keyword = _keyword_score(query, text)
        semantic = _cosine(query_vector, vector) if query_vector and vector else 0.0
        if semantic or keyword:
            text_scores[row["event_id"]] = (semantic * 0.78) + (keyword * 0.22)
    # PARKED: CLIP visual branch — only if image embeddings were stored as clip:*.
    # See image_embeddings.py / image_embeddings_enabled (default off).
    image_scores = {}
    image_rows = [
        row for row in rows
        if str(row.get("image_embedding_model") or "").startswith("clip:")
    ]
    if image_rows:
        try:
            image_query = embed_image_text(query)
            if image_query:
                for row in image_rows:
                    vector = _decode_vector(row.get("image_embedding"))
                    if vector:
                        image_scores[row["event_id"]] = max(0.0, _cosine(image_query, vector))
        except Exception as exc:
            print(f"[rag] image search unavailable: {exc}")

    scored = []
    row_map = {row["event_id"]: row for row in rows}
    now = time.time()
    for event_id in set(text_scores) | set(image_scores):
        text_score = text_scores.get(event_id)
        image_score = image_scores.get(event_id)
        if text_score is not None and image_score is not None:
            score = (max(0.0, text_score) * 0.65) + (image_score * 0.35)
        elif text_score is not None:
            score = text_score
        else:
            score = image_score or 0.0
        age_days = max(0.0, (now - float(row_map[event_id]["timestamp"])) / 86400.0)
        recency = math.exp(-math.log(2) * age_days / 30.0)
        # Recency breaks near-ties without overpowering semantically older hits.
        score = (score * 0.96) + (recency * 0.04)
        scored.append((score, row_map[event_id]))
    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    selected = scored[: max(1, min(limit, 20))]
    result = []
    for score, row in selected:
        timestamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["timestamp"]))
        parts = [
            f"time: {timestamp}",
            f"event_type: {row['event_type']}",
            f"relevance: {score:.2f}",
        ]
        for key, label in (
            ("process_name", "process"),
            ("current_window_title", "window"),
            ("active_url", "url"),
            ("summary", "summary"),
            ("vision_activity", "vision_activity"),
            ("vision_ocr_text", "vision_ocr_text"),
            ("interest_reason", "interest_reason"),
        ):
            value = row.get(key)
            if value:
                parts.append(f"{label}: {value}")
        filename = resolve_screenshot_filename(row["timestamp"], row.get("screenshot_filename"))
        if filename:
            parts.append(f"screenshot_source: {filename}")
        parts.append(f"event_id: {row['event_id']}")
        result.append("\n".join(parts))
    return result, len(scored)


def _index_pending_once() -> None:
    rows = _candidate_rows(None, None)
    pending = [row for row in rows if not row.get("vector_embedding")]
    if pending:
        _backfill_rows(pending[:INDEX_BATCH], priority=Priority.BACKGROUND)


def _index_loop() -> None:
    while not _stop.wait(INDEX_INTERVAL_SECONDS):
        if not get_capture_settings()["rag_enabled"]:
            break
        try:
            _index_pending_once()
        except Exception as exc:
            print(f"[rag] indexer error: {exc}")


def start_event_indexer() -> threading.Thread | None:
    """Start one best-effort background event embedding worker."""

    global _thread
    with _start_lock:
        if not get_capture_settings()["rag_enabled"]:
            return None
        if _thread and _thread.is_alive():
            return _thread
        _stop.clear()
        _thread = threading.Thread(target=_index_loop, daemon=True, name="rag-event-indexer")
        _thread.start()
        return _thread


def stop_event_indexer(*, wait: bool = False) -> None:
    """Stop background embedding promptly when event RAG is disabled."""
    global _thread
    _stop.set()
    thread = _thread
    if wait and thread and thread is not threading.current_thread():
        thread.join(timeout=1.0)
    if not thread or not thread.is_alive():
        _thread = None
