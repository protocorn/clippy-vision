"""
Tier 0 --> Raw events (< 2 hours)
Tier 1 --> Sessions/summaries (< 7 days)
Tier 2 --> Distiller (beyond 7 days)
"""

import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.helpers.time_resolver import TemporalRange, resolve_temporal_range
from agent.prefetch.topic_search import cosine_similarity
from core.storage import conn

EVENT_TIER_MAX_SECONDS = 7200  # 2 hours'
RAW_EVENTS_TTL_DAYS = 7
SESSION_EVENTS_TTL_DAYS = 90
SESSION_TIER_MAX_SECONDS = 604800  # 7 days
DISTILLER_CLUSTER_GATE_SIM = 0.30
DISTILLER_FACT_MIN_SIM = 0.40
MAX_EVENTS = 30
MAX_SESSIONS = 15
MAX_DISTILLER_FACTS = 20
COMPRESSION_THRESHOLD_NARROW = 0.86  # tight window (1-3 days)
COMPRESSION_THRESHOLD_WIDE = 0.78  # broad window (3-7 days)



NOISE_TYPES = "('typing_burst', 'deviation', 'context_change')"






###########################################
############# TIER SELECTOR ###############
###########################################
def select_tier(temporal_range: TemporalRange) -> str:
    now = time.time()
    if temporal_range.start_ts > now:
        return "none"
    raw_ttl_cutoff     = now - RAW_EVENTS_TTL_DAYS * 86400
    session_ttl_cutoff = now - SESSION_EVENTS_TTL_DAYS * 86400
    window_seconds     = temporal_range.end_ts - temporal_range.start_ts
    if (
        window_seconds <= EVENT_TIER_MAX_SECONDS
        and temporal_range.start_ts >= raw_ttl_cutoff
    ):
        return "events"
    if temporal_range.start_ts >= session_ttl_cutoff:
        return "sessions"
    return "distiller"





###########################################
############### COMPRESSOR  ##################
###########################################
def compress_threshold(temporal_range: TemporalRange) -> float:
    window_days = (temporal_range.end_ts - temporal_range.start_ts) / (86400)
    if window_days <= 3:
        return COMPRESSION_THRESHOLD_NARROW
    return COMPRESSION_THRESHOLD_WIDE





###########################################
############# Tier 1: Events ###############
###########################################
def fetch_events(temporal_range: TemporalRange) -> list[dict]:
    now = time.time()
    raw_ttl_cutoff = now - RAW_EVENTS_TTL_DAYS * 24 * 60 * 60

    if temporal_range.end_ts < raw_ttl_cutoff:
        return []

    start_ts = max(temporal_range.start_ts, raw_ttl_cutoff)
    sql = f"""
        SELECT
            timestamp, event_type, process_name,
            current_window_title, active_url,
            summary, vision_activity, vision_ocr_text
        FROM events
        WHERE interesting = 1
          AND timestamp >= ?
          AND timestamp < ?
          AND (
              event_type NOT IN {NOISE_TYPES}
              OR vision_ocr_text IS NOT NULL
          )
        ORDER BY timestamp ASC
        LIMIT {MAX_EVENTS}
    """

    try:
        rows = conn.execute(sql, (start_ts, temporal_range.end_ts)).fetchall()
    except Exception:
        return []
    return [
        {
            "timestamp":      r[0],
            "event_type":     r[1],
            "process_name":   r[2],
            "window_title":   r[3],
            "active_url":     r[4],
            "summary":        r[5],
            "vision_activity": r[6],
            "vision_ocr_text": r[7],
        }
        for r in rows
    ]





###########################################
############# Tier 2: Sessions ############
###########################################
def fetch_sessions(temporal_range: TemporalRange) -> list[dict]:
    sql = """
    SELECT window_start, window_end, summary, active_task, entities, summary_embedding, event_count
    FROM sessions
    WHERE window_start >= ?
      AND window_start < ?
      AND summary IS NOT NULL AND summary != ''
    ORDER BY window_start ASC
    """

    try:
        rows = conn.execute(sql, (temporal_range.start_ts, temporal_range.end_ts)).fetchall()
    except Exception as e:
        print(f"[time_anchor] fetch_sessions error: {e}")
        return []

    return [
        {
            "window_start": r[0],
            "window_end": r[1],
            "summary": r[2],
            "active_task": r[3],
            "entities": r[4],
            "summary_embeddings": json.loads(r[5]) if r[5] else None,
            "event_count": r[6] or 0,
        }
        for r in rows
    ]

