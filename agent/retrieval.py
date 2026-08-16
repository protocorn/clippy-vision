import json
import math
import re
import time

from agent.helpers.time_resolver import resolve_temporal_range
from core.llm_gateway import Priority, gateway
from core.local_embeddings import embed_text, embed_texts
from core.rag import search_event_rag
from core.storage import conn

MAX_RESULT_ROWS = 20
MAX_RESULT_CHARS = 4000
_HEAVY_COLS = {"payload", "vector_embedding", "summary_embedding"}

MODEL = "qwen3:8b"

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "sql_query": {"type": "string"},
    },
    "required": ["sql_query"]
}





# ─────────────────────────────────────────────────────────────
# Two focused prompts — model only sees the table it will use
# ─────────────────────────────────────────────────────────────
_SESSIONS_PROMPT = """
You generate SQLite SELECT queries against a sessions table.

sessions (
    summary_id   TEXT PRIMARY KEY,
    session_id   TEXT,
    window_start REAL,
    window_end   REAL,
    summary      TEXT,
    active_task  TEXT,
    entities     TEXT,
    event_count  INTEGER
)

Date helpers (always use 'localtime'):
  Today / Tonight / This evening / This morning/ This afternoon:
                 window_start >= CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day')) AS INTEGER)
                 AND window_start <= <TS>
  Yesterday    : window_start >= CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day','-1 day')) AS INTEGER)
                 AND window_start <  CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day')) AS INTEGER)
  This week    : window_start >= CAST(strftime('%s', date(<TS>,'unixepoch','localtime','weekday 1','-7 days')) AS INTEGER)
  Specific date: window_start >= CAST(strftime('%s', 'YYYY-MM-DD') AS INTEGER)
                 AND window_start <  CAST(strftime('%s', date('YYYY-MM-DD', '+1 day')) AS INTEGER)
  N days ago   : window_start >= CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day','-N days')) AS INTEGER)
                 AND window_start <  CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day','-(N-1) days')) AS INTEGER)

CRITICAL date rule: When the question mentions a specific date (e.g. "June 23", "2026-06-23", "last Tuesday") or
references a prior tool result that contained a specific timestamp — compute the exact calendar date and use the
Specific date helper with the literal 'YYYY-MM-DD' string. NEVER use a relative helper (yesterday / -1 day) as a
substitute for a date that is 2 or more days ago — you will search the wrong day.

Rules:
- SELECT summary, active_task, entities, datetime(window_start,'unixepoch','localtime') as time
- ALWAYS include: AND summary IS NOT NULL AND summary != '' — to skip unprocessed sessions.
- Always add a time-window WHERE clause based on the question.
- For count/how-many questions, search by topic first: WHERE summary LIKE '%<topic>%' OR active_task LIKE '%<topic>%' OR entities LIKE '%<topic>%', then use COUNT(*).
- ORDER BY window_start ASC. LIMIT 20.
- Output only valid SQLite SELECT SQL in JSON.
""".strip()

_EVENTS_PROMPT = """
You generate SQLite SELECT queries against an events table.

events (
    timestamp             REAL,
    event_type            TEXT,
    process_name          TEXT,
    current_window_title  TEXT,
    active_url            TEXT,
    summary               TEXT,
    payload               TEXT,
    interesting           INTEGER,
    interest_score        REAL,
    interest_reason       TEXT,
    vision_ocr_text       TEXT,
    vision_activity       TEXT,
    vision_suggested_action TEXT
)

Date helpers (always use 'localtime'):
  Today / Tonight / This evening / This morning:
                 timestamp >= CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day')) AS INTEGER)
                 AND timestamp <= <TS>
  Yesterday    : timestamp >= CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day','-1 day')) AS INTEGER)
                 AND timestamp <  CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day')) AS INTEGER)
  Specific date: timestamp >= CAST(strftime('%s', 'YYYY-MM-DD') AS INTEGER)
                 AND timestamp <  CAST(strftime('%s', date('YYYY-MM-DD', '+1 day')) AS INTEGER)
  N days ago   : timestamp >= CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day','-N days')) AS INTEGER)
                 AND timestamp <  CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day','-(N-1) days')) AS INTEGER)

CRITICAL date rule: When the question mentions a specific date (e.g. "June 23", "2026-06-23", "last Tuesday") or
references a prior tool result that contained a specific timestamp — compute the exact calendar date and use the
Specific date helper with the literal 'YYYY-MM-DD' string. NEVER use a relative helper (yesterday / -1 day) as a
substitute for a date that is 2 or more days ago — you will search the wrong day.

Rules:
- SELECT only columns needed to answer the question — never SELECT *.
- For keyword searches use: summary, current_window_title, active_url, vision_ocr_text, interest_reason.
- For any question about what the user was doing, reading, working on, or looking at — always include
  vision_activity and vision_ocr_text in the SELECT list alongside summary (they may be NULL but include them).
- Use OR between search conditions, not AND.
  Only filter by event_type when the question explicitly asks for a specific event kind (e.g. "what did I paste",
  "what URLs did I visit"). Invented event_type values will return zero rows — always use LIKE on text columns instead.
- Prefer interesting=1 rows unless the question requires all events.
- For URL, browser, or app-switch questions (e.g. "what sites did I visit", "what did I open", "what link") —
  do NOT filter by interesting — include all events so brief context switches are not missed.
- LIMIT 20.
- Output only valid SQLite SELECT SQL in JSON.
""".strip()





