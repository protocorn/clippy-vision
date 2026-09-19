import json
import math
import re
import time
from difflib import get_close_matches

from agent.helpers.keywords import keywords_from_query
from agent.helpers.time_resolver import resolve_temporal_range
from core.chat_model import get_chat_model
from core.llm_gateway import Priority, gateway
from core.local_embeddings import embed_text, embed_texts
from core.rag import search_event_rag
from core.storage import conn

MAX_RESULT_ROWS = 20
MAX_RESULT_CHARS = 8000
# Embeddings only — never strip payload. Paste/clipboard answers live there
# (JSON with pasted_content), and omitting it forced MCP callers to open
# events.db by hand to recover essay text the tools were supposed to return.
_HEAVY_COLS = {"vector_embedding", "summary_embedding", "image_embedding"}
_PAYLOAD_FIELD_MAX = 3000
_OCR_FIELD_MAX = 2000

# Schema the LLM SQL path may reference. Unknown identifiers are fuzzy-matched
# against this set (not a hardcoded typo dictionary); weak/no match is left
# alone so aliases survive, and SQLite + one corrective retry handles the rest.
_EVENT_COLUMNS = frozenset({
    "event_id", "session_id", "timestamp", "event_type", "process_name",
    "current_window_title", "active_url", "previous_process_name",
    "previous_window_title", "summary", "payload", "interesting",
    "interest_score", "interest_reason", "vision_ocr_text", "vision_activity",
    "vision_suggested_action", "screenshot_filename", "classification_status",
})
# Strict cutoff so aliases like event_time are not rewritten to event_type.
_COLUMN_FUZZY_CUTOFF = 0.88

MODEL = get_chat_model()

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
    event_type            TEXT,   -- paste | clipboard_change | context_change | screenshot_analysis | typing_burst | ...
    process_name          TEXT,
    current_window_title  TEXT,
    active_url            TEXT,
    summary               TEXT,
    payload               TEXT,   -- JSON; paste/clipboard text lives here as pasted_content (or similar keys)
    interesting           INTEGER,
    interest_score        REAL,
    interest_reason       TEXT,
    vision_ocr_text       TEXT,   -- EXACT name of the column
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

COLUMN NAME RULE (hard):
- Use ONLY the column names listed above, character-for-character.
- Paste / clipboard / essay / form-answer content is in payload, never in a invented column.

Rules:
- SELECT only columns needed to answer the question — never SELECT *.
- For keyword searches use: summary, current_window_title, active_url, vision_ocr_text, interest_reason, payload.
- For any question about what the user was doing, reading, working on, or looking at — always include
  vision_activity and vision_ocr_text in the SELECT list alongside summary (they may be NULL but include them).
- For paste, clipboard, copied text, essay, form answer, or "what did I write/paste" questions:
  filter event_type IN ('paste', 'clipboard_change') and SELECT payload, summary, current_window_title,
  datetime(timestamp,'unixepoch','localtime') as time. Prefer LIKE '%keyword%' on payload and summary.
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
_SQL_STRING_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]*)\b")
_SQL_KEYWORDS = frozenset({
    "select", "from", "where", "and", "or", "not", "in", "like", "between",
    "is", "null", "as", "on", "join", "left", "right", "inner", "outer",
    "order", "by", "group", "having", "limit", "offset", "asc", "desc",
    "case", "when", "then", "else", "end", "distinct", "count", "sum",
    "avg", "min", "max", "cast", "strftime", "datetime", "date", "time",
    "unixepoch", "localtime", "start", "of", "day", "weekday", "integer",
    "real", "text", "exists", "union", "all", "ifnull", "coalesce",
    "lower", "upper", "length", "substr", "replace", "trim", "round",
    "abs", "events", "sessions",
})


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


def _normalize_ident(name: str) -> str:
    """Normalize invented forms like vision_ocr.txt toward underscored names."""
    return name.replace(".", "_").casefold()