def compress_sessions(sessions: list[dict], threshold: float) -> list[dict]:
    """Greedy cluster compression.

    Each unassigned session starts a new group. Any later session with
    cosine similarity >= threshold to the group's first member is absorbed.
    One representative (densest by event_count) is kept per group.
    Sessions without embeddings are kept as-is.
    """
    N = len(sessions)
    assigned = [-1] * N  # -1 means unassigned

    group_id = 0
    for i in range(N):
        if assigned[i] != -1:
            continue

        assigned[i] = group_id
        emb_i = sessions[i]["summary_embeddings"]

        if emb_i:
            for j in range(i + 1, N):
                if assigned[j] == -1:
                    emb_j = sessions[j]["summary_embeddings"]
                    if emb_j and cosine_similarity(emb_i, emb_j) >= threshold:
                        assigned[j] = group_id

        group_id += 1

    groups: dict[int, list[int]] = {}
    for idx, gid in enumerate(assigned):
        groups.setdefault(gid, []).append(idx)

    keep = []
    for indices in groups.values():
        representative = max(
            indices,
            key=lambda idx: (sessions[idx]["event_count"], sessions[idx]["window_start"]),
        )
        keep.append(sessions[representative])

    keep.sort(key=lambda s: s["window_start"])
    return keep


def cap_sessions(sessions: list[dict], limit: int = MAX_SESSIONS) -> list[dict]:
    """If over limit, keep densest sessions then restore chronological order."""
    if len(sessions) <= limit:
        return sessions
    top = sorted(
        sessions,
        key=lambda s: (s["event_count"], s["window_start"]),
        reverse=True,
    )[:limit]
    top.sort(key=lambda s: s["window_start"])
    return top





###########################################
############ Tier 3: Distiller ############
###########################################
def fetch_distiller_facts(temporal_range: TemporalRange, q_vec: list) -> list[dict]:
    cluster_rows = conn.execute(
        "SELECT cluster_id, centroid FROM memory_clusters"
    ).fetchall()

    if not cluster_rows:
        return []

    surviving = set()
    for cluster_id, centroid in cluster_rows:
        if not centroid:
            surviving.add(cluster_id)
            continue
        sim = cosine_similarity(q_vec, json.loads(centroid))
        if sim >= DISTILLER_CLUSTER_GATE_SIM:
            surviving.add(cluster_id)

    if not surviving:
        return []

    placeholders = ",".join("?" * len(surviving))
    fact_rows = conn.execute(
        f"""
        SELECT f.text, f.vector_embedding, f.created_at, f.valid_from, f.valid_to, c.label
        FROM memory_facts f
        JOIN memory_clusters c ON f.cluster_id = c.cluster_id
        WHERE f.source = 'distiller'
          AND f.cluster_id IN ({placeholders})
          AND f.valid_from  <= ?
          AND (f.valid_to IS NULL OR f.valid_to >= ?)
        """,
        [*surviving, temporal_range.end_ts, temporal_range.start_ts],
    ).fetchall()

    if not fact_rows:
        return []

    scored = []
    for text, emb_json, created_at, valid_from, valid_to, label in fact_rows:
        if not emb_json:
            continue
        sim = cosine_similarity(q_vec, json.loads(emb_json))
        if sim >= DISTILLER_FACT_MIN_SIM:
            scored.append({
                "text":       text,
                "sim":        sim,
                "created_at": created_at,
                "label":      label,
            })

    scored.sort(key=lambda x: x["sim"], reverse=True)
    return scored[:MAX_DISTILLER_FACTS]






###########################################
############# Formatting ##################
###########################################
def format_events(events: list[dict], temporal_range: TemporalRange) -> str:
    if not events:
        start_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(temporal_range.start_ts))
        end_str   = time.strftime("%H:%M",           time.localtime(temporal_range.end_ts))
        return f"[events] no activity recorded between {start_str} and {end_str}."

    date_str = time.strftime("%Y-%m-%d", time.localtime(temporal_range.start_ts))
    parts = [f"[raw events — {date_str}, {len(events)} entries]"]

    for e in events:
        ts_str = time.strftime("%H:%M:%S", time.localtime(e["timestamp"]))
        block = [f"time: {ts_str}"]
        if e["process_name"]:
            block.append(f"app: {e['process_name']}")
        if e["window_title"]:
            block.append(f"window: {e['window_title']}")
        if e["active_url"]:
            block.append(f"url: {e['active_url']}")
        if e["vision_activity"]:
            block.append(f"activity: {e['vision_activity']}")
        if e["summary"]:
            block.append(f"summary: {e['summary']}")
        if e["vision_ocr_text"]:
            ocr = e["vision_ocr_text"]
            display = ocr if len(ocr) <= 300 else ocr[:300] + f"… [{len(ocr)} chars]"
            block.append(f"ocr: {display}")
        parts.append("\n".join(block))

    return "\n\n---\n".join(parts)