# ─────────────────────────────────────────────────────────────
# Safety
# ─────────────────────────────────────────────────────────────
_BLOCKED = re.compile(
    r'\b(DROP|DELETE|UPDATE|INSERT|ALTER|CREATE|ATTACH|DETACH|PRAGMA|REPLACE|TRUNCATE)\b',
    re.IGNORECASE,
)

def _is_safe(sql: str) -> bool:
    return sql.strip().upper().startswith("SELECT") and not _BLOCKED.search(sql)






# ─────────────────────────────────────────────────────────────
# Core helpers
# ─────────────────────────────────────────────────────────────
def _generate_sql(system_prompt: str, user_content: str) -> str:
    body = gateway.chat(
        [{"role": "system", "content": system_prompt},
         {"role": "user",   "content": user_content}],
        model=MODEL, format=OUTPUT_SCHEMA, think=False,
        options={"temperature": 0}, priority=Priority.INTERACTIVE,
    )
    content = body["message"]["content"]
    parsed = json.loads(content) if isinstance(content, str) else content
    return parsed.get("sql_query", "").strip()


def _run_sql(sql: str) -> tuple[list, int]:
    """Execute sql, return (rows_as_text_list, total_matched_count)."""
    cur = conn.execute(sql)
    all_rows = cur.fetchall()
    total = len(all_rows)
    result_text = []
    for row in all_rows[:MAX_RESULT_ROWS]:
        row_text = []
        for i, col in enumerate(cur.description):
            if col[0] not in _HEAVY_COLS:
                val = row[i]
                if isinstance(val, str):
                    val = val.encode("utf-8", errors="replace").decode("utf-8")
                row_text.append(f"{col[0]}: {val}")
        result_text.append("\n".join(row_text))
    return result_text, total






# ─────────────────────────────────────────────────────────────
# Cosine similarity helpers (Fix 3 — semantic session search)
# ─────────────────────────────────────────────────────────────
def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _semantic_sessions(question: str, date_sql_filter: str, limit: int = MAX_RESULT_ROWS) -> tuple[list, int]:
    """Embed the question and re-rank sessions by cosine similarity.

    date_sql_filter is a WHERE fragment (without the WHERE keyword) that
    scopes the candidate set to the relevant time window, e.g.:
      "window_start >= X AND window_start < Y AND summary IS NOT NULL AND summary != ''"

    Returns (rows_as_text_list, total_candidates_count).
    Rows that have no embedding yet are scored 0 (still returned if nothing better exists).
    """
    q_vec = embed_text(question)

    candidate_sql = f"""
        SELECT summary_id, session_id, window_start, window_end,
               summary, active_task, entities, event_count, summary_embedding
        FROM sessions
        WHERE {date_sql_filter}
    """
    rows = conn.execute(candidate_sql).fetchall()
    total = len(rows)
    if not rows:
        return [], 0

    scored = []
    unembedded_ids = []
    for row in rows:
        (summary_id, session_id, ws, we, summary, active_task,
         entities, event_count, emb_json) = row
        if emb_json:
            vec = json.loads(emb_json)
            score = _cosine(q_vec, vec)
        else:
            score = 0.0
            unembedded_ids.append((summary_id, summary))
        scored.append((score, summary_id, ws, summary, active_task, entities))


    # Back-fill missing embeddings in the background (non-blocking)
    if unembedded_ids:
        _backfill_session_embeddings(unembedded_ids)

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    result_text = []
    for score, summary_id, ws, summary, active_task, entities in top:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(ws))
        parts = [f"time: {ts}", f"summary: {summary}"]
        if active_task:
            parts.append(f"active_task: {active_task}")
        if entities:
            parts.append(f"entities: {entities}")
        result_text.append("\n".join(parts))

    return result_text, total


def _backfill_session_embeddings(pairs: list[tuple[str, str]]) -> None:
    """Embed session summaries that were stored before this feature existed.
    Runs inline (called from the query path) but only processes unembedded rows."""
    texts = [summary for _, summary in pairs if summary]
    if not texts:
        return
    try:
        vecs = embed_texts(texts)
        for (summary_id, _), vec in zip(pairs, vecs):
            conn.execute(
                "UPDATE sessions SET summary_embedding = ? WHERE summary_id = ?",
                (json.dumps(vec), summary_id),
            )
        conn.commit()
    except Exception:
        pass  # best-effort; will retry next query


