import json
import re
import time

from agent.helpers.detect_recency import detect_recency_hint
from agent.helpers.keywords import STOPWORDS, content_keywords, keywords_from_query
from agent.helpers.time_resolver import resolve_temporal_range
from agent.prefetch.topic_search import cosine_similarity
from core.screenshot_search import resolve_screenshot_filename

# Ensure events / events_fts tables exist before we touch the index
from core.storage import conn

MAX_RESULTS = 5
DEFAULT_LOOKBACK_DAYS = 7

SESSION_SCORE_THRESHOLD = 0.35

ARTIFACT_PATTERNS = [
  ("clipboard", re.compile(r'\b(cop(y|ied)|clipboard|clipped)\b', re.I)),
  ("paste",     re.compile(r'\b(past(e|ed)|pasting)\b', re.I)),
  ("url",       re.compile(r'\b(link|url|website|article|book|page|site|read|reading|visit|browse|open)\b', re.I)),
  ("screen",    re.compile(r'\b(screenshot|screen[- ]?shot|screen capture|captured frame|saw|screen|viewing|looked at|displayed|showing|visible)\b', re.I)),
]

ARTIFACT_ROUTES = {
"url":       {"fields": ["active_url", "current_window_title", "summary"],
                  "event_types": None, "app_filter": None},
"screen":    {"fields": ["vision_ocr_text", "vision_activity", "summary"],
                  "event_types": ["screenshot_analysis"], "app_filter": None},
"paste":     {"fields": ["payload", "summary"],
                  "event_types": ["paste"], "app_filter": None},
"clipboard": {"fields": ["payload", "summary"],
                  "event_types": ["clipboard_change"], "app_filter": None},
"generic":   {"fields": ["summary", "vision_activity", "current_window_title", "vision_ocr_text", "active_url"],
                  "event_types": None, "app_filter": None}
}

URL_RE = re.compile(
    r'https?://\S+|www\.\S+|\b\w[\w-]*\.(com|org|io|net|edu|gov|co|ai|dev|in|uk|au|ca|de|fr|it|jp|kr|mx|nl|nz|ru|sa|se|ch|tw|hk|us)\b',
    re.I
)

def detect_artifact_type(query: str) -> str:
    q_lower = query.lower()
    for artifact_type, pattern in ARTIFACT_PATTERNS:
        if pattern.search(q_lower):
            return artifact_type
    return "generic"

def extract_urls_from_entities(entities_json: str) -> list[str]:
    if not entities_json:
        return []
    try:
        entities = json.loads(entities_json)
    except Exception as e:
        print(f"Error loading entities: {e}")
        return []

    return [e for e in entities if isinstance(e, str) and URL_RE.search(e)]


# Track A: Session-level search for URLs
def search_sessions_for_url(query:str,  query_vec, keywords: list[str], temporal_range=None, recency_hint=None) -> list | None:

    if temporal_range:
        time_filter = (
            f"window_start >= {temporal_range.start_ts} "
            f"AND window_start < {temporal_range.end_ts}"
        )
    elif recency_hint == "window":
        time_filter = f"window_start >= {time.time() - 7 * 86400}"
    else:
        time_filter = "1=1"

    sql = f"""
    SELECT window_start, summary, active_task, entities, summary_embedding
    FROM sessions
    WHERE {time_filter}
      AND summary IS NOT NULL AND summary != ''
    """
    try:
        rows = conn.execute(sql).fetchall()
    except Exception:
        return None
    if not rows:
        return None


    # Only keep sessions that have at least one URL-like entity
    candidates = [
        (ws, summary, active_task, entities, emb_json)
        for (ws, summary, active_task, entities, emb_json) in rows
        if extract_urls_from_entities(entities)
    ]
    if not candidates:
        return None

    recency_weight = 0.4 if recency_hint == "soft" else 0.2

    timestamps = [window_start for window_start, *_ in candidates]
    newest_ts = max(timestamps)
    oldest_ts = min(timestamps)
    ts_range = max(newest_ts - oldest_ts, 1)

    scores = []
    for (window_start, summary, active_task, entities, summary_embedding) in candidates:
        urls = extract_urls_from_entities(entities)
        recency_score = (window_start - oldest_ts) / ts_range
        score = 0.0
        if query_vec and summary_embedding:
            score += cosine_similarity(query_vec, json.loads(summary_embedding))
        else:

            # Fallback to keyword-based similarity
            combined_keywords = f"{summary or ''} {entities or ''} {active_task or ''}".lower()
            score += sum(1 for kw in keywords if kw in combined_keywords) / max(len(keywords), 1)

        score+= recency_weight * recency_score
        scores.append((score, window_start, summary, active_task, urls))

    if not scores:
        return None
    scores.sort(key=lambda x: x[0], reverse=True)
    top_k = [score for score in scores[:MAX_RESULTS] if score[0] >= SESSION_SCORE_THRESHOLD]
    return top_k if top_k else None