def _fuzzy_column_leaf(leaf: str) -> str | None:
    allowed = {col.casefold(): col for col in _EVENT_COLUMNS}
    key = _normalize_ident(leaf)
    if key in allowed:
        return allowed[key]
    if key in _SQL_KEYWORDS or len(key) <= 1:
        return None
    matches = get_close_matches(
        key, list(allowed.keys()), n=1, cutoff=_COLUMN_FUZZY_CUTOFF
    )
    return allowed[matches[0]] if matches else None


def _fuzzy_column(name: str) -> str | None:
    """Return the real schema column closest to name, or None if no strong match.

    `_SQL_KEYWORDS` skips SQL grammar/functions (`strftime`, `localtime`, …).
    Anything else that is not already a real column is a candidate: close
    enough → rewrite to the schema name; otherwise leave alone (aliases) and
    let SQLite error → one corrective retry.
    """
    # Table-qualified refs (e.vision_ocr_text). Do NOT split typo dots like
    # vision_ocr.txt — those are one invented identifier.
    if "." in name:
        prefix, leaf = name.rsplit(".", 1)
        is_qualifier = (
            prefix.casefold() in {"events", "sessions"}
            or (len(prefix) <= 2 and prefix.isalpha())
        )
        if is_qualifier:
            resolved = _fuzzy_column_leaf(leaf)
            return f"{prefix}.{resolved}" if resolved else None

    return _fuzzy_column_leaf(name)


def _sanitize_event_sql(sql: str) -> str:
    """Fuzzy-resolve unknown identifiers against the real events schema."""
    # Mask string literals so LIKE '%vision_ocr.txt%' is not rewritten.
    pieces: list[str] = []
    last = 0
    for match in _SQL_STRING_RE.finditer(sql):
        pieces.append(_rewrite_idents_in_span(sql[last:match.start()]))
        pieces.append(match.group(0))
        last = match.end()
    pieces.append(_rewrite_idents_in_span(sql[last:]))
    return "".join(pieces)


def _rewrite_idents_in_span(span: str) -> str:
    if not span:
        return span
    out: list[str] = []
    last = 0
    for match in _IDENT_RE.finditer(span):
        out.append(span[last:match.start()])
        ident = match.group(1)
        resolved = _fuzzy_column(ident)
        out.append(resolved if resolved else ident)
        last = match.end()
    out.append(span[last:])
    return "".join(out)


def _format_field_value(col: str, val) -> str:
    if val is None:
        return "None"
    if not isinstance(val, str):
        return str(val)
    text = val.encode("utf-8", errors="replace").decode("utf-8")
    if col == "payload":
        text = _payload_display_text(text)
        if len(text) > _PAYLOAD_FIELD_MAX:
            text = text[:_PAYLOAD_FIELD_MAX] + f"... (truncated to {_PAYLOAD_FIELD_MAX} chars)"
    elif col == "vision_ocr_text" and len(text) > _OCR_FIELD_MAX:
        text = text[:_OCR_FIELD_MAX] + f"... (truncated to {_OCR_FIELD_MAX} chars)"
    return text


def _payload_display_text(payload: str) -> str:
    """Surface pasted_content (etc.) instead of raw JSON when possible."""
    if not payload:
        return ""
    try:
        obj = json.loads(payload)
        if isinstance(obj, dict):
            for key in ("pasted_content", "clipboard_content", "text", "content"):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in obj.values():
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if isinstance(obj, str):
            return obj.strip()
    except (json.JSONDecodeError, TypeError):
        pass
    return payload.strip()


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
                row_text.append(f"{col[0]}: {_format_field_value(col[0], row[i])}")
        result_text.append("\n".join(row_text))
    return result_text, total


def _sanitize_for_fts(keyword: str) -> str:
    clean = re.sub(r"[^\w]", "", keyword)
    return f'"{clean}"' if clean else ""


