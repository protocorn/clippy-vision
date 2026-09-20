"""Freshness / staleness scoring for long-term memory facts.

freshness ∈ [0, 1] = decay(age) × source_weight × quality

Used to demote recovered placeholders, OCR pollution, and long-untouched
distilled guesses relative to recent agent-authored facts — without deleting
anything. Callers still require semantic similarity; freshness only re-ranks
and filters listing noise.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

# Half-lives (days): agent notes stay useful longer; distilled guesses rot faster.
_HALF_LIFE_DAYS = {
    "agent": 180.0,
    "distiller": 45.0,
}
_HALF_LIFE_DEFAULT = 60.0

_SOURCE_WEIGHT = {
    "agent": 1.0,
    "distiller": 0.85,
}
_SOURCE_WEIGHT_DEFAULT = 0.75

# Relevance still dominates; freshness breaks ties and sinks junk.
SIM_WEIGHT = 0.72
FRESHNESS_WEIGHT = 0.28

# Clusters below this are hidden from recall listings unless include_stale.
DEFAULT_MIN_CLUSTER_FRESHNESS = 0.20

_PLACEHOLDER_RE = re.compile(
    r"^(unknown|n/?a|none|null|recovered|placeholder|todo|tbd)[\s.]*$",
    re.IGNORECASE,
)
_RECOVERED_LABEL_RE = re.compile(r"^recovered([_\s]|$)", re.IGNORECASE)


def age_days(valid_from: float | None, created_at: float | None = None, *, now: float | None = None) -> float:
    stamp = float(valid_from or created_at or 0.0)
    if stamp <= 0:
        return 3650.0  # ~10y → essentially dead
    return max(0.0, ((now if now is not None else time.time()) - stamp) / 86400.0)


def decay(age: float, source: str | None) -> float:
    half = _HALF_LIFE_DAYS.get((source or "").strip().lower(), _HALF_LIFE_DEFAULT)
    return 0.5 ** (age / half)


def source_weight(source: str | None) -> float:
    return _SOURCE_WEIGHT.get((source or "").strip().lower(), _SOURCE_WEIGHT_DEFAULT)


def quality_factor(
    text: str,
    *,
    cluster_label: str = "",
    in_unresolved_conflict: bool = False,
) -> float:
    """Multiplicative quality gate. Low values bury junk without hard-deleting."""
    q = 1.0
    label = (cluster_label or "").strip()
    if _RECOVERED_LABEL_RE.match(label) or label.lower().startswith("recovered_"):
        q *= 0.12

    cleaned = (text or "").strip()
    if not cleaned:
        return 0.0
    if _PLACEHOLDER_RE.match(cleaned):
        q *= 0.05
    if len(cleaned) < 12:
        q *= 0.35
    if in_unresolved_conflict:
        q *= 0.5
    return max(0.0, min(1.0, q))


def fact_freshness(
    *,
    text: str,
    source: str | None,
    valid_from: float | None,
    created_at: float | None = None,
    cluster_label: str = "",
    in_unresolved_conflict: bool = False,
    now: float | None = None,
) -> float:
    """Return freshness in [0, 1]."""
    d = decay(age_days(valid_from, created_at, now=now), source)
    s = source_weight(source)
    q = quality_factor(
        text,
        cluster_label=cluster_label,
        in_unresolved_conflict=in_unresolved_conflict,
    )
    return max(0.0, min(1.0, d * s * q))


def combined_score(similarity: float, freshness: float) -> float:
    return (SIM_WEIGHT * float(similarity)) + (FRESHNESS_WEIGHT * float(freshness))


def unresolved_conflict_fact_ids(conn, limit: int = 500) -> set[str]:
    rows = conn.execute(
        """SELECT fact_id_a, fact_id_b FROM memory_conflicts
           WHERE resolved_at IS NULL
           LIMIT ?""",
        (limit,),
    ).fetchall()
    ids: set[str] = set()
    for a, b in rows:
        if a:
            ids.add(a)
        if b:
            ids.add(b)
    return ids


def score_fact_row(
    row: dict[str, Any],
    *,
    conflict_ids: set[str] | None = None,
    now: float | None = None,
) -> float:
    fact_id = row.get("fact_id") or ""
    return fact_freshness(
        text=str(row.get("text") or ""),
        source=row.get("source"),
        valid_from=row.get("valid_from"),
        created_at=row.get("created_at"),
        cluster_label=str(row.get("label") or row.get("cluster_label") or ""),
        in_unresolved_conflict=bool(conflict_ids and fact_id in conflict_ids),
        now=now,
    )


def cluster_freshness_summary(
    facts: list[dict[str, Any]],
    *,
    conflict_ids: set[str] | None = None,
    now: float | None = None,
) -> dict[str, float]:
    """Aggregate freshness for a cluster from its active facts."""
    if not facts:
        return {"max": 0.0, "mean": 0.0, "ranking": 0.0}
    scores = [score_fact_row(f, conflict_ids=conflict_ids, now=now) for f in facts]
    mx = max(scores)
    mean = sum(scores) / len(scores)
    ranking = mx * math.log1p(len(facts))
    return {"max": mx, "mean": mean, "ranking": ranking}


def is_recovered_label(label: str) -> bool:
    return bool(_RECOVERED_LABEL_RE.match((label or "").strip()))