# Track B: Event-level search for URLs
def _sanitize_for_fts(keyword: str) -> str:
    """Strip FTS5 special chars, wrap in quotes for safe exact-token matching."""
    clean = re.sub(r'[^\w]', '', keyword)
    return f'"{clean}"' if clean else ''


def _extract_payload_text(payload: str) -> str | None:
    """Return displayable text from a payload field.

    Payload may be a plain string or a JSON object like
    {"pasted_content": "hello"} or {"pasted_content": null}.
    Returns None when there is no real content to show.
    """
    if not payload:
        return None
    try:
        obj = json.loads(payload)
        if isinstance(obj, dict):

            # grab first non-null string value
            for v in obj.values():
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return None  # all values were null / empty
        if isinstance(obj, str):
            return obj.strip() or None
    except (json.JSONDecodeError, TypeError):
        pass
    return payload.strip() or None

def search_events_for_url(keywords: list[str], temporal_range=None, recency_hint=None) -> list | None:
    if temporal_range:
        time_filter = (
            f"timestamp >= {temporal_range.start_ts} "
            f"AND timestamp < {temporal_range.end_ts}"
        )
    elif recency_hint == "window":
        time_filter = f"timestamp >= {time.time() - 7 * 86400}"
    else:
        time_filter = f"timestamp >= {time.time() - DEFAULT_LOOKBACK_DAYS * 86400}"

    fts_keywords = [_sanitize_for_fts(kw) for kw in content_keywords(keywords)]
    fts_keywords = [t for t in fts_keywords if t]

    if not fts_keywords:
        return None

    fts_query = " OR ".join(fts_keywords)


    # Use parameterized MATCH — handles any remaining edge cases safely
    sql = f"""
        SELECT e.timestamp, e.active_url, e.current_window_title, e.summary, events_fts.rank
        FROM events_fts
        JOIN events e ON events_fts.rowid = e.rowid
        WHERE events_fts MATCH ?
          AND {time_filter}
          AND e.active_url IS NOT NULL AND e.active_url != ''
          AND e.interesting = 1
        ORDER BY events_fts.rank
        LIMIT 20
    """

    try:
        rows = conn.execute(sql, (fts_query,)).fetchall()
    except Exception:
        return []

    if not rows:
        return []


    # fts.rank is negative — more negative = more relevant
    timestamps = [r[0] for r in rows]
    newest_ts  = max(timestamps)
    oldest_ts  = min(timestamps)
    ts_range   = max(newest_ts - oldest_ts, 1.0)
    recency_weight = 0.4 if recency_hint == "soft" else 0.2

    scores = []
    for (ts, url, window_title, summary, rank) in rows:
        relevance = -rank  # flip: now positive, higher -> more relevant
        recency   = (ts - oldest_ts) / ts_range  # 0.0 oldest -> 1.0 newest
        score     = relevance + recency_weight * recency
        scores.append((score, ts, url, window_title, summary))

    if not scores:
        return []

    scores.sort(key=lambda x: x[0], reverse=True)
    return scores[:MAX_RESULTS]

