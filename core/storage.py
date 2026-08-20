import json
import os
import sqlite3
import time

from core.events import Event
from core.paths import get_db_path

TTL_SUMMARY_DAYS = 90

_DB_PATH = str(get_db_path())
conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=30)
conn.execute("PRAGMA journal_mode=WAL")
conn.commit()


def _table_columns(table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(table: str, column: str, decl: str) -> None:
    """Add a column only if it is missing.

    Duplicate ADD COLUMN is not always a sqlite3.OperationalError. Some
    Windows/SQLite builds raise sqlite3.DatabaseError or SystemError
    ("returned NULL without setting an exception") instead, which used to
    kill API startup.
    """
    if column in _table_columns(table):
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()
    except Exception:
        pass





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
    image_embedding       TEXT,
    image_embedding_model TEXT,
    screenshot_filename   TEXT,

    expires_at            REAL NOT NULL,
    classification_status TEXT DEFAULT 'pending',

    vision_ocr_text          TEXT,
    vision_activity          TEXT,
    vision_suggested_action  TEXT
)
""")
conn.commit()


for _column, _type in (
    ("vector_embedding", "TEXT"),
    ("image_embedding", "TEXT"),
    ("image_embedding_model", "TEXT"),
    ("screenshot_filename", "TEXT"),
    ("classification_status", "TEXT DEFAULT 'pending'"),
    ("vision_ocr_text", "TEXT"),
    ("vision_activity", "TEXT"),
    ("vision_suggested_action", "TEXT"),
):
    _ensure_column("events", _column, _type)




conn.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_events_rag_queue ON events(classification_status, interesting, timestamp)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type_timestamp ON events(event_type, timestamp)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_events_classification_status ON events(classification_status, timestamp)")
conn.commit()





#-------------------------------------#
#---------- EVENTS FTS5 INDEX --------#
#-------------------------------------#
conn.execute("""
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts
    USING fts5(
        event_id UNINDEXED,
        current_window_title,
        active_url,
        summary,
        payload,
        vision_ocr_text,
        content='events',
        content_rowid='rowid'
    )
""")
conn.commit()

conn.executescript("""
CREATE TRIGGER IF NOT EXISTS events_fts_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, event_id, current_window_title, active_url, summary, payload, vision_ocr_text)
    VALUES (new.rowid, new.event_id, new.current_window_title, new.active_url, new.summary, new.payload, new.vision_ocr_text);
END;
CREATE TRIGGER IF NOT EXISTS events_fts_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, event_id, current_window_title, active_url, summary, payload, vision_ocr_text)
    VALUES ('delete', old.rowid, old.event_id, old.current_window_title, old.active_url, old.summary, old.payload, old.vision_ocr_text);
END;
CREATE TRIGGER IF NOT EXISTS events_fts_au AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, event_id, current_window_title, active_url, summary, payload, vision_ocr_text)
    VALUES ('delete', old.rowid, old.event_id, old.current_window_title, old.active_url, old.summary, old.payload, old.vision_ocr_text);
    INSERT INTO events_fts(rowid, event_id, current_window_title, active_url, summary, payload, vision_ocr_text)
    VALUES (new.rowid, new.event_id, new.current_window_title, new.active_url, new.summary, new.payload, new.vision_ocr_text);
END;
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
    vision_enriched   INTEGER DEFAULT 0,
    summary_embedding TEXT
)
""")
conn.commit()

_ensure_column("sessions", "summary_embedding", "TEXT")

conn.execute("""
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts
    USING fts5(
        summary_id UNINDEXED,
        summary,
        active_task,
        entities,
        content='sessions',
        content_rowid='rowid'
    )
""")
conn.commit()

conn.executescript("""
CREATE TRIGGER IF NOT EXISTS sessions_fts_ai AFTER INSERT ON sessions BEGIN
    INSERT INTO sessions_fts(rowid, summary_id, summary, active_task, entities)
    VALUES (new.rowid, new.summary_id, new.summary, new.active_task, new.entities);
END;
CREATE TRIGGER IF NOT EXISTS sessions_fts_ad AFTER DELETE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, summary_id, summary, active_task, entities)
    VALUES ('delete', old.rowid, old.summary_id, old.summary, old.active_task, old.entities);