def _fts_event_search(question: str) -> tuple[list, int] | None:
    """Deterministic keyword path over events_fts — no LLM SQL required.

    This is what should have answered the Makeable paste/OCR hunt without
    inventing column names. Returns None when there are no usable keywords.
    """
    keywords = keywords_from_query(question)
    if not keywords:
        return None
    tokens = [_sanitize_for_fts(kw) for kw in keywords[:8]]
    tokens = [t for t in tokens if t]
    if not tokens:
        return None

    fts_query = " OR ".join(tokens)
    temporal = resolve_temporal_range(question)
    time_filter = "1=1"
    params: list = [fts_query]
    if temporal is not None:
        time_filter = "e.timestamp >= ? AND e.timestamp < ?"
        params.extend([temporal.start_ts, temporal.end_ts])

    # Prefer paste/clipboard when the question is clearly about that, otherwise
    # search titles/OCR/payload together so screen text still surfaces.
    q_lower = question.casefold()
    paste_only = any(
        word in q_lower
        for word in ("paste", "pasted", "clipboard", "copied", "essay", "form answer")
    )
    type_filter = ""
    if paste_only:
        type_filter = "AND e.event_type IN ('paste', 'clipboard_change')"

    sql = f"""
        SELECT e.timestamp, e.event_type, e.process_name, e.current_window_title,
               e.active_url, e.summary, e.payload, e.vision_ocr_text, e.vision_activity
        FROM events_fts
        JOIN events e ON events_fts.rowid = e.rowid
        WHERE events_fts MATCH ?
          AND {time_filter}
          {type_filter}
        ORDER BY events_fts.rank, e.timestamp DESC
        LIMIT 40
    """
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception as exc:
        print(f"[fts:events] unavailable: {exc}")
        return None
    if not rows:
        return [], 0

    result_text = []
    for row in rows[:MAX_RESULT_ROWS]:
        (
            ts, event_type, process_name, window_title, active_url,
            summary, payload, ocr, activity,
        ) = row
        parts = [
            f"time: {time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}",
            f"event_type: {event_type}",
        ]
        if process_name:
            parts.append(f"process_name: {process_name}")
        if window_title:
            parts.append(f"current_window_title: {window_title}")
        if active_url:
            parts.append(f"active_url: {active_url}")
        if summary:
            parts.append(f"summary: {summary}")
        if payload:
            parts.append(f"payload: {_format_field_value('payload', payload)}")
        if activity:
            parts.append(f"vision_activity: {activity}")
        if ocr:
            parts.append(f"vision_ocr_text: {_format_field_value('vision_ocr_text', ocr)}")
        result_text.append("\n".join(parts))
    return result_text, len(rows)


# A small model hand-writing date arithmetic + hard filters like "interesting = 1"
# is a narrow, single-shot guess — if that guess is even slightly off (wrong week
# boundary, filter too strict for this data), it silently returns zero rows and
# the caller reports "no data" even when real activity exists. Widen once before
# trusting an empty result, the same way we already retry once on a bad column name.
_INTERESTING_FILTER_RE = re.compile(r"\s+AND\s+interesting\s*=\s*1\b", re.IGNORECASE)


def _drop_interesting_filter(sql: str) -> str | None:
    widened = _INTERESTING_FILTER_RE.sub("", sql, count=1)
    return widened if widened != sql else None