def search_events_for_artifact(
    artifact_type: str,
    keywords: list[str],
    temporal_range=None,
    recency_hint=None,
) -> list:
    """Event-level FTS5 search for clipboard, paste, screen, and generic artifacts.

    For pure-recency queries (no keywords), falls back to a time-sorted scan
    so "what did I last copy" still works without FTS.
    """
    route = ARTIFACT_ROUTES.get(artifact_type, ARTIFACT_ROUTES["generic"])
    event_type = route["event_types"][0] if route["event_types"] else None

    if temporal_range:
        time_filter = (
            f"e.timestamp >= {temporal_range.start_ts} "
            f"AND e.timestamp < {temporal_range.end_ts}"
        )
    elif recency_hint == "window":
        time_filter = f"e.timestamp >= {time.time() - 7 * 86400}"
    else:
        time_filter = f"e.timestamp >= {time.time() - DEFAULT_LOOKBACK_DAYS * 86400}"

    event_type_filter = f"AND e.event_type = '{event_type}'" if event_type else ""

    fts_keywords = [_sanitize_for_fts(kw) for kw in content_keywords(keywords)]
    fts_keywords = [t for t in fts_keywords if t]

    recency_weight = 0.4 if recency_hint in ("soft", "window") else 0.2


    # --- clipboard / paste: primary field is payload ---
    if artifact_type in ("clipboard", "paste"):
        if fts_keywords:
            fts_query = " OR ".join(fts_keywords)
            sql = f"""
                SELECT e.timestamp, e.payload, e.current_window_title, e.process_name, events_fts.rank
                FROM events_fts
                JOIN events e ON events_fts.rowid = e.rowid
                WHERE events_fts MATCH ?
                  AND {time_filter}
                  {event_type_filter}
                  AND e.payload IS NOT NULL AND e.payload != ''
                ORDER BY events_fts.rank
                LIMIT 20
            """
            try:
                rows = conn.execute(sql, (fts_query,)).fetchall()
            except Exception:
                rows = []
        else:
            rows = []


        # Pure-recency fallback — only when there were no keywords to begin with
        if not rows and not fts_keywords:
            sql = f"""
                SELECT e.timestamp, e.payload, e.current_window_title, e.process_name, 0 AS rank
                FROM events e
                WHERE {time_filter}
                  {event_type_filter}
                  AND e.payload IS NOT NULL AND e.payload != ''
                ORDER BY e.timestamp DESC
                LIMIT 20
            """
            try:
                rows = conn.execute(sql).fetchall()
            except Exception:
                return []

        if not rows:
            return []

        timestamps = [r[0] for r in rows]
        newest_ts, oldest_ts = max(timestamps), min(timestamps)
        ts_range = max(newest_ts - oldest_ts, 1.0)

        scored = []
        for (ts, payload, window_title, process_name, rank) in rows:
            text = _extract_payload_text(payload)
            if not text:
                continue
            relevance = -rank
            recency   = (ts - oldest_ts) / ts_range
            score     = relevance + recency_weight * recency
            scored.append((score, ts, text, window_title, process_name))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:MAX_RESULTS]




    # --- screen: primary field is vision_ocr_text ---
    # Do NOT filter by event_type — vision data is attached to whichever event
    # happened to be nearest when the screenshot was captured.
    if artifact_type == "screen":
        if fts_keywords:
            fts_query = " OR ".join(fts_keywords)
            sql = f"""
                SELECT e.timestamp, e.vision_ocr_text, e.vision_activity,
                       e.current_window_title, e.process_name, e.summary,
                       e.screenshot_filename, events_fts.rank
                FROM events_fts
                JOIN events e ON events_fts.rowid = e.rowid
                WHERE events_fts MATCH ?
                  AND {time_filter}
                  AND (e.event_type = 'screenshot_analysis'
                       OR e.vision_ocr_text IS NOT NULL
                       OR e.screenshot_filename IS NOT NULL)
                ORDER BY events_fts.rank
                LIMIT 20
            """
            try:
                rows = conn.execute(sql, (fts_query,)).fetchall()
            except Exception:
                rows = []
        else:
            rows = []


        # Keyword misses should stay empty instead of injecting unrelated recent frames.
        if not rows and not fts_keywords:
            sql = f"""
                SELECT e.timestamp, e.vision_ocr_text, e.vision_activity,
                       e.current_window_title, e.process_name, e.summary,
                       e.screenshot_filename, 0 AS rank
                FROM events e
                WHERE {time_filter}
                  AND (e.event_type = 'screenshot_analysis'
                       OR e.vision_ocr_text IS NOT NULL
                       OR e.screenshot_filename IS NOT NULL)
                ORDER BY e.timestamp DESC
                LIMIT 20
            """
            try:
                rows = conn.execute(sql).fetchall()
            except Exception:
                return []

        if not rows:
            return []

        timestamps = [r[0] for r in rows]
        newest_ts, oldest_ts = max(timestamps), min(timestamps)
        ts_range = max(newest_ts - oldest_ts, 1.0)

        scored = []
        for (ts, ocr_text, vision_activity, window_title, process_name, summary, screenshot_filename, rank) in rows:
            relevance = -rank
            recency   = (ts - oldest_ts) / ts_range
            score     = relevance + recency_weight * recency
            scored.append((score, ts, ocr_text, vision_activity, window_title, process_name, summary, screenshot_filename))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:MAX_RESULTS]



    # --- generic: broad search across all text fields ---
    # Exclude pure noise event types, but keep them if vision data is attached.
    NOISE_FILTER = (
        "(e.event_type NOT IN ('typing_burst', 'deviation', 'context_change') "
        "OR e.vision_ocr_text IS NOT NULL)"
    )

    if fts_keywords:
        fts_query = " OR ".join(fts_keywords)
        sql = f"""
            SELECT e.timestamp, e.summary, e.vision_activity,
                   e.current_window_title, e.active_url, events_fts.rank
            FROM events_fts
            JOIN events e ON events_fts.rowid = e.rowid
            WHERE events_fts MATCH ?
              AND {time_filter}
              AND e.interesting = 1
              AND {NOISE_FILTER}
            ORDER BY events_fts.rank
            LIMIT 20
        """
        try:
            rows = conn.execute(sql, (fts_query,)).fetchall()
        except Exception:
            rows = []
    else:
        rows = []


    if not rows and not fts_keywords:
        sql = f"""
            SELECT e.timestamp, e.summary, e.vision_activity,
                   e.current_window_title, e.active_url, 0 AS rank
            FROM events e
            WHERE {time_filter}
              AND e.interesting = 1
              AND {NOISE_FILTER}
            ORDER BY e.timestamp DESC
            LIMIT 20
        """
        try:
            rows = conn.execute(sql).fetchall()
        except Exception:
            return []

    if not rows:
        return []

    timestamps = [r[0] for r in rows]
    newest_ts, oldest_ts = max(timestamps), min(timestamps)
    ts_range = max(newest_ts - oldest_ts, 1.0)

    scored = []
    for (ts, summary, vision_activity, window_title, active_url, rank) in rows:
        relevance = -rank
        recency   = (ts - oldest_ts) / ts_range
        score     = relevance + recency_weight * recency
        scored.append((score, ts, summary, vision_activity, window_title, active_url))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:MAX_RESULTS]