def _truncate_result(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"\n... (truncated to {MAX_RESULT_CHARS} chars)"


def _rows_are_useful(rows: list) -> bool:
    if not rows:
        return False
    for row in rows:
        for part in row.split("\n"):
            if ": " in part:
                val = part.split(": ", 1)[1].strip()
                if val and val.lower() not in ("none", "[]", ""):
                    return True
    return False







# ─────────────────────────────────────────────────────────────
# Helper: extract the date-filter fragment from a full SQL query
# so we can pass it to the semantic ranker (Fix 3).
# ─────────────────────────────────────────────────────────────
def _extract_where_fragment(sql: str) -> str | None:
    """Return everything after WHERE up to ORDER BY / LIMIT / end, or None."""
    m = re.search(r'\bWHERE\b(.+?)(?:\bORDER\s+BY\b|\bLIMIT\b|$)', sql, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return None






# ─────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────
def search_sessions(question: str) -> str:
    """Search session summaries. Best for: broad time windows, daily/weekly
    overviews, project topics, what-did-I-work-on questions."""
    now_ts  = int(time.time())
    now_str = time.strftime("%A %B %d, %Y at %H:%M (local time)")
    user_content = f"Current timestamp: {now_ts} ({now_str})\n\nQuestion: {question}"

    sql = _generate_sql(_SESSIONS_PROMPT, user_content)
    if not sql:
        return (
            "search_sessions: could not generate a SQL query.\n"
            "→ Try search_events for granular event-level detail."
        )
    print(f"[sql:sessions] {sql}")
    if not _is_safe(sql):
        return "search_sessions: unsafe query blocked."


    # Fix 3: use semantic re-ranking when we can extract a date filter
    where_fragment = _extract_where_fragment(sql)
    if where_fragment:
        try:
            rows, total = _semantic_sessions(question, where_fragment)
        except Exception:

            # Fall back to plain SQL on any embedding failure
            try:
                rows, total = _run_sql(sql)
            except Exception as e:
                return f"search_sessions: SQL error — {e}\n→ Try search_events instead."
    else:
        try:
            rows, total = _run_sql(sql)
        except Exception as e:
            return f"search_sessions: SQL error — {e}\n→ Try search_events instead."

    if not _rows_are_useful(rows):
        return (
            "search_sessions: no matching session summaries found.\n"
            "Sessions store broad topic summaries — if you need specific "
            "message text, OCR content, URLs, or app-level detail, "
            "call search_events."
        )


    # Fix 1: include total count so the agent knows if it's seeing a partial view
    shown = len(rows)
    if total > shown:
        header = (
            f"search_sessions results (showing {shown} most relevant of {total} total"
            f" — consider a more specific query or call search_events for details):"
        )
    else:
        header = f"search_sessions results ({shown} sessions matched):"

    return _truncate_result(header + "\n\n" + "\n---\n".join(rows))


def search_events(question: str) -> str:
    """Search individual events. Best for: specific messages, OCR text,
    exact URLs, clipboard content, app switches, fine-grained timestamps."""



    try:
        temporal = resolve_temporal_range(question)
        rag = search_event_rag(
            question,
            start_ts=temporal.start_ts if temporal else None,
            end_ts=temporal.end_ts if temporal else None,
        )
        if rag is not None:
            rows, total = rag
            if rows and _rows_are_useful(rows):
                shown = len(rows)
                if total > shown:
                    header = (
                        f"search_events hybrid RAG results (showing {shown} most relevant of {total} "
                        "embedded events — refine the query for more detail):"
                    )
                else:
                    header = f"search_events RAG results ({shown} hybrid matches):"
                return _truncate_result(header + "\n\n" + "\n---\n".join(rows))
    except Exception as exc:
        print(f"[rag] event search unavailable; using SQL fallback: {exc}")

    now_ts  = int(time.time())
    now_str = time.strftime("%A %B %d, %Y at %H:%M (local time)")
    user_content = f"Current timestamp: {now_ts} ({now_str})\n\nQuestion: {question}"

    sql = _generate_sql(_EVENTS_PROMPT, user_content)
    if not sql:
        return (
            "search_events: could not generate a SQL query.\n"
            "→ Try search_sessions for broader topic/summary search."
        )
    print(f"[sql:events] {sql}")
    if not _is_safe(sql):
        return "search_events: unsafe query blocked."

    try:
        rows, total = _run_sql(sql)
    except Exception as e:
        return f"search_events: SQL error — {e}\n→ Try search_sessions instead."

    if not _rows_are_useful(rows):
        return (
            "search_events: no matching events found.\n"
            "Events store low-level activity — if you need a high-level "
            "topic or time-window summary, call search_sessions."
        )


    # Fix 1: include total count
    shown = len(rows)
    if total > shown:
        header = (
            f"search_events results (showing {shown} most relevant of {total} total"
            f" — refine your query or broaden the time window to see more):"
        )
    else:
        header = f"search_events results ({shown} events matched):"

    return _truncate_result(header + "\n\n" + "\n---\n".join(rows))
