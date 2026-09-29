"""Deletes, corrections, and app lists the desktop UI can show the user."""

from __future__ import annotations

import time
from pathlib import Path

from core.paths import get_screenshots_dir
from core.screenshot_files import delete_screenshot_files
from core.storage import conn

_MAX_SPAN_SECONDS = 36 * 60 * 60


def _orphan_screenshots(filenames: list[str]) -> None:
    root = get_screenshots_dir()
    seen: set[str] = set()
    for name in filenames:
        filename = Path(str(name or "")).name
        if not filename or filename in seen:
            continue
        seen.add(filename)
        still = conn.execute(
            "SELECT 1 FROM events WHERE screenshot_filename = ? LIMIT 1",
            (filename,),
        ).fetchone()
        if still:
            continue
        delete_screenshot_files(root / filename)


def _event_filenames(where_sql: str, params: tuple) -> list[str]:
    rows = conn.execute(
        f"SELECT screenshot_filename FROM events WHERE {where_sql}",
        params,
    ).fetchall()
    return [row[0] for row in rows if row[0]]


def delete_event(event_id: str) -> bool:
    filenames = _event_filenames("event_id = ?", (event_id,))
    cur = conn.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
    conn.commit()
    if not cur.rowcount:
        return False
    _orphan_screenshots(filenames)
    return True


def delete_session(summary_id: str) -> dict | None:
    row = conn.execute(
        "SELECT window_start, window_end FROM sessions WHERE summary_id = ?",
        (summary_id,),
    ).fetchone()
    if row is None:
        return None
    filenames = _event_filenames(
        "timestamp >= ? AND timestamp <= ?",
        (row[0], row[1]),
    )
    events = conn.execute(
        "DELETE FROM events WHERE timestamp >= ? AND timestamp <= ?",
        (row[0], row[1]),
    ).rowcount or 0
    sessions = conn.execute(
        "DELETE FROM sessions WHERE summary_id = ?",
        (summary_id,),
    ).rowcount or 0
    conn.commit()
    _orphan_screenshots(filenames)
    return {"events": int(events), "sessions": int(sessions)}


def delete_time_span(since: float, until: float) -> dict:
    if until <= since or (until - since) > _MAX_SPAN_SECONDS:
        raise ValueError("Choose a single day to delete.")
    filenames = _event_filenames(
        "timestamp >= ? AND timestamp < ?",
        (since, until),
    )
    events = conn.execute(
        "DELETE FROM events WHERE timestamp >= ? AND timestamp < ?",
        (since, until),
    ).rowcount or 0
    sessions = conn.execute(
        """DELETE FROM sessions
           WHERE window_end >= ? AND window_start < ?""",
        (since, until),
    ).rowcount or 0
    conn.commit()
    _orphan_screenshots(filenames)
    return {"events": int(events), "sessions": int(sessions)}


def set_session_correction(summary_id: str, text: str) -> dict | None:
    row = conn.execute(
        "SELECT summary_id FROM sessions WHERE summary_id = ?",
        (summary_id,),
    ).fetchone()
    if row is None:
        return None
    correction = " ".join((text or "").split())
    if len(correction) > 2000:
        correction = correction[:2000].rsplit(" ", 1)[0]
    conn.execute(
        "UPDATE sessions SET user_correction = ? WHERE summary_id = ?",
        (correction, summary_id),
    )
    conn.commit()
    return {"summary_id": summary_id, "user_correction": correction}


def list_seen_apps(limit: int = 24) -> list[dict]:
    limit = min(max(int(limit), 1), 40)
    rows = conn.execute(
        """SELECT process_name, COUNT(*) AS n
           FROM events
           WHERE process_name IS NOT NULL AND TRIM(process_name) != ''
           GROUP BY LOWER(process_name)
           ORDER BY n DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [{"process_name": row[0], "count": int(row[1])} for row in rows]


def list_memory_facts(limit: int = 40) -> list[dict]:
    limit = min(max(int(limit), 1), 80)
    rows = conn.execute(
        """SELECT f.fact_id, f.text, IFNULL(c.label, '')
           FROM memory_facts f
           LEFT JOIN memory_clusters c ON c.cluster_id = f.cluster_id
           WHERE f.valid_to IS NULL AND TRIM(f.text) != ''
           ORDER BY f.created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {"fact_id": row[0], "text": row[1], "label": row[2]}
        for row in rows
    ]


def retire_fact(fact_id: str) -> bool:
    cur = conn.execute(
        """UPDATE memory_facts
           SET valid_to = ?
           WHERE fact_id = ? AND valid_to IS NULL""",
        (time.time(), fact_id),
    )
    conn.commit()
    return bool(cur.rowcount)