def _format_clipboard_results(artifact_type: str, results: list) -> str:
    if not results:
        return (
            f"specific_recall: no {artifact_type} events found in the last {DEFAULT_LOOKBACK_DAYS} days "
            f"matching the query."
        )
    label = "clipboard copies" if artifact_type == "clipboard" else "paste events"
    parts = [f"[{label} — last {DEFAULT_LOOKBACK_DAYS} days]"]
    for item in results:
        score, ts, payload, window_title, process_name = item
        ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        block  = [f"time: {ts_str}"]
        if process_name:
            block.append(f"app: {process_name}")
        if window_title:
            block.append(f"window: {window_title}")
        if payload:
            display = payload if len(payload) <= 300 else payload[:300] + f"… [{len(payload)} chars total]"
            block.append(f"content: {display}")
        parts.append("\n".join(block))
    return "\n\n---\n".join(parts)


def _format_screen_results(results: list) -> str:
    if not results:
        return (
            f"specific_recall: no screen/vision events found in the last {DEFAULT_LOOKBACK_DAYS} days "
            f"matching the query."
        )
    parts = ["[screen activity — last 7 days]"]
    for item in results:
        score, ts, ocr_text, vision_activity, window_title, process_name, summary, screenshot_filename = item
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        block  = [f"time: {ts_str}"]
        if process_name:
            block.append(f"app: {process_name}")
        if window_title:
            block.append(f"window: {window_title}")
        if vision_activity:
            block.append(f"activity: {vision_activity}")
        if summary:
            block.append(f"summary: {summary}")
        if ocr_text:
            display = ocr_text if len(ocr_text) <= 400 else ocr_text[:400] + f"… [{len(ocr_text)} chars total]"
            block.append(f"ocr_text: {display}")
        source = resolve_screenshot_filename(ts, screenshot_filename)
        if source:
            block.append(f"screenshot_source: {source}")
        parts.append("\n".join(block))
    return "\n\n---\n".join(parts)