END;
CREATE TRIGGER IF NOT EXISTS sessions_fts_au AFTER UPDATE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, summary_id, summary, active_task, entities)
    VALUES ('delete', old.rowid, old.summary_id, old.summary, old.active_task, old.entities);
    INSERT INTO sessions_fts(rowid, summary_id, summary, active_task, entities)
    VALUES (new.rowid, new.summary_id, new.summary, new.active_task, new.entities);
END;
""")
conn.commit()

for _fts_table in ("events_fts", "sessions_fts"):
    try:
        conn.execute(f"INSERT INTO {_fts_table}({_fts_table}) VALUES('rebuild')")
    except sqlite3.Error:
        pass
conn.commit()

_ensure_column("sessions", "vision_enriched", "INTEGER DEFAULT 0")





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
    valid_from REAL NOT NULL,
    valid_to   REAL,
    superseded_by TEXT,
    source TEXT,
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
    fact_id_a    TEXT NOT NULL,
    fact_id_b    TEXT NOT NULL,
    cluster_id   TEXT NOT NULL,
    created_at   REAL NOT NULL,
    resolved_at  REAL,
    resolution   TEXT
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
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    vector_embedding TEXT,
    is_summary_chat INTEGER DEFAULT 0
)
""")
conn.commit()





#-------------------------------------#
#-------- USER PROFILE TABLE ---------#
#-------------------------------------#
conn.execute("""
CREATE TABLE IF NOT EXISTS user_profile (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL DEFAULT ''
)
""")
conn.commit()
conn.execute("INSERT OR IGNORE INTO user_profile (id, name) VALUES (1, '')")
conn.commit()


def get_user_name() -> str:
    row = conn.execute("SELECT name FROM user_profile WHERE id = 1").fetchone()
    return row[0] if row else ""


def set_user_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("Display name cannot be empty.")
    if len(name) > 120:
        raise ValueError("Display name is too long.")
    conn.execute(
        "INSERT INTO user_profile (id, name) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
        (name,),
    )
    conn.execute("DELETE FROM memory_meta WHERE key = 'identity.name'")
    conn.commit()
    return name






###########################################
####### HELPERS FOR STORING EVENTS ########
###########################################
def store_event(event: Event):

    from core.app_settings import get_capture_settings
    retention_days = get_capture_settings()["raw_retention_days"]




    #------------------------------------------------------#
    # Vector embedding is a JSON string for now            #
    # but should be changed to a binary blob in the future #
    # ------------------------------------------------------#
    prev = event["previous_window_context"]
    conn.execute(
        """INSERT OR IGNORE INTO events (
            event_id, session_id, timestamp, event_type,
            process_name, current_window_title, active_url,
            previous_process_name, previous_window_title,
            summary, payload,
            interesting, interest_score, interest_reason, vector_embedding,
            image_embedding, image_embedding_model, screenshot_filename,
            expires_at, classification_status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            json.dumps(event.get("image_embedding")) if event.get("image_embedding") else None,
            event.get("image_embedding_model"),
            event.get("screenshot_filename"),
            event["timestamp"] + (retention_days * 24 * 60 * 60),
            "pending"
        )
    )
    conn.commit()





###########################################
####### HELPERS FOR STORING SUMMARY #######
###########################################
def store_summary(summary: dict, vision_enriched: bool = False, embedding: list | None = None):
    conn.execute(
        """INSERT OR REPLACE INTO sessions (
            session_id, summary_id, created_at,
            window_start, window_end,
            summary, active_task, entities,
            event_count, expires_at, vision_enriched, summary_embedding
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            json.dumps(embedding) if embedding else None,
        ),
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
        (since,),
    ).fetchall()
    return [
        {
            "session_id": r[0],
            "summary_id": r[1],
            "created_at": r[2],
            "window_start": r[3],
            "window_end": r[4],
            "summary": r[5],
            "active_task": r[6],
            "entities": r[7],
            "event_count": r[8],
            "expires_at": r[9],
            "vision_enriched": r[10],
        }
        for r in rows
    ]


