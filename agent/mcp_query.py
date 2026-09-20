"""Structured activity queries for MCP (no LLM SQL).

Gives big-model consumers explicit time bounds, coverage, app-time aggregates,
URL inventories, and paginated session lists — the gaps dogfooding exposed.
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from core.paths import get_screenshots_dir
from core.screenshot_ttl import get_screenshot_payload
from core.storage import conn, list_timeline_sessions

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def parse_time_bound(value: str | float | int | None, *, end_of_day: bool = False) -> float | None:
    """Accept epoch seconds or ISO date/datetime (local)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d" and end_of_day:
                dt = dt + timedelta(days=1)
                return dt.timestamp()
            if fmt == "%Y-%m-%d":
                return dt.timestamp()
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(f"unrecognized time bound: {value!r} (use epoch or YYYY-MM-DD)")


def activity_coverage(
    start: str | float | None,
    end: str | float | None = None,
    *,
    bucket_hours: float = 1.0,
) -> dict[str, Any]:
    """Hourly (or custom) event counts so gaps are honest."""
    start_ts = parse_time_bound(start)
    end_ts = parse_time_bound(end, end_of_day=True) if end is not None else time.time()
    if start_ts is None:
        end_ts = end_ts or time.time()
        start_ts = end_ts - 7 * 86400
    if end_ts is None:
        end_ts = time.time()
    if start_ts >= end_ts:
        return {"ok": False, "error": "start must be before end", "buckets": []}

    bucket_secs = max(0.25, float(bucket_hours)) * 3600.0
    rows = conn.execute(
        """SELECT timestamp, process_name FROM events
           WHERE timestamp >= ? AND timestamp < ?
           ORDER BY timestamp ASC""",
        (start_ts, end_ts),
    ).fetchall()

    buckets: dict[int, dict[str, Any]] = {}
    cursor = start_ts
    idx = 0
    while cursor < end_ts:
        buckets[idx] = {
            "start": cursor,
            "end": min(cursor + bucket_secs, end_ts),
            "start_local": datetime.fromtimestamp(cursor).isoformat(timespec="minutes"),
            "event_count": 0,
            "top_processes": {},
        }
        cursor += bucket_secs
        idx += 1

    for ts, process in rows:
        bi = int((float(ts) - start_ts) // bucket_secs)
        if bi < 0 or bi not in buckets:
            continue
        buckets[bi]["event_count"] += 1
        pname = (process or "unknown").strip() or "unknown"
        tops = buckets[bi]["top_processes"]
        tops[pname] = tops.get(pname, 0) + 1

    out_buckets = []
    empty = 0
    for bi in sorted(buckets):
        b = buckets[bi]
        tops = sorted(b["top_processes"].items(), key=lambda x: -x[1])[:5]
        b["top_processes"] = [{"process": p, "events": n} for p, n in tops]
        if b["event_count"] == 0:
            empty += 1
        out_buckets.append(b)

    return {
        "ok": True,
        "start": start_ts,
        "end": end_ts,
        "bucket_hours": bucket_hours,
        "bucket_count": len(out_buckets),
        "empty_buckets": empty,
        "total_events": len(rows),
        "buckets": out_buckets,
    }


def list_sessions_range(
    start: str | float | None = None,
    end: str | float | None = None,
    *,
    limit: int = 40,
    offset: int = 0,
) -> dict[str, Any]:
    start_ts = parse_time_bound(start)
    end_ts = parse_time_bound(end, end_of_day=True) if end is not None else None
    page = list_timeline_sessions(since=start_ts, until=end_ts, limit=limit, offset=offset)
    # Compact for MCP
    sessions = []
    for s in page["sessions"]:
        sessions.append({
            "session_id": s["session_id"],
            "summary_id": s["summary_id"],
            "window_start": s["window_start"],
            "window_end": s["window_end"],
            "window_start_local": datetime.fromtimestamp(s["window_start"]).isoformat(timespec="minutes")
            if s.get("window_start") else None,
            "window_end_local": datetime.fromtimestamp(s["window_end"]).isoformat(timespec="minutes")
            if s.get("window_end") else None,
            "active_task": s.get("active_task"),
            "event_count": s.get("event_count"),
            "summary": (s.get("summary") or "")[:800],
        })
    return {
        "ok": True,
        "total": page["total"],
        "limit": page["limit"],
        "offset": page["offset"],
        "sessions": sessions,
    }


def app_time_summary(
    start: str | float | None,
    end: str | float | None = None,
) -> dict[str, Any]:
    """Approximate foreground time from consecutive context_change / events."""
    start_ts = parse_time_bound(start)
    end_ts = parse_time_bound(end, end_of_day=True) if end is not None else time.time()
    if start_ts is None:
        end_ts = end_ts or time.time()
        start_ts = end_ts - 86400
    if end_ts is None:
        end_ts = time.time()

    rows = conn.execute(
        """SELECT timestamp, process_name, event_type FROM events
           WHERE timestamp >= ? AND timestamp < ?
           ORDER BY timestamp ASC""",
        (start_ts, end_ts),
    ).fetchall()

    durations: dict[str, float] = defaultdict(float)
    event_counts: dict[str, int] = defaultdict(int)
    prev_ts: float | None = None
    prev_proc: str | None = None
    max_gap = 300.0  # don't credit idle gaps >5m to an app

    for ts, process, _etype in rows:
        ts = float(ts)
        proc = (process or "unknown").strip() or "unknown"
        event_counts[proc] += 1
        if prev_ts is not None and prev_proc is not None:
            gap = min(max(0.0, ts - prev_ts), max_gap)
            durations[prev_proc] += gap
        prev_ts = ts
        prev_proc = proc

    ranked = sorted(durations.items(), key=lambda x: -x[1])
    apps = [
        {
            "process": name,
            "seconds": round(secs, 1),
            "minutes": round(secs / 60.0, 2),
            "event_count": event_counts.get(name, 0),
        }
        for name, secs in ranked[:30]
    ]
    return {
        "ok": True,
        "start": start_ts,
        "end": end_ts,
        "note": "Durations are approximate from event gaps (capped at 5 min per gap).",
        "apps": apps,
    }


def list_urls(
    pattern: str = "",
    start: str | float | None = None,
    end: str | float | None = None,
    *,
    limit: int = 40,
) -> dict[str, Any]:
    start_ts = parse_time_bound(start)
    end_ts = parse_time_bound(end, end_of_day=True) if end is not None else None
    filters = ["1=1"]
    params: list[Any] = []
    if start_ts is not None:
        filters.append("timestamp >= ?")
        params.append(start_ts)
    if end_ts is not None:
        filters.append("timestamp < ?")
        params.append(end_ts)
    where = " AND ".join(filters)

    rows = conn.execute(
        f"""SELECT timestamp, active_url, vision_ocr_text, current_window_title, process_name
            FROM events
            WHERE {where}
              AND (
                (active_url IS NOT NULL AND active_url != '')
                OR (vision_ocr_text IS NOT NULL AND vision_ocr_text LIKE '%http%')
              )
            ORDER BY timestamp DESC
            LIMIT 500""",
        params,
    ).fetchall()

    needle = (pattern or "").strip().lower()
    seen: dict[str, dict[str, Any]] = {}
    for ts, active_url, ocr, title, process in rows:
        candidates: list[str] = []
        if active_url:
            candidates.append(active_url.strip())
        if ocr:
            candidates.extend(_URL_RE.findall(ocr))
        for url in candidates:
            url = url.rstrip(").,];'\"")
            if needle and needle not in url.lower():
                continue
            key = url.lower()
            if key not in seen:
                seen[key] = {
                    "url": url,
                    "first_seen": ts,
                    "last_seen": ts,
                    "process": process,
                    "title": title,
                    "count": 1,
                }
            else:
                seen[key]["count"] += 1
                seen[key]["last_seen"] = max(seen[key]["last_seen"], ts)
                seen[key]["first_seen"] = min(seen[key]["first_seen"], ts)

    ranked = sorted(seen.values(), key=lambda u: (-u["count"], -u["last_seen"]))
    return {
        "ok": True,
        "total_distinct": len(ranked),
        "urls": ranked[: max(1, min(limit, 100))],
    }


def list_screenshots(
    start: str | float | None = None,
    end: str | float | None = None,
    *,
    limit: int = 30,
) -> dict[str, Any]:
    start_ts = parse_time_bound(start)
    end_ts = parse_time_bound(end, end_of_day=True) if end is not None else None
    directory = get_screenshots_dir()
    items: list[dict[str, Any]] = []

    # Prefer DB-linked screenshots (survive listing after purge with OCR note)
    filters = ["screenshot_filename IS NOT NULL AND screenshot_filename != ''"]
    params: list[Any] = []
    if start_ts is not None:
        filters.append("timestamp >= ?")
        params.append(start_ts)
    if end_ts is not None:
        filters.append("timestamp < ?")
        params.append(end_ts)
    rows = conn.execute(
        f"""SELECT timestamp, screenshot_filename, vision_ocr_text, process_name,
                   current_window_title, interesting
            FROM events
            WHERE {' AND '.join(filters)}
            ORDER BY timestamp DESC
            LIMIT ?""",
        (*params, max(1, min(limit, 100))),
    ).fetchall()

    for ts, filename, ocr, process, title, interesting in rows:
        path = directory / filename if filename else None
        on_disk = bool(path and path.is_file())
        items.append({
            "timestamp": ts,
            "timestamp_local": datetime.fromtimestamp(ts).isoformat(timespec="seconds"),
            "filename": filename,
            "image_available": on_disk,
            "process": process,
            "title": title,
            "interesting": bool(interesting),
            "ocr_preview": ((ocr or "")[:240]),
        })

    return {"ok": True, "count": len(items), "screenshots": items}


def get_screenshot(filename: str = "", timestamp: float | None = None) -> dict[str, Any]:
    if not filename and timestamp is None:
        return {"ok": False, "error": "provide filename or timestamp"}
    return get_screenshot_payload(
        filename=filename or None,
        timestamp=timestamp,
    )


def search_with_bounds(
    question: str,
    *,
    start: str | float | None = None,
    end: str | float | None = None,
    limit: int = 20,
    offset: int = 0,
    table: str = "events",
) -> dict[str, Any]:
    """Keyword / LIKE search with explicit time bounds (no LLM)."""
    from agent.helpers.keywords import keywords_from_query

    start_ts = parse_time_bound(start)
    end_ts = parse_time_bound(end, end_of_day=True) if end is not None else None
    limit = max(1, min(int(limit or 20), 50))
    offset = max(0, int(offset or 0))
    keywords = keywords_from_query(question) if question else []

    if table == "sessions":
        filters = ["summary IS NOT NULL", "summary != ''"]
        params: list[Any] = []
        if start_ts is not None:
            filters.append("window_end >= ?")
            params.append(start_ts)
        if end_ts is not None:
            filters.append("window_start < ?")
            params.append(end_ts)
        like_clauses = []
        for kw in keywords[:8]:
            like_clauses.append("(summary LIKE ? OR active_task LIKE ? OR entities LIKE ?)")
            needle = f"%{kw}%"
            params.extend([needle, needle, needle])
        if like_clauses:
            filters.append("(" + " OR ".join(like_clauses) + ")")
        where = " AND ".join(filters)
        total = conn.execute(f"SELECT COUNT(*) FROM sessions WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT summary_id, session_id, window_start, window_end, summary, active_task, event_count
                FROM sessions WHERE {where}
                ORDER BY window_start DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        results = [
            {
                "summary_id": r[0],
                "session_id": r[1],
                "window_start": r[2],
                "window_end": r[3],
                "summary": (r[4] or "")[:1000],
                "active_task": r[5],
                "event_count": r[6],
            }
            for r in rows
        ]
        return {"ok": True, "table": "sessions", "total": total, "limit": limit, "offset": offset, "results": results}

    # events
    filters = ["1=1"]
    params = []
    if start_ts is not None:
        filters.append("timestamp >= ?")
        params.append(start_ts)
    if end_ts is not None:
        filters.append("timestamp < ?")
        params.append(end_ts)
    like_clauses = []
    for kw in keywords[:8]:
        like_clauses.append(
            "(current_window_title LIKE ? OR active_url LIKE ? OR summary LIKE ? "
            "OR vision_ocr_text LIKE ? OR payload LIKE ? OR process_name LIKE ?)"
        )
        needle = f"%{kw}%"
        params.extend([needle] * 6)
    if like_clauses:
        filters.append("(" + " OR ".join(like_clauses) + ")")
    where = " AND ".join(filters)
    total = conn.execute(f"SELECT COUNT(*) FROM events WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT timestamp, event_type, process_name, current_window_title, active_url,
                   summary, vision_ocr_text, screenshot_filename
            FROM events WHERE {where}
            ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    ).fetchall()
    results = [
        {
            "timestamp": r[0],
            "timestamp_local": datetime.fromtimestamp(r[0]).isoformat(timespec="seconds"),
            "event_type": r[1],
            "process_name": r[2],
            "title": r[3],
            "active_url": r[4],
            "summary": (r[5] or "")[:500],
            "ocr_preview": ((r[6] or "")[:400]),
            "screenshot_filename": r[7],
        }
        for r in rows
    ]
    return {"ok": True, "table": "events", "total": total, "limit": limit, "offset": offset, "results": results}


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False)
