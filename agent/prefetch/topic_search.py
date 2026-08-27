import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.helpers.keywords import content_keywords, keywords_from_query
from core.local_embeddings import embed_text
from core.rag import search_event_rag
from core.storage import conn as _conn

MAX_RESULTS = 5

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x*y for x, y in zip(a, b))

    norm_a = math.sqrt(sum(x*x for x in a))
    norm_b = math.sqrt(sum(y*y for y in b))

    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

def date_filter(temporal_range) -> str:
    base__query = "summary is NOT NULL and summary != ''"
    if temporal_range is None:
        return base__query
    return (
        f"window_start >= {temporal_range.start_ts} "
        f"AND window_start <= {temporal_range.end_ts} "
        f"AND {base__query}"
    )

def entity_boost(keywords: list[str], entities: list[str]) -> float:
    if not entities or not keywords:
        return 0.0
    try:
        ents = [e.lower() for e in json.loads(entities)]
    except Exception:
        return 0.0
    hits = sum(1 for kw in keywords if any(kw in e for e in ents))
    return min(hits * 0.05, 0.15)

def topic_search(query: str, q_vec: list | None, temporal_range=None) -> str:

    keywords = keywords_from_query(query)
    if q_vec is None:
        q_vec = embed_text(query)

    date_filter_query = date_filter(temporal_range)
    sql = f"""
    SELECT summary_id, window_start, summary, active_task, entities, summary_embedding
    FROM sessions
    WHERE {date_filter_query}
    """

    try:
        rows = _conn.execute(sql).fetchall()
    except Exception as e:
        return f"Error querying sessions: {e}"

    
    if not rows:
        event_result = search_event_rag(
            query,
            start_ts=temporal_range.start_ts if temporal_range else None,
            end_ts=temporal_range.end_ts if temporal_range else None,
            limit=8,
        )
        if event_result:
            matches, total = event_result
            if matches:
                return (
                    f"[event-level activity fallback — showing {len(matches)} of {total} matches]\n"
                    + "\n\n---\n\n".join(matches)
                )
        return "No activity events or session summaries found."

    results = []
    score = 0.0

    for (summary_id, window_start, summary, active_task, entities, summary_embedding) in rows:
        if summary_embedding:
            score = cosine_similarity(q_vec, json.loads(summary_embedding))
        
        if entities:
            score+= entity_boost(keywords, entities)

        results.append((score,window_start, summary, active_task, entities))


    results.sort(key=lambda x: x[0], reverse=True)
    top_results = results[:MAX_RESULTS]
    total = len(results)

    response = []
    for score, window_start, summary, active_task, entities in top_results:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(window_start))
        parts = [f"timestamp: {timestamp}", f"summary: {summary}"]
        if active_task:
            parts.append(f"active_task: {active_task}")
        if entities:
            parts.append(f"entities: {entities}")
        response.append(" ".join(p for p in parts if p))

    
    shown_results = len(response)
    if total > shown_results:
        header = f"Showing {shown_results} of {total} results - for more specific or finer detail call search_events"
    else:
        header = f"Showing {total} results"

    result = header + "\n" + "\n".join(response)
    return result

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from agent.helpers.time_resolver import resolve_temporal_range

    while True:
        query = input("Enter a query: ").strip()
        if not query:
            break
        temporal_range = resolve_temporal_range(query)
        result = topic_search(query, q_vec=None, temporal_range=temporal_range)
        print(result)
