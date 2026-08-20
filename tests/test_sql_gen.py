"""
Standalone test for LLM SQL generation against events.db.
No imports from agent/ — run directly: python tests/test_sql_gen.py

Runs the full query battery RUNS times. Each run is saved to
tests/results/sql_gen_<timestamp>_run<N>.txt
A final summary across all runs is saved to tests/results/sql_gen_<timestamp>_summary.txt
"""

import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent.parent / "core" / "data" / "events.db"
RESULTS_DIR = Path(__file__).parent / "results"
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:8b"
RUNS = 3  # how many times to run the full battery

# ── Schema injected into every prompt ─────────────────────────────────────────

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "sql_query": {"type": "string"},
    },
    "required": ["reasoning", "sql_query"],
}

SCHEMA = """
You have access to a SQLite database with the following tables:

events (
    event_id              TEXT PRIMARY KEY,
    session_id            TEXT,
    timestamp             REAL,          -- Unix epoch float
    event_type            TEXT,
    process_name          TEXT,          -- e.g. "cursor.exe", "chrome.exe"
    current_window_title  TEXT,
    active_url            TEXT,
    previous_process_name TEXT,
    previous_window_title TEXT,
    summary               TEXT,          -- short description of what happened
    payload               TEXT,
    interesting           INTEGER,       -- 1 = flagged as interesting
    interest_score        REAL,
    interest_reason       TEXT,
    vision_ocr_text       TEXT,          -- raw OCR from screen
    vision_activity       TEXT,
    vision_suggested_action TEXT
)

sessions (
    session_id   TEXT,
    summary_id   TEXT PRIMARY KEY,
    created_at   REAL,                   -- Unix epoch float
    window_start REAL,                   -- session start timestamp
    window_end   REAL,                   -- session end timestamp
    summary      TEXT,                   -- paragraph summary of the session
    active_task  TEXT,                   -- what the user was working on
    entities     TEXT,                   -- named entities (people, projects, tools)
    event_count  INTEGER
)

Date/time helpers (SQLite):
  - strftime('%Y-%m-%d', timestamp, 'unixepoch', 'localtime')  → date string
  - strftime('%w', timestamp, 'unixepoch', 'localtime')        → weekday (0=Sunday, 1=Monday ... 6=Saturday)
  - strftime('%H', timestamp, 'unixepoch', 'localtime')        → hour (00-23)
  - Current Unix timestamp is provided as a plain integer in the user message. Use it as a number literal directly in the SQL — do NOT use :now or any bind parameters.
  - Use date(<current_ts>, 'unixepoch', 'localtime', ...) and strftime('%s', ...) for calendar boundaries.
  - Do NOT subtract a fixed number of days for "last Monday", "this week", "weekend", etc. Compute calendar boundaries with SQLite date modifiers.

Safe date patterns to copy:
  - Today:
    timestamp >= CAST(strftime('%s', date(<current_ts>, 'unixepoch', 'localtime', 'start of day')) AS INTEGER)
    AND timestamp < CAST(strftime('%s', date(<current_ts>, 'unixepoch', 'localtime', 'start of day', '+1 day')) AS INTEGER)

  - Yesterday:
    timestamp >= CAST(strftime('%s', date(<current_ts>, 'unixepoch', 'localtime', 'start of day', '-1 day')) AS INTEGER)
    AND timestamp < CAST(strftime('%s', date(<current_ts>, 'unixepoch', 'localtime', 'start of day')) AS INTEGER)

  - This week so far, Monday-based:
    timestamp >= CAST(strftime('%s', date(<current_ts>, 'unixepoch', 'localtime', 'weekday 1', '-7 days')) AS INTEGER)
    AND timestamp <= <current_ts>

  - Last Monday:
    strftime('%Y-%m-%d', timestamp, 'unixepoch', 'localtime') =
    date(<current_ts>, 'unixepoch', 'localtime', 'weekday 1', '-7 days')

  - Last weekend, not future weekend:
    strftime('%w', timestamp, 'unixepoch', 'localtime') IN ('0', '6')
    AND timestamp < <current_ts>

  - Morning means 06:00 through 11:59 unless the user gives a different hour range.

Rules:
- Output ONLY valid SQLite SELECT SQL. No explanation, no markdown, no code fences.
- Never use DROP, DELETE, UPDATE, INSERT, ALTER, CREATE, ATTACH, or PRAGMA.
- Always use 'localtime' modifier in strftime calls.
- Prefer sessions table for broad time/topic queries; use events for granular detail.
- Limit results to 20 rows unless the question asks for counts or aggregates.
- For "usually", "most", "typical", or "pattern" questions, use GROUP BY / COUNT / AVG. Do not just return the latest rows.
- For "what time do I usually start working", group by local HH:MM or local hour and order by frequency. Do not return the single earliest session ever.
- For app usage questions, use events.process_name and GROUP BY process_name.
- For duration questions, prefer sessions and compute SUM(window_end - window_start) or MAX(window_end - window_start). Convert seconds to hours with / 3600.0.
- For keyword or semantic lookup, search summary, active_task, entities, current_window_title, active_url, vision_ocr_text, and interest_reason with LIKE patterns.
- If the user asks "this week" and the target weekend has not happened yet, interpret weekend as the most recent past Saturday/Sunday unless the user explicitly asks about the future.
"""