def _execute_event_sql(question: str, user_content: str) -> tuple[list, int] | str:
    """Generate → sanitize → run event SQL, with one corrective retry on failure
    and one widen retry if a valid query returns nothing (see note above)."""
    sql = _generate_sql(_EVENTS_PROMPT, user_content)
    if not sql:
        return (
            "search_events: could not generate a SQL query.\n"
            "→ Try search_sessions for broader topic/summary search."
        )

    for attempt in range(2):
        print(f"[sql:events] {sql}")
        if not _is_safe(sql):
            return "search_events: unsafe query blocked."
        sanitized = _sanitize_event_sql(sql)
        try:
            rows, total = _run_sql(sanitized)
        except Exception as e:
            err = str(e)
            print(f"[sql:events] error: {err}")
            if attempt == 0 and "no such column" in err.casefold():
                sql = _generate_sql(
                    _EVENTS_PROMPT,
                    user_content
                    + f"\n\nPrevious SQL failed with: {err}\n"
                    "Regenerate using ONLY the exact column names from the schema "
                    f"({', '.join(sorted(_EVENT_COLUMNS))}). Never invent columns.",
                )
                if not sql:
                    return f"search_events: SQL error — {e}\n→ Try search_sessions instead."
                continue
            return f"search_events: SQL error — {e}\n→ Try search_sessions instead."

        if total == 0:
            widened = _drop_interesting_filter(sanitized)
            if widened:
                print("[sql:events] 0 rows with interesting=1 — retrying without that filter")
                try:
                    widened_rows, widened_total = _run_sql(widened)
                    if widened_total > 0:
                        return widened_rows, widened_total
                except Exception as e:
                    print(f"[sql:events] widen retry failed: {e}")
        return rows, total
    return "search_events: SQL error — retries exhausted.\n→ Try search_sessions instead."






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
    available = conn.execute(
        "SELECT 1 FROM sessions WHERE summary IS NOT NULL AND summary != '' LIMIT 1"
    ).fetchone()
    if available is None:
        return (
            "search_sessions: no matching session summaries found.\n"
            "Sessions store broad topic summaries — if you need specific "
            "message text, OCR content, URLs, or app-level detail, "
            "call search_events."
        )

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
        # The exact time window came back empty even though the table has real
        # summaries somewhere (checked at the top of this function) — same class
        # of failure as the events widen-retry above: a single LLM-guessed date
        # boundary is not trustworthy enough to declare "no data" on its own.
        # Widen once to "most recent sessions, any window" before giving up.
        if where_fragment:
            try:
                widened_rows, widened_total = _run_sql(
                    "SELECT summary, active_task, entities, "
                    "datetime(window_start,'unixepoch','localtime') as time "
                    "FROM sessions WHERE summary IS NOT NULL AND summary != '' "
                    "ORDER BY window_start DESC LIMIT 20"
                )
            except Exception as e:
                widened_rows, widened_total = [], 0
                print(f"[sql:sessions] widen retry failed: {e}")
            if _rows_are_useful(widened_rows):
                header = (
                    f"search_sessions: nothing matched the exact requested window — "
                    f"showing the {len(widened_rows)} most recent sessions instead "
                    "(tell the user this is outside the timeframe they asked about):"
                )
                return _truncate_result(header + "\n\n" + "\n---\n".join(widened_rows))

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

    # Deterministic FTS before LLM SQL — keyword/paste/OCR hunts must not
    # depend on the model inventing valid column names.
    try:
        fts = _fts_event_search(question)
        if fts is not None:
            rows, total = fts
            if rows and _rows_are_useful(rows):
                shown = len(rows)
                if total > shown:
                    header = (
                        f"search_events FTS results (showing {shown} most relevant of {total} "
                        "keyword matches — refine the query for more detail):"
                    )
                else:
                    header = f"search_events FTS results ({shown} keyword matches):"
                return _truncate_result(header + "\n\n" + "\n---\n".join(rows))
    except Exception as exc:
        print(f"[fts] event search unavailable; using SQL fallback: {exc}")

    now_ts  = int(time.time())
    now_str = time.strftime("%A %B %d, %Y at %H:%M (local time)")
    user_content = f"Current timestamp: {now_ts} ({now_str})\n\nQuestion: {question}"

    outcome = _execute_event_sql(question, user_content)
    if isinstance(outcome, str):
        return outcome
    rows, total = outcome

    if not _rows_are_useful(rows):
        return (
            "search_events: no matching events found.\n"
            "Events store low-level activity — if you need a high-level "
            "topic or time-window summary, call search_sessions."
        )

    shown = len(rows)
    if total > shown:
        header = (
            f"search_events results (showing {shown} most relevant of {total} total"
            f" — refine your query or broaden the time window to see more):"
        )
    else:
        header = f"search_events results ({shown} events matched):"

    return _truncate_result(header + "\n\n" + "\n---\n".join(rows))
