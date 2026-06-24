import os
import sqlite3
import json
import time
from events import Event

TTL_RAW_DAYS = 7
TTL_SUMMARY_DAYS = 90

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "events.db")
conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=30)
conn.execute("PRAGMA journal_mode=WAL")
conn.commit()

#-------------------------------------#
#----------- EVENTS TABLE ------------#
#-------------------------------------#

conn.execute("""
CREATE TABLE IF NOT EXISTS events (
    event_id              TEXT PRIMARY KEY,
    session_id            TEXT NOT NULL,
    timestamp             REAL NOT NULL,
    event_type            TEXT NOT NULL,

    process_name          TEXT,
    current_window_title  TEXT,
    active_url            TEXT,
    previous_process_name TEXT,
    previous_window_title TEXT,

    summary               TEXT,
    payload               TEXT,

    interesting           INTEGER,
    interest_score        REAL,
    interest_reason       TEXT,
    vector_embedding      TEXT,

    expires_at            REAL NOT NULL,
    classification_status TEXT DEFAULT 'pending',

    vision_ocr_text          TEXT,
    vision_activity          TEXT,
    vision_suggested_action  TEXT
)
""")
conn.commit()

#-------------------------------------#
#---------- SUMMARY TABLE ------------#
#-------------------------------------#

conn.execute("""
CREATE TABLE IF NOT EXISTS sessions(
    session_id        TEXT NOT NULL,
    summary_id        TEXT PRIMARY KEY,
    created_at        REAL NOT NULL,
    window_start      REAL NOT NULL,
    window_end        REAL NOT NULL,
    summary           TEXT NOT NULL,
    active_task       TEXT,
    entities          TEXT,
    event_count       INTEGER,
    expires_at        REAL NOT NULL,
    vision_enriched   INTEGER DEFAULT 0
)
""")
conn.commit()

try:
    conn.execute("ALTER TABLE sessions ADD COLUMN vision_enriched INTEGER DEFAULT 0")
    conn.commit()
except sqlite3.OperationalError:
    pass  # column already exists

#-------------------------------------#
#--------- MEMORY TABLES -------------#
#-------------------------------------#

conn.execute("""
CREATE TABLE IF NOT EXISTS memory_clusters (
    cluster_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    description TEXT,
    centroid TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    fact_count INTEGER DEFAULT 0
)
""")
conn.commit()

conn.execute("""
CREATE TABLE IF NOT EXISTS memory_facts (
    fact_id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    text TEXT NOT NULL,
    vector_embedding TEXT NOT NULL,
    valid_from REAL NOT NULL,        -- when this fact was created
    valid_to   REAL,                 -- NULL = still current; set when superseded
    superseded_by TEXT,              -- fact_id of the replacement, if any
    source TEXT,                      -- where this fact came from (e.g. "distiller", "agent")
    created_at REAL NOT NULL,
    FOREIGN KEY (cluster_id) REFERENCES memory_clusters(cluster_id)
)
""")
conn.commit()

conn.execute("""
CREATE TABLE IF NOT EXISTS memory_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""")
conn.commit()

#-------------------------------------#
#-------- CONFLICT TABLE ------------#
#-------------------------------------#

conn.execute("""
CREATE TABLE IF NOT EXISTS memory_conflicts (
    conflict_id  TEXT PRIMARY KEY,
    fact_id_a    TEXT NOT NULL,   -- existing fact that was already stored
    fact_id_b    TEXT NOT NULL,   -- incoming fact that contradicts it
    cluster_id   TEXT NOT NULL,
    created_at   REAL NOT NULL,
    resolved_at  REAL,            -- NULL = unresolved
    resolution   TEXT             -- 'kept_a', 'kept_b', 'dismissed'
)
""")
conn.commit()

#-------------------------------------#
#------- CONVERSATION TABLE ----------#
#-------------------------------------#

conn.execute("""
CREATE TABLE IF NOT EXISTS conversations (
    chat_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    role TEXT NOT NULL, -- user or assistant
    content TEXT NOT NULL,
    vector_embedding TEXT,
    is_summary_chat INTEGER DEFAULT 0 -- 1 if the chat is a rolling summary
)
""")
conn.commit()
###########################################
####### HELPERS FOR STORING EVENTS ########
###########################################