def format_sessions(sessions: list[dict], temporal_range: TemporalRange, total_before_dedup: int) -> str:
    if not sessions:
        start_str = time.strftime("%Y-%m-%d", time.localtime(temporal_range.start_ts))
        end_str   = time.strftime("%Y-%m-%d", time.localtime(temporal_range.end_ts))
        return f"[sessions] no activity summaries found between {start_str} and {end_str}."
    start_str = time.strftime("%Y-%m-%d", time.localtime(temporal_range.start_ts))
    end_str   = time.strftime("%Y-%m-%d", time.localtime(temporal_range.end_ts))
    header = (
        f"[activity summaries — {start_str} to {end_str}, "
        f"showing {len(sessions)} of {total_before_dedup} sessions]"
    )
    parts = [header]
    for s in sessions:
        ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["window_start"]))
        block = [f"time: {ts_str}"]
        if s["active_task"]:
            block.append(f"task: {s['active_task']}")
        if s["summary"]:
            block.append(f"summary: {s['summary']}")
        if s["entities"]:
            try:
                ents = json.loads(s["entities"])
                if ents:
                    block.append(f"entities: {', '.join(str(e) for e in ents[:10])}")
            except Exception:
                pass
        parts.append("\n".join(block))
    return "\n\n---\n".join(parts)



def format_distiller(facts: list[dict], temporal_range: TemporalRange) -> str:
    start_str = time.strftime("%Y-%m-%d", time.localtime(temporal_range.start_ts))
    end_str   = time.strftime("%Y-%m-%d", time.localtime(temporal_range.end_ts))
    if not facts:
        return (
            f"[distiller] no activity records found for {start_str} to {end_str}.\n"
            f"Note: raw events and session summaries have expired for this period. "
            f"Only high-level memory facts are available beyond 90 days."
        )
    header = (
        f"[activity memory — {start_str} to {end_str}, "
        f"{len(facts)} facts, approximate timestamps]\n"
        f"Note: these are distilled from session summaries. "
        f"Timestamps reflect when the fact was recorded, not exact activity time."
    )

    # Group by label so related facts are presented together
    by_label: dict[str, list[str]] = {}
    for f in facts:
        by_label.setdefault(f["label"], []).append(f["text"])
    sections = [header]
    for label, texts in by_label.items():
        block = f"[{label}]\n" + "\n".join(f"  - {t}" for t in texts)
        sections.append(block)
    return "\n\n".join(sections)





###########################################
############# Time Anchor #################
###########################################
def time_anchor_fetch(temporal_range: TemporalRange, q_vec: list | None = None) -> str:
    tier = select_tier(temporal_range)

    if tier == "none":
        return "No data available for this temporal range (future temporal range)"

    if tier == "events":
        events = fetch_events(temporal_range)
        return format_events(events, temporal_range)

    if tier == "sessions":
        sessions = fetch_sessions(temporal_range)
        total_before_dedup = len(sessions)
        threshold = compress_threshold(temporal_range)
        sessions = compress_sessions(sessions, threshold)
        sessions = cap_sessions(sessions)
        return format_sessions(sessions, temporal_range, total_before_dedup)

    if tier == "distiller":
        if not q_vec:
            return "[distiller] no query vector available — cannot retrieve distiller facts."
        facts = fetch_distiller_facts(temporal_range, q_vec)
        return format_distiller(facts, temporal_range)

    return "Unknown tier"

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from agent.helpers.time_resolver import resolve_temporal_range

    while True:
        query = input("Query: ").strip()
        if not query:
            break
        temporal_range = resolve_temporal_range(query)
        if temporal_range is None:
            print("  -> could not resolve a time range from that query")
            continue
        print(f"  -> tier: {select_tier(temporal_range)}")
        print(f"     range: {temporal_range.phrase!r} [{temporal_range.granularity}]")
        print()
        print(time_anchor_fetch(temporal_range))
        print()