SYSTEM_PROMPT = (
    SCHEMA.strip()
    + "\n\nGenerate a single SELECT query to answer the user's question in structured JSON format."
)

# ── Test queries ──────────────────────────────────────────────────────────────
# Grouped by type so the summary is easier to read

TEST_QUERIES = [
    # --- relative time and calendar boundaries ---
    ("relative_time", "what did I do earlier today?"),
    ("relative_time", "what was I doing late last night?"),
    ("relative_time", "show me my activity for the past 90 minutes"),
    ("relative_time", "what did I work on during the first half of this week?"),
    # --- explicit ranges and ordering ---
    ("range", "what happened between 2pm and 5pm yesterday?"),
    ("range", "summarize my work from the start of this month until now"),
    ("range", "show the first thing and last thing I did today"),
    # --- patterns and habits ---
    ("pattern", "which days of the week am I most active?"),
    ("pattern", "what do I tend to do after opening Chrome?"),
    ("pattern", "do I usually switch apps a lot during coding sessions?"),
    # --- aggregates and duration ---
    ("aggregate", "which process took the most of my time recently?"),
    (
        "aggregate",
        "how many interesting events did I have today compared to yesterday?",
    ),
    ("aggregate", "what is my average session length over the last 7 days?"),
    ("aggregate", "which active tasks appear most often in my session summaries?"),
    # --- semantic / fuzzy lookup ---
    ("semantic", "find moments where I seemed stuck or debugging something"),
    ("semantic", "did I look at anything related to databases recently?"),
    ("semantic", "find sessions about building or testing an agent"),
    # --- sequence and transition questions ---
    ("sequence", "what did I do immediately after using WhatsApp?"),
    ("sequence", "what app did I usually open before Cursor?"),
    ("sequence", "show me cases where I moved from browser research into coding"),
    # --- low-signal / edge behavior ---
    ("edge", "was there any long idle or low-activity period today?"),
    ("edge", "show me recent events with no useful summary or empty OCR"),
    ("edge", "what is one surprising thing in my recent activity?"),
]

# ── Safety gate ───────────────────────────────────────────────────────────────

_BLOCKED = re.compile(
    r"\b(DROP|DELETE|UPDATE|INSERT|ALTER|CREATE|ATTACH|DETACH|PRAGMA|REPLACE|TRUNCATE)\b",
    re.IGNORECASE,
)


def is_safe(sql: str) -> bool:
    return sql.strip().upper().startswith("SELECT") and not _BLOCKED.search(sql)


# ── Ollama call ───────────────────────────────────────────────────────────────


def generate_sql(query: str) -> tuple[str | None, str | None]:
    """Returns (sql, reasoning). Both are None on failure."""
    now_str = time.strftime("%A %B %d, %Y at %H:%M (local time)")
    now_ts = int(time.time())
    user_content = (
        f"Current date/time: {now_str} (Unix timestamp: {now_ts})\n\nQuestion: {query}"
    )
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "format": OUTPUT_SCHEMA,
        "stream": False,
        "options": {"temperature": 0},
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read())
        content = body["message"]["content"]
        # Ollama may return content as a string or already-parsed dict
        result = content if isinstance(content, dict) else json.loads(content)
        sql = result.get("sql_query", "").strip()
        reasoning = result.get("reasoning", "").strip()
        return (sql or None, reasoning or None)
    except Exception as e:
        print(f"  [generate_sql ERROR] {type(e).__name__}: {e}")
        return None, None


# ── Execute ───────────────────────────────────────────────────────────────────


def run_query(sql: str, conn: sqlite3.Connection) -> list | str:
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.Error as e:
        return f"SQL ERROR: {e}"


# ── Single run ────────────────────────────────────────────────────────────────