def store_event(event: Event):
    #------------------------------------------------------#
    # Vector embedding is a JSON string for now            #
    # but should be changed to a binary blob in the future #
    #------------------------------------------------------#
    prev = event["previous_window_context"]
    conn.execute(
        """INSERT OR IGNORE INTO events (
            event_id, session_id, timestamp, event_type,
            process_name, current_window_title, active_url,
            previous_process_name, previous_window_title,
            summary, payload,
            interesting, interest_score, interest_reason, vector_embedding,
            expires_at, classification_status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event["event_id"],
            event["session_id"],
            event["timestamp"],
            event["event_type"],

            event["window_context"]["process_name"],
            event["window_context"]["current_window_title"],
            event["window_context"]["active_url"],
            prev["process_name"] if prev else None,
            prev["current_window_title"] if prev else None,

            event["summary"],
            json.dumps(event["payload"]),

            event["interesting"],
            event["interest_score"],
            event["interest_reason"],
            json.dumps(event["vector_embedding"]) if event["vector_embedding"] else None,
            event["timestamp"] + (TTL_RAW_DAYS * 24 * 60 * 60),
            "pending"
        )
    )
    conn.commit()

###########################################
####### HELPERS FOR STORING SUMMARY #######
###########################################

def store_summary(summary: dict, vision_enriched: bool = False):
    conn.execute(
        """INSERT OR REPLACE INTO sessions (
            session_id, summary_id, created_at,
            window_start, window_end,
            summary, active_task, entities,
            event_count, expires_at, vision_enriched
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            summary["session_id"],
            summary["summary_id"],
            summary["created_at"],
            summary["window_start"],
            summary["window_end"],
            summary["summary"],
            summary.get("active_task"),
            json.dumps(summary.get("entities", [])),
            summary["event_count"],
            summary["created_at"] + (TTL_SUMMARY_DAYS * 24 * 60 * 60),
            1 if vision_enriched else 0,
        )
    )
    conn.commit()

def get_summaries(since: float) -> list[dict]:
    rows = conn.execute(
        """SELECT session_id, summary_id, created_at,
                  window_start, window_end, summary, active_task, entities,
                  event_count, expires_at, vision_enriched
        FROM sessions
        WHERE created_at > ?
        ORDER BY created_at ASC""",
        (since,)
    ).fetchall()
    return [{"session_id": r[0], "summary_id": r[1], "created_at": r[2], "window_start": r[3], "window_end": r[4], "summary": r[5], "active_task": r[6], "entities": r[7], "event_count": r[8], "expires_at": r[9], "vision_enriched": r[10]} for r in rows]

def get_unsummarized_events(since: float) -> list[dict]:
    rows = conn.execute(
        """SELECT event_id, timestamp, event_type,
                  process_name, current_window_title,
                  summary, vision_activity, vision_ocr_text,
                  interest_reason
           FROM events
           WHERE interesting = 1
           AND timestamp > ?
           ORDER BY timestamp ASC""",
        (since,)
    ).fetchall()
    return [
        {
            "event_id": r[0],
            "timestamp": r[1],
            "event_type": r[2],
            "process_name": r[3],
            "window_title": r[4],
            "summary": r[5],
            "vision_activity": r[6],
            "vision_ocr_text": r[7],
            "interest_reason": r[8],
        }
        for r in rows
    ]


def get_events_for_window(window_start: float, window_end: float) -> list[dict]:
    rows = conn.execute(
        """SELECT event_id, timestamp, event_type,
                  process_name, current_window_title,
                  summary, vision_activity, vision_ocr_text,
                  interest_reason
           FROM events
           WHERE interesting = 1
           AND timestamp BETWEEN ? AND ?
           ORDER BY timestamp ASC""",
        (window_start, window_end)
    ).fetchall()
    return [
        {
            "event_id": r[0],
            "timestamp": r[1],
            "event_type": r[2],
            "process_name": r[3],
            "window_title": r[4],
            "summary": r[5],
            "vision_activity": r[6],
            "vision_ocr_text": r[7],
            "interest_reason": r[8],
        }
        for r in rows
    ]


def get_sessions_needing_refresh() -> list[dict]:
    """Sessions summarized before vision finished that now have vision data available.
    Searches across all sessions (not just current) so restarts don't orphan stale sessions."""
    rows = conn.execute(
        """SELECT s.summary_id, s.window_start, s.window_end
           FROM sessions s
           WHERE s.vision_enriched = 0
           AND EXISTS (
               SELECT 1 FROM events e
               WHERE e.timestamp BETWEEN s.window_start AND s.window_end
               AND e.interesting = 1
               AND e.vision_ocr_text IS NOT NULL
           )""",
    ).fetchall()
    return [{"summary_id": r[0], "window_start": r[1], "window_end": r[2]} for r in rows]


def mark_session_vision_enriched(summary_id: str):
    conn.execute("UPDATE sessions SET vision_enriched = 1 WHERE summary_id = ?", (summary_id,))
    conn.commit()


def get_last_summary_time(session_id: str) -> float:
    # Prefer the current session's last window
    row = conn.execute(
        "SELECT MAX(window_end) FROM sessions WHERE session_id = ?",
        (session_id,)
    ).fetchone()
    if row and row[0]:
        return row[0]
    # On a fresh session (e.g. after restart), continue from wherever
    # any previous session left off so we don't re-summarize old events
    row = conn.execute("SELECT MAX(window_end) FROM sessions").fetchone()
    return row[0] if row and row[0] else time.time() - 3600

###########################################
########### DELETE EXPIRED DATA ###########
###########################################

def purge_expired():
    conn.execute("DELETE FROM events WHERE expires_at < ?", (time.time(),))
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    conn.commit()