def get_unsummarized_events(since: float) -> list[dict]:
    rows = conn.execute(
        """SELECT event_id, session_id, timestamp, event_type,
                  process_name, current_window_title,
                  summary, vision_activity, vision_ocr_text,
                  interest_reason
           FROM events
           WHERE timestamp > ?
           AND event_type IN ('context_change', 'screenshot_analysis', 'paste',
                              'clipboard_change', 'typing_burst')
           AND summary IS NOT NULL AND summary != ''
           AND NOT EXISTS (
               SELECT 1 FROM sessions s
               WHERE s.session_id = events.session_id
                 AND events.timestamp >= s.window_start
                 AND events.timestamp <= s.window_end
           )
           ORDER BY timestamp ASC""",
        (since,),
    ).fetchall()
    return [
        {
            "event_id": r[0],
            "session_id": r[1],
            "timestamp": r[2],
            "event_type": r[3],
            "process_name": r[4],
            "window_title": r[5],
            "summary": r[6],
            "vision_activity": r[7],
            "vision_ocr_text": r[8],
            "interest_reason": r[9],
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
        (window_start, window_end),
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
    return [
        {"summary_id": r[0], "window_start": r[1], "window_end": r[2]} for r in rows
    ]


def mark_session_vision_enriched(summary_id: str):
    conn.execute(
        "UPDATE sessions SET vision_enriched = 1 WHERE summary_id = ?", (summary_id,)
    )
    conn.commit()


def get_last_summary_time(session_id: str) -> float:

    # Prefer the current session's last window
    row = conn.execute(
        "SELECT MAX(window_end) FROM sessions WHERE session_id = ?", (session_id,)
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
    from core.app_settings import get_capture_settings

    now = time.time()
    settings = get_capture_settings()
    event_cutoff = now - (settings["raw_retention_days"] * 86400)
    conn.execute(
        "DELETE FROM events WHERE expires_at < ? OR timestamp < ?",
        (now, event_cutoff),
    )
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    conn.commit()


def get_data_stats() -> dict:
    from core.paths import get_data_dir, get_screenshots_dir

    counts = {}
    for table in ("events", "sessions", "conversations", "memory_facts"):
        counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    screenshot_dir = get_screenshots_dir()
    screenshot_files = list(screenshot_dir.glob("*.jpg"))
    screenshot_bytes = 0
    existing_screenshots = 0
    for path in screenshot_files:
        try:
            screenshot_bytes += path.stat().st_size
            existing_screenshots += 1
        except OSError:
            continue
    database_bytes = 0
    for suffix in ("", "-wal", "-shm"):
        path = get_data_dir() / f"events.db{suffix}"
        try:
            database_bytes += path.stat().st_size
        except OSError:
            pass
    return {
        **counts,
        "screenshots": existing_screenshots,
        "screenshot_bytes": screenshot_bytes,
        "database_bytes": database_bytes,
    }


def export_data() -> dict:
    from core.app_settings import get_capture_settings
    from core.memory_store import get_profile
    from core.paths import get_screenshots_dir
    from core.privacy_settings import get_privacy_enabled

    def decode_payload(value):
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"raw": str(value)}

    event_rows = conn.execute(
        """SELECT event_id, session_id, timestamp, event_type, process_name,
                  current_window_title, active_url, summary, payload,
                  interesting, interest_score, interest_reason, vision_ocr_text,
                  vision_activity, vision_suggested_action, screenshot_filename
           FROM events ORDER BY timestamp ASC"""
    ).fetchall()
    conversation_rows = conn.execute(
        """SELECT chat_id, conversation_id, timestamp, role, content,
                  is_summary_chat FROM conversations ORDER BY timestamp ASC"""
    ).fetchall()
    session_rows = conn.execute(
        """SELECT summary_id, session_id, created_at, window_start, window_end,
                  summary, active_task, entities, event_count, vision_enriched
           FROM sessions ORDER BY window_start ASC"""
    ).fetchall()
    fact_rows = conn.execute(
        """SELECT fact_id, cluster_id, text, valid_from, valid_to,
                  superseded_by, source, created_at
           FROM memory_facts ORDER BY created_at ASC"""
    ).fetchall()
    cluster_rows = conn.execute(
        """SELECT cluster_id, label, description, created_at, updated_at, fact_count
           FROM memory_clusters ORDER BY created_at ASC"""
    ).fetchall()
    conflict_rows = conn.execute(
        """SELECT conflict_id, fact_id_a, fact_id_b, created_at, resolved_at, resolution
           FROM memory_conflicts ORDER BY created_at ASC"""
    ).fetchall()
    screenshot_files = []
    for path in sorted(get_screenshots_dir().glob("*.jpg")):
        try:
            screenshot_files.append({"filename": path.name, "bytes": path.stat().st_size})
        except OSError:
            pass
    return {
        "exported_at": time.time(),
        "profile": get_profile(),
        "settings": {
            "capture": get_capture_settings(),
            "privacy": get_privacy_enabled(),
        },
        "events": [
            {
                "event_id": row[0],
                "session_id": row[1],
                "timestamp": row[2],
                "event_type": row[3],
                "process_name": row[4],
                "current_window_title": row[5],
                "active_url": row[6],
                "summary": row[7],
                "payload": decode_payload(row[8]),
                "interesting": bool(row[9]),
                "interest_score": row[10],
                "interest_reason": row[11],
                "vision_ocr_text": row[12],
                "vision_activity": row[13],
                "vision_suggested_action": row[14],
                "screenshot_filename": row[15],
            }
            for row in event_rows
        ],
        "conversations": [
            {
                "chat_id": row[0],
                "conversation_id": row[1],
                "timestamp": row[2],
                "role": row[3],
                "content": row[4],
                "is_summary_chat": bool(row[5]),
            }
            for row in conversation_rows
        ],
        "session_summaries": [
            {
                "summary_id": row[0],
                "session_id": row[1],
                "created_at": row[2],
                "window_start": row[3],
                "window_end": row[4],
                "summary": row[5],
                "active_task": row[6],
                "entities": json.loads(row[7] or "[]"),
                "event_count": row[8],
                "vision_enriched": bool(row[9]),
            }
            for row in session_rows
        ],
        "memory": {
            "facts": [
                {
                    "fact_id": row[0],
                    "cluster_id": row[1],
                    "text": row[2],
                    "valid_from": row[3],
                    "valid_to": row[4],
                    "superseded_by": row[5],
                    "source": row[6],
                    "created_at": row[7],
                }
                for row in fact_rows
            ],
            "clusters": [
                {
                    "cluster_id": row[0],
                    "label": row[1],
                    "description": row[2],
                    "created_at": row[3],
                    "updated_at": row[4],
                    "fact_count": row[5],
                }
                for row in cluster_rows
            ],
            "conflicts": [
                {
                    "conflict_id": row[0],
                    "fact_id_a": row[1],
                    "fact_id_b": row[2],
                    "created_at": row[3],
                    "resolved_at": row[4],
                    "resolution": row[5],
                }
                for row in conflict_rows
            ],
        },
        "screenshots": screenshot_files,
    }


def clear_data(scopes: list[str] | set[str]) -> dict:
    from core.paths import get_screenshots_dir

    requested = {str(scope).strip().lower() for scope in scopes or []}
    if "all" in requested:
        requested.update({"events", "screenshots", "conversations"})
    result = {
        "events": 0,
        "sessions": 0,
        "conversations": 0,
        "screenshots": 0,
        "screenshot_events": 0,
        "memory_facts": 0,
        "memory_clusters": 0,
        "identity_fields": 0,
    }
    if "events" in requested:
        result["events"] = int(conn.execute("DELETE FROM events").rowcount or 0)
        result["sessions"] = int(conn.execute("DELETE FROM sessions").rowcount or 0)
        for table in ("events_fts", "sessions_fts"):
            try:
                conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
            except sqlite3.Error:
                pass
        result["memory_facts"] += int(conn.execute(
            "DELETE FROM memory_facts WHERE source = 'distiller'"
        ).rowcount or 0)
        conn.execute(
            "DELETE FROM memory_meta WHERE key IN ('last_distilled_at', 'profile_version', 'distilled_from_sessions')"
        )
        intro = conn.execute("SELECT value FROM memory_meta WHERE key = 'introduction'").fetchone()
        if intro:
            try:
                if json.loads(intro[0]).get("source") == "distiller":
                    conn.execute("DELETE FROM memory_meta WHERE key = 'introduction'")
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    if "conversations" in requested:
        result["conversations"] = int(conn.execute("DELETE FROM conversations").rowcount or 0)
    if "memory" in requested:
        result["memory_facts"] += int(conn.execute("DELETE FROM memory_facts").rowcount or 0)
        result["memory_clusters"] += int(conn.execute("DELETE FROM memory_clusters").rowcount or 0)
        conn.execute("DELETE FROM memory_conflicts")
        conn.execute(
            "DELETE FROM memory_meta WHERE key IN ('last_distilled_at', 'profile_version', 'distilled_from_sessions')"
        )
        rows = conn.execute(
            "SELECT key, value FROM memory_meta WHERE key LIKE 'identity.%'"
        ).fetchall()
        for key, encoded in rows:
            try:
                source = json.loads(encoded).get("source")
            except (TypeError, ValueError, json.JSONDecodeError):
                source = None
            if source != "user":
                result["identity_fields"] += int(conn.execute(
                    "DELETE FROM memory_meta WHERE key = ?", (key,)
                ).rowcount or 0)
        intro = conn.execute("SELECT value FROM memory_meta WHERE key = 'introduction'").fetchone()
        if intro:
            try:
                source = json.loads(intro[0]).get("source")
            except (TypeError, ValueError, json.JSONDecodeError):
                source = None
            if source != "user":
                conn.execute("DELETE FROM memory_meta WHERE key = 'introduction'")
    if "screenshots" in requested and "events" not in requested:
        result["screenshot_events"] = int(conn.execute(
            "DELETE FROM events WHERE event_type = 'screenshot_analysis'"
        ).rowcount or 0)
        conn.execute(
            """UPDATE events
               SET screenshot_filename = NULL,
                   image_embedding = NULL,
                   image_embedding_model = NULL,
                   vision_ocr_text = NULL,
                   vision_activity = NULL,
                   vision_suggested_action = NULL
               WHERE screenshot_filename IS NOT NULL
                  OR image_embedding IS NOT NULL
                  OR image_embedding_model IS NOT NULL
                  OR vision_ocr_text IS NOT NULL
                  OR vision_activity IS NOT NULL
                  OR vision_suggested_action IS NOT NULL"""
        )
    empty_clusters = int(conn.execute(
        "DELETE FROM memory_clusters WHERE NOT EXISTS (SELECT 1 FROM memory_facts f WHERE f.cluster_id = memory_clusters.cluster_id)"
    ).rowcount or 0)
    result["memory_clusters"] += empty_clusters
    conn.execute(
        """DELETE FROM memory_conflicts
           WHERE fact_id_a NOT IN (SELECT fact_id FROM memory_facts)
              OR fact_id_b NOT IN (SELECT fact_id FROM memory_facts)"""
    )
    remaining_clusters = conn.execute("SELECT cluster_id FROM memory_clusters").fetchall()
    for (cluster_id,) in remaining_clusters:
        vectors = []
        for (encoded,) in conn.execute(
            "SELECT vector_embedding FROM memory_facts WHERE cluster_id = ? AND valid_to IS NULL",
            (cluster_id,),
        ).fetchall():
            try:
                vector = json.loads(encoded)
                if isinstance(vector, list) and vector:
                    vectors.append([float(value) for value in vector])
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if vectors:
            size = min(len(vector) for vector in vectors)
            centroid = [sum(vector[index] for vector in vectors) / len(vectors) for index in range(size)]
            conn.execute(
                "UPDATE memory_clusters SET centroid = ?, fact_count = ?, updated_at = ? WHERE cluster_id = ?",
                (json.dumps(centroid), len(vectors), time.time(), cluster_id),
            )
    conn.commit()
    if "screenshots" in requested:
        for path in get_screenshots_dir().glob("*.jpg"):
            try:
                path.unlink()
                result["screenshots"] += 1
            except OSError:
                pass
    return result
