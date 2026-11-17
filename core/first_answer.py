"""One ready-made question after the first stretch of captured work.

Nothing here calls a model or leaves the machine. The desktop home screen
(the session timeline) asks for this payload and shows it once.
"""

from __future__ import annotations

GENERIC_QUESTION = "What was I working on just now?"
MIN_CAPTURE_SECONDS = 30 * 60
QUESTION_TASK_CHARS = 72

_FRIENDLY_APPS = {
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "msedge": "Microsoft Edge",
    "microsoft edge": "Microsoft Edge",
    "firefox": "Firefox",
    "code": "Visual Studio Code",
    "cursor": "Cursor",
    "slack": "Slack",
    "discord": "Discord",
    "notepad": "Notepad",
    "explorer": "File Explorer",
    "windowsterminal": "Windows Terminal",
    "windows terminal": "Windows Terminal",
    "powershell": "PowerShell",
    "safari": "Safari",
    "terminal": "Terminal",
}


def shorten_phrase(text: str, limit: int = QUESTION_TASK_CHARS) -> str:
    phrase = " ".join(str(text or "").split())
    if len(phrase) <= limit:
        return phrase
    return phrase[: max(limit - 3, 0)].rstrip() + "..."


def friendly_app_name(process_name: str) -> str:
    raw = str(process_name or "").replace(".exe", "").strip()
    if not raw:
        return ""
    return _FRIENDLY_APPS.get(raw.lower(), raw)


def build_suggested_question(
    *,
    active_task: str | None = None,
    process_name: str | None = None,
    private: bool = False,
) -> str:
    """One paste-ready question. Private sessions never contribute wording."""
    if private:
        return GENERIC_QUESTION
    task = str(active_task or "").strip()
    if task:
        return f"What was I doing during “{shorten_phrase(task)}”?"
    app = friendly_app_name(process_name or "")
    if app:
        return f"What was I working on in {app}?"
    return GENERIC_QUESTION


def _event_span_seconds() -> float:
    from core.storage import conn

    row = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM events").fetchone()
    if not row or row[0] is None or row[1] is None:
        return 0.0
    return max(0.0, float(row[1]) - float(row[0]))


def _newest_session_row() -> dict | None:
    from core.storage import conn

    row = conn.execute(
        """SELECT summary_id, active_task, summary, window_start, window_end,
                  IFNULL(private, 0)
           FROM sessions
           WHERE summary IS NOT NULL AND summary != ''
           ORDER BY window_end DESC, created_at DESC, summary_id DESC
           LIMIT 1"""
    ).fetchone()
    if not row:
        return None
    return {
        "summary_id": row[0],
        "active_task": row[1] or "",
        "summary": row[2] or "",
        "window_start": row[3],
        "window_end": row[4],
        "private": bool(row[5]),
    }


def _top_process_name(since: float | None = None, until: float | None = None) -> str:
    from core.storage import conn

    filters = ["IFNULL(process_name, '') != ''"]
    params: list = []
    if since is not None:
        filters.append("timestamp >= ?")
        params.append(since)
    if until is not None:
        filters.append("timestamp <= ?")
        params.append(until)
    row = conn.execute(
        f"""SELECT process_name, COUNT(*) AS n
            FROM events
            WHERE {' AND '.join(filters)}
            GROUP BY process_name
            ORDER BY n DESC, process_name ASC
            LIMIT 1""",
        params,
    ).fetchone()
    return str(row[0]) if row else ""


def first_answer_from_inputs(
    session: dict | None,
    span_seconds: float,
    process_name: str = "",
) -> dict:
    eligible = session is not None or span_seconds >= MIN_CAPTURE_SECONDS
    question = build_suggested_question(
        active_task=None if not session else session.get("active_task"),
        process_name=process_name,
        private=bool(session and session.get("private")),
    )
    source = None
    if session is not None:
        source = "session"
    elif span_seconds >= MIN_CAPTURE_SECONDS:
        source = "capture_span"
    return {
        "eligible": eligible,
        "question": question,
        "generic": question == GENERIC_QUESTION,
        "has_session": session is not None,
        "capture_span_seconds": span_seconds,
        "min_capture_seconds": MIN_CAPTURE_SECONDS,
        "source": source,
    }


def get_first_answer_prompt() -> dict:
    session = _newest_session_row()
    span = _event_span_seconds()
    process_name = ""
    if session and not session["private"]:
        process_name = _top_process_name(session["window_start"], session["window_end"])
    elif session is None and span >= MIN_CAPTURE_SECONDS:
        process_name = _top_process_name()
    return first_answer_from_inputs(session, span, process_name)
