"""Day-card evidence packs: fetch → collapse noisy sessions → compact JSON.

The lab quality gap vs local models was mostly input shape: raw
``list_sessions`` dumps near-duplicate rows. Collapse happens in code so the
LLM (any size) only sees ~N themes, not 40 spam rows.
"""

from __future__ import annotations

import re
from typing import Any

from agent.mcp_query import (
    activity_coverage,
    app_time_summary,
    list_sessions_range,
    list_urls,
    search_with_bounds,
)

# After collapse — roughly the manual Sep-10 budget (~14).
DEFAULT_MAX_THEMES = 14
# Fetch up to SQL hard max, then collapse (do not prompt on all rows).
DEFAULT_SESSION_FETCH = 100
DEFAULT_URL_LIMIT = 30
DEFAULT_TITLE_SAMPLE = 20

_STOP = frozenset(
    "a an the of for to in on at with and or my your their from into "
    "by via using while during about after before over under".split()
)

# UI-chrome / screenshot-viewer rows I dropped by hand on Sep 10.
_NOISE_TASK = re.compile(
    r"^(viewing|looking at|watching|background screenshot|capturing)\b",
    re.IGNORECASE,
)

_FILE_RE = re.compile(
    r"\b([\w.-]+\.(?:ps1|py|js|ts|tsx|jsx|md|json|bat|cmd|psm1|toml|yml|yaml))\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _norm_task(task: str | None) -> str:
    return re.sub(r"\s+", " ", (task or "").strip().lower())


def _task_tokens(task: str | None) -> frozenset[str]:
    text = _norm_task(task)
    files = {m.group(1).lower() for m in _FILE_RE.finditer(text)}
    words = {
        t
        for t in _TOKEN_RE.findall(text)
        if len(t) > 1 and t not in _STOP and not t.isdigit()
    }
    # Possessive / plural light stem for matching ("worth's" already split)
    return frozenset(files | words)


def _files_in(task: str | None) -> frozenset[str]:
    return {m.group(1).lower() for m in _FILE_RE.finditer(task or "")}


def is_noise_session(session: dict[str, Any]) -> bool:
    """True for screenshot-viewer / 'viewing X interface' fluff."""
    task = session.get("active_task") or ""
    if not task.strip():
        return True
    if _NOISE_TASK.match(task.strip()):
        return True
    return False


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _same_theme(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Heuristic used in the local probe: same file, or overlapping task tokens."""
    fa, fb = _files_in(a.get("active_task")), _files_in(b.get("active_task"))
    if fa and fb and fa & fb:
        return True

    ta, tb = a.get("_tokens") or frozenset(), b.get("_tokens") or frozenset()
    if not ta or not tb:
        return False

    # High overlap
    if _jaccard(ta, tb) >= 0.45:
        return True

    # Containment (shorter task is a paraphrase of longer)
    smaller, larger = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(smaller) >= 3 and smaller <= larger:
        return True
    if len(smaller) >= 4 and len(smaller & larger) >= max(3, int(0.7 * len(smaller))):
        return True

    return False


def _ts(session: dict[str, Any], key: str) -> float:
    v = session.get(key)
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _merge_group(members: list[dict[str, Any]]) -> dict[str, Any]:
    """One representative: densest summary; span covers the group."""
    best = max(
        members,
        key=lambda s: (
            int(s.get("event_count") or 0),
            len(s.get("summary") or ""),
            -_ts(s, "window_start"),
        ),
    )
    start = min(_ts(s, "window_start") for s in members)
    end = max(_ts(s, "window_end") or _ts(s, "window_start") for s in members)
    total_events = sum(int(s.get("event_count") or 0) for s in members)

    start_local = best.get("window_start_local")
    end_local = best.get("window_end_local")
    # Prefer locals from extreme members when present
    for s in members:
        if _ts(s, "window_start") == start and s.get("window_start_local"):
            start_local = s["window_start_local"]
        if (_ts(s, "window_end") or _ts(s, "window_start")) == end and s.get(
            "window_end_local"
        ):
            end_local = s["window_end_local"]

    return {
        "start_local": start_local,
        "end_local": end_local,
        "window_start": start,
        "window_end": end,
        "active_task": best.get("active_task"),
        "summary": (best.get("summary") or "")[:800],
        "event_count": total_events,
        "merged_from": len(members),
    }


def collapse_sessions(
    sessions: list[dict[str, Any]],
    *,
    max_themes: int = DEFAULT_MAX_THEMES,
    drop_noise: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collapse near-duplicate session rows into theme rows.

    Mirrors the manual Sep-10 probe: drop viewer fluff, merge paraphrase
    ``active_task`` rows (and same script filename), cap at ``max_themes``.

    Returns (themes, meta).
    """
    raw = list(sessions or [])
    kept: list[dict[str, Any]] = []
    noise_n = 0
    for s in raw:
        if drop_noise and is_noise_session(s):
            noise_n += 1
            continue
        row = dict(s)
        row["_tokens"] = _task_tokens(row.get("active_task"))
        kept.append(row)

    # Greedy clusters in chronological order (oldest first helps span)
    kept.sort(key=lambda s: (_ts(s, "window_start"), _ts(s, "window_end")))
    clusters: list[list[dict[str, Any]]] = []
    for s in kept:
        placed = False
        for cluster in clusters:
            if any(_same_theme(s, m) for m in cluster):
                cluster.append(s)
                placed = True
                break
        if not placed:
            clusters.append([s])

    merged = [_merge_group(c) for c in clusters]

    # Cap: keep densest themes, restore chronological order
    over_cap = max(0, len(merged) - max(1, max_themes))
    if len(merged) > max_themes:
        merged = sorted(
            merged,
            key=lambda s: (int(s.get("event_count") or 0), _ts(s, "window_end")),
            reverse=True,
        )[:max_themes]
        merged.sort(key=lambda s: (_ts(s, "window_start"), _ts(s, "window_end")))

    meta = {
        "raw_sessions": len(raw),
        "dropped_noise": noise_n,
        "themes_before_cap": len(clusters),
        "themes_after_cap": len(merged),
        "capped_away": over_cap,
        "max_themes": max_themes,
    }
    return merged, meta


def _title_sample_for_day(day: str, *, limit: int = DEFAULT_TITLE_SAMPLE) -> list[str]:
    """Distinct window titles when sessions are empty (events-only days)."""
    page = search_with_bounds(
        "window title cursor chrome",
        start=day,
        end=day,
        table="events",
        limit=min(50, max(limit * 2, 20)),
    )
    seen: list[str] = []
    seen_set: set[str] = set()
    for row in page.get("results") or []:
        title = (row.get("title") or row.get("summary") or "").strip()
        if not title:
            continue
        # Skip typing-burst meta titles
        if title.lower().startswith("typed "):
            continue
        key = title.lower()
        if key in seen_set:
            continue
        seen_set.add(key)
        seen.append(title)
        if len(seen) >= limit:
            break
    return seen


def build_day_evidence_pack(
    day: str,
    *,
    max_themes: int = DEFAULT_MAX_THEMES,
    session_fetch_limit: int = DEFAULT_SESSION_FETCH,
    url_limit: int = DEFAULT_URL_LIMIT,
    title_sample_limit: int = DEFAULT_TITLE_SAMPLE,
) -> dict[str, Any]:
    """Assemble the day-card evidence pack (collapsed sessions)."""
    cov = activity_coverage(day, day, bucket_hours=24.0)
    event_count = int(cov.get("total_events") or 0)

    apps = app_time_summary(day, day)
    app_time = [
        {"process": a["process"], "minutes": a["minutes"]}
        for a in (apps.get("apps") or [])
        if a.get("process") and not str(a["process"]).lower().endswith((".jpg", ".png", ".webp"))
    ][:8]

    page = list_sessions_range(day, day, limit=session_fetch_limit, offset=0)
    raw_sessions = page.get("sessions") or []
    themes, collapse_meta = collapse_sessions(raw_sessions, max_themes=max_themes)

    url_page = list_urls(start=day, end=day, limit=url_limit)
    urls = [
        {"url": u["url"], "count": u["count"], "title": u.get("title")}
        for u in (url_page.get("urls") or [])
    ]

    title_sample: list[str] = []
    capture_hint = "session summaries"
    if not themes:
        title_sample = _title_sample_for_day(day, limit=title_sample_limit)
        capture_hint = "events only" if title_sample or app_time else "thin day"
    elif collapse_meta.get("dropped_noise", 0) >= 5 or collapse_meta.get("raw_sessions", 0) >= 25:
        capture_hint = "noisy/duplicate sessions"

    if event_count == 0:
        capture_hint = "thin day"

    return {
        "ok": True,
        "date": day,
        "event_count": event_count,
        "app_time": app_time,
        "sessions": themes,
        "urls": urls,
        "title_sample": title_sample,
        "capture_hint": capture_hint,
        "collapse": collapse_meta,
    }