def _exact_screenshot_evidence(temporal_range) -> str | None:
    midpoint = (temporal_range.start_ts + temporal_range.end_ts) / 2
    row = conn.execute(
        """SELECT timestamp, process_name, current_window_title, summary,
                  vision_ocr_text, vision_activity, screenshot_filename
           FROM events
           WHERE event_type = 'screenshot_analysis'
             AND timestamp >= ? AND timestamp < ?
           ORDER BY ABS(timestamp - ?)
           LIMIT 1""",
        (temporal_range.start_ts, temporal_range.end_ts, midpoint),
    ).fetchone()
    if not row:
        return None
    ts, process_name, window_title, summary, ocr_text, vision_activity, screenshot_filename = row
    parts = [
        "[exact screenshot evidence — do not merge with nearby events]",
        f"time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}",
        f"match_delta_seconds: {abs(ts - midpoint):.3f}",
    ]
    if process_name:
        parts.append(f"app: {process_name}")
    if window_title:
        parts.append(f"window: {window_title}")
    if summary:
        parts.append(f"summary: {summary}")
    if vision_activity:
        parts.append(f"activity: {vision_activity}")
    if ocr_text:
        display = ocr_text if len(ocr_text) <= 1200 else ocr_text[:1200] + f"… [{len(ocr_text)} chars total]"
        parts.append(f"ocr_text: {display}")
    source = resolve_screenshot_filename(ts, screenshot_filename)
    if source:
        parts.append(f"screenshot_source: {source}")
    return "\n".join(parts)


def _format_generic_results(results: list) -> str:
    if not results:
        return (
            f"specific_recall: no relevant events found in the last {DEFAULT_LOOKBACK_DAYS} days."
        )
    parts = ["[activity events — last 7 days]"]
    for item in results:
        score, ts, summary, vision_activity, window_title, active_url = item
        ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        block  = [f"time: {ts_str}"]
        if window_title:
            block.append(f"window: {window_title}")
        if active_url:
            block.append(f"url: {active_url}")
        if vision_activity:
            block.append(f"activity: {vision_activity}")
        if summary:
            block.append(f"summary: {summary}")
        parts.append("\n".join(block))
    return "\n\n---\n".join(parts)


def _format_url_results(session_results, event_results) -> str:
    parts = []
    if session_results:
        parts.append("[URLs from activity history — up to 90 days]")
        for score, ws, summary, active_task, urls in session_results:
            ts    = time.strftime("%Y-%m-%d %H:%M", time.localtime(ws))
            block = [f"time: {ts}"]
            if summary:
                block.append(f"summary: {summary}")
            if active_task:
                block.append(f"active_task: {active_task}")
            block.append("urls: " + ", ".join(urls))
            parts.append("\n".join(block))
    if event_results:
        parts.append("[URLs from recent events — last 7 days]")
        for score, ts, url, window_title, summary in event_results:
            ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            block  = [f"time: {ts_str}"]
            if window_title:
                block.append(f"window: {window_title}")
            block.append(f"url: {url}")
            if summary:
                block.append(f"summary: {summary}")
            parts.append("\n".join(block))
    if not parts:
        return (
            "specific_recall: no URLs found matching the query.\n"
            "If the activity is older than 7 days, the URL may only exist in session "
            "summaries — try topic_search for broader context."
        )
    return "\n\n---\n".join(parts)

def specific_recall(query: str, temporal_range=None, q_vec: list | None = None) -> str:
    from concurrent.futures import ThreadPoolExecutor

    artifact_type = detect_artifact_type(query)
    keywords      = keywords_from_query(query)
    recency_hint  = detect_recency_hint(query)

    if artifact_type == "url":
        with ThreadPoolExecutor(max_workers=2) as executor:
            session_f = executor.submit(
                search_sessions_for_url, query, q_vec, keywords, temporal_range, recency_hint
            )
            event_f = executor.submit(
                search_events_for_url, keywords, temporal_range, recency_hint
            )
            session_results = session_f.result()
            event_results   = event_f.result()

        return _format_url_results(session_results, event_results)

    if artifact_type in ("clipboard", "paste"):
        results = search_events_for_artifact(artifact_type, keywords, temporal_range, recency_hint)
        return _format_clipboard_results(artifact_type, results)

    if artifact_type == "screen":
        if temporal_range and temporal_range.granularity == "instant":
            exact = _exact_screenshot_evidence(temporal_range)
            if exact:
                return exact
        results = search_events_for_artifact("screen", keywords, temporal_range, recency_hint)
        return _format_screen_results(results)


    # generic fallback
    results = search_events_for_artifact("generic", keywords, temporal_range, recency_hint)
    return _format_generic_results(results)


if __name__ == "__main__":
    while True:
        query = input("Query: ").strip()
        if not query:
            break
        tr  = resolve_temporal_range(query)
        art = detect_artifact_type(query)
        kws = keywords_from_query(query)
        rec = detect_recency_hint(query)

        if tr:
            print(f"  time scope: {tr.phrase!r} [{tr.granularity}]")
        print(f"  artifact:    {art}")
        print(f"  keywords:    {kws}")
        print(f"  recency:     {rec}")
        print()
        print(specific_recall(query, temporal_range=tr))
        print()
