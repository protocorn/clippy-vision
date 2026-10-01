"""Decide whether stored text still belongs to a private window.

Session summaries, memory facts, and clipboard lines often drop the process
name. A cloud reply can keep a row only when this module can show it came
from a window the user did not mark private.
"""

from __future__ import annotations

_PRIVATE_TITLE = "private window"
_EXPLICIT_FACT_SOURCES = frozenset({"agent", "user"})


def event_is_private(process_name: str, title: str) -> bool:
    """True for a privacy-listed app, Clippy itself, or a private browser window."""
    if (title or "").strip().casefold() == _PRIVATE_TITLE:
        return True
    from core.privacy_settings import is_clippy_window, should_redact_window

    return is_clippy_window(process_name or "", title or "") or should_redact_window(
        process_name or "", title or ""
    )


def window_private_state(window_start: float, window_end: float) -> int | None:
    """1 if any event in the span is private, 0 if all are clean, None if none remain."""
    from core.storage import conn

    rows = conn.execute(
        """SELECT process_name, current_window_title FROM events
           WHERE timestamp >= ? AND timestamp <= ?""",
        (float(window_start), float(window_end)),
    ).fetchall()
    if not rows:
        return None
    if any(event_is_private(row[0] or "", row[1] or "") for row in rows):
        return 1
    return 0


def resolve_session_private(
    summary_id: str,
    window_start: float,
    window_end: float,
    private: int | None,
) -> int | None:
    """Return the stored flag, or compute it from events and save that answer."""
    if private == 0 or private == 1:
        return int(private)
    state = window_private_state(window_start, window_end)
    from core.storage import conn

    conn.execute(
        "UPDATE sessions SET private = ? WHERE summary_id = ?",
        (state, summary_id),
    )
    conn.commit()
    return state


def summary_text_is_clean(summary: str) -> bool:
    """True only when this exact summary is stored and marked clean."""
    text = (summary or "").strip()
    if not text:
        return False
    from core.storage import conn

    row = conn.execute(
        """SELECT summary_id, window_start, window_end, private
           FROM sessions WHERE summary = ?
           ORDER BY created_at DESC LIMIT 1""",
        (text,),
    ).fetchone()
    if row is None:
        return False
    return resolve_session_private(row[0], row[1], row[2], row[3]) == 0


def span_is_clean(window_start, window_end) -> bool:
    """True when every session on this exact span is clean, or the events are."""
    try:
        start = float(window_start)
        end = float(window_end)
    except (TypeError, ValueError):
        return False
    from core.storage import conn

    rows = conn.execute(
        """SELECT summary_id, window_start, window_end, private
           FROM sessions WHERE window_start = ? AND window_end = ?""",
        (start, end),
    ).fetchall()
    if rows:
        return all(
            resolve_session_private(row[0], row[1], row[2], row[3]) == 0
            for row in rows
        )
    return window_private_state(start, end) == 0


def fact_is_stored(text: str) -> bool:
    needle = (text or "").strip()
    if not needle:
        return False
    from core.storage import conn

    row = conn.execute(
        """SELECT 1 FROM memory_facts
           WHERE text = ? AND valid_to IS NULL LIMIT 1""",
        (needle,),
    ).fetchone()
    return row is not None


def fact_text_is_clean(text: str) -> bool:
    """True for an explicit user note, or a distiller fact marked clean."""
    needle = (text or "").strip()
    if not needle:
        return False
    from core.storage import conn

    row = conn.execute(
        """SELECT source, private FROM memory_facts
           WHERE text = ? AND valid_to IS NULL
           ORDER BY created_at DESC LIMIT 1""",
        (needle,),
    ).fetchone()
    if row is None:
        return False
    source = (row[0] or "").strip().lower()
    private = row[1]
    if private == 1:
        return False
    if source in _EXPLICIT_FACT_SOURCES:
        return True
    return private == 0


def cluster_label_is_clean(label: str) -> bool:
    """True when every active fact in the cluster may leave the machine."""
    name = (label or "").strip()
    if not name:
        return False
    from core.storage import conn

    row = conn.execute(
        "SELECT cluster_id FROM memory_clusters WHERE lower(label) = lower(?) LIMIT 1",
        (name,),
    ).fetchone()
    if row is None:
        return False
    facts = conn.execute(
        """SELECT source, private FROM memory_facts
           WHERE cluster_id = ? AND valid_to IS NULL""",
        (row[0],),
    ).fetchall()
    if not facts:
        return False
    for source, private in facts:
        src = (source or "").strip().lower()
        if private == 1:
            return False
        if src in _EXPLICIT_FACT_SOURCES:
            continue
        if private != 0:
            return False
    return True
