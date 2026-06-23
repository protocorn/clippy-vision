import sqlite3
from pathlib import Path
import time
from core.llm_gateway import gateway, Priority
import re
import json

_DB_PATH = Path(__file__).parent.parent / "core" / "data" / "events.db"

conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=30)
conn.execute("PRAGMA journal_mode=WAL")
conn.commit()


MAX_RESULT_ROWS = 20
MAX_RESULT_CHARS = 4000
_HEAVY_COLS = {"payload", "vector_embedding"}

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
    window_start REAL,   -- Unix epoch
    window_end   REAL,   -- Unix epoch
    summary      TEXT,   -- paragraph describing what the user did
    active_task  TEXT,
    entities     TEXT,   -- JSON array of names/tools/files mentioned
    event_count  INTEGER
)

Date helpers (always use 'localtime'):
  Yesterday : window_start >= CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day','-1 day')) AS INTEGER)
              AND window_start <  CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day')) AS INTEGER)
  Today     : window_start >= CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day')) AS INTEGER)
  This week : window_start >= CAST(strftime('%s', date(<TS>,'unixepoch','localtime','weekday 1','-7 days')) AS INTEGER)

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
    timestamp             REAL,   -- Unix epoch
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
  Yesterday : timestamp >= CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day','-1 day')) AS INTEGER)
              AND timestamp <  CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day')) AS INTEGER)
  Today     : timestamp >= CAST(strftime('%s', date(<TS>,'unixepoch','localtime','start of day')) AS INTEGER)

Rules:
- SELECT only columns needed to answer the question — never SELECT *.
- For keyword searches use: summary, current_window_title, active_url, vision_ocr_text, interest_reason.
- Use OR between search conditions, not AND.
- Prefer interesting=1 rows unless the question requires all events.
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


def _run_sql(sql: str) -> list:
    cur = conn.execute(sql)
    rows = cur.fetchmany(MAX_RESULT_ROWS)
    result_text = []
    for row in rows:
        row_text = []
        for i, col in enumerate(cur.description):
            if col[0] not in _HEAVY_COLS:
                row_text.append(f"{col[0]}: {row[i]}")
        result_text.append("\n".join(row_text))
    return result_text


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
# Public entry points  (replaces the old query_activity black-box)
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

    try:
        rows = _run_sql(sql)
    except Exception as e:
        return f"search_sessions: SQL error — {e}\n→ Try search_events instead."

    if not _rows_are_useful(rows):
        return (
            "search_sessions: no matching session summaries found.\n"
            "Sessions store broad topic summaries — if you need specific "
            "message text, OCR content, URLs, or app-level detail, "
            "call search_events."
        )

    header = f"search_sessions results ({len(rows)} sessions matched):"
    return _truncate_result(header + "\n\n" + "\n---\n".join(rows))


def search_events(question: str) -> str:
    """Search individual events. Best for: specific messages, OCR text,
    exact URLs, clipboard content, app switches, fine-grained timestamps."""
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
        rows = _run_sql(sql)
    except Exception as e:
        return f"search_events: SQL error — {e}\n→ Try search_sessions instead."

    if not _rows_are_useful(rows):
        return (
            "search_events: no matching events found.\n"
            "Events store low-level activity — if you need a high-level "
            "topic or time-window summary, call search_sessions."
        )

    header = f"search_events results ({len(rows)} events matched):"
    return _truncate_result(header + "\n\n" + "\n---\n".join(rows))