def run_once(run_num: int, conn: sqlite3.Connection, out_path: Path) -> dict:
    """Run the full battery once. Returns per-query results dict."""
    results = {}
    lines = []

    header = f"SQL GENERATION TEST — RUN {run_num}/{RUNS}   {time.strftime('%Y-%m-%d %H:%M:%S')}"
    lines.append("=" * 70)
    lines.append(header)
    lines.append("=" * 70)

    passed = failed = blocked = 0

    for i, (category, query) in enumerate(TEST_QUERIES, 1):
        sep = f"\n[{i:02d}/{len(TEST_QUERIES)}] [{category}] {query}"
        lines.append(sep)
        lines.append("-" * 70)

        # Print progress to terminal too
        print(sep.strip())

        sql, reasoning = generate_sql(query)
        print(f"  REASONING: {reasoning}")
        print(f"  SQL: {sql}")
        if reasoning:
            lines.append(f"  REASONING: {reasoning}")
        if not sql:
            status = "NO_SQL"
            lines.append("  FAIL — model returned nothing")
            failed += 1
        elif not is_safe(sql):
            status = "BLOCKED"
            lines.append(f"  BLOCKED — unsafe SQL:\n  {sql}")
            blocked += 1
        else:
            lines.append(f"  SQL: {sql}")
            try:
                conn.execute(f"EXPLAIN QUERY PLAN {sql}")
            except sqlite3.Error as e:
                status = "INVALID"
                lines.append(f"  INVALID — {e}")
                failed += 1
                results[query] = status
                continue

            result = run_query(sql, conn)
            if isinstance(result, str):
                status = "ERROR"
                lines.append(f"  {result}")
                failed += 1
            else:
                status = "PASS"
                lines.append(f"  ROWS: {len(result)}")
                for row in result[:5]:
                    lines.append(f"    {row}")
                if len(result) > 5:
                    lines.append(f"    ... ({len(result) - 5} more rows)")
                passed += 1

        results[query] = status
        print(f"  → {status}")

    summary_line = (
        f"\nRUN {run_num} TOTALS: {passed} passed / {failed} failed / {blocked} blocked"
    )
    lines.append("\n" + "=" * 70)
    lines.append(summary_line)
    lines.append("=" * 70)
    print(summary_line)

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  saved → {out_path}\n")

    return results


# ── Cross-run summary ─────────────────────────────────────────────────────────


def write_summary(all_results: list[dict], summary_path: Path) -> None:
    lines = []
    lines.append("=" * 70)
    lines.append(f"SUMMARY ACROSS {RUNS} RUNS   {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)

    # Per-query consistency table
    lines.append(f"\n{'QUERY':<52} {'RUNS':>6}  OUTCOMES")
    lines.append("-" * 70)
    for category, query in TEST_QUERIES:
        outcomes = [r.get(query, "?") for r in all_results]
        pass_count = outcomes.count("PASS")
        short_q = (query[:49] + "…") if len(query) > 50 else query
        flag = "✓" if pass_count == RUNS else ("~" if pass_count > 0 else "✗")
        lines.append(
            f"  {flag} [{category}] {short_q:<46} {pass_count}/{RUNS}  {outcomes}"
        )

    # Overall counts
    total_pass = sum(v == "PASS" for r in all_results for v in r.values())
    total_blocked = sum(v == "BLOCKED" for r in all_results for v in r.values())
    total_invalid = sum(
        v in ("INVALID", "ERROR", "NO_SQL") for r in all_results for v in r.values()
    )
    total = len(TEST_QUERIES) * RUNS

    lines.append("\n" + "=" * 70)
    lines.append(
        f"OVERALL: {total_pass}/{total} passed | {total_invalid} failed | {total_blocked} blocked"
    )
    lines.append("=" * 70)

    summary_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSummary saved → {summary_path}")
    print("\n".join(lines[-6:]))  # print just the tail to terminal


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    session_ts = time.strftime("%Y%m%d_%H%M%S")
    all_results = []

    for run_num in range(1, RUNS + 1):
        print(f"\n{'#' * 70}")
        print(f"# RUN {run_num} of {RUNS}")
        print(f"{'#' * 70}\n")
        out_path = RESULTS_DIR / f"sql_gen_{session_ts}_run{run_num}.txt"
        results = run_once(run_num, conn, out_path)
        all_results.append(results)

    summary_path = RESULTS_DIR / f"sql_gen_{session_ts}_summary.txt"
    write_summary(all_results, summary_path)
    conn.close()


if __name__ == "__main__":
    main()
