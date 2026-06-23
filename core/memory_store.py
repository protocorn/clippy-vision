import os
import sys
import sqlite3
import json
import time

# Ensure core/ is on sys.path so 'storage' can be found whether this module
# is imported as 'storage' (from core/) or as 'core.memory_store' (from root).
_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

from storage import conn

def save_identity_field( field: str, value: str, source: str="agent") -> str:
    existing_row = conn.execute(
        "SELECT value FROM memory_meta WHERE key = ?",
        (f"identity.{field}",)
    ).fetchone()
    if existing_row:
        existing = json.loads(existing_row[0])
        # distiller never overwrites an agent-written field
        if source == "distiller" and existing.get("source") == "agent":
            return f"Skipped — agent-written value for '{field}' is protected."
    conn.execute(
        "INSERT OR REPLACE INTO memory_meta (key, value) VALUES (?, ?)",
        (f"identity.{field}", json.dumps({
            "value": value,
            "source": source,
            "updated_at": time.time()
        }))
    )
    conn.commit()
    return f"Saved {field}: {value}"

def get_identity() -> dict:
    """Return all identity fields as {field: value} dict."""
    rows = conn.execute(
        "SELECT key, value FROM memory_meta WHERE key LIKE 'identity.%'"
    ).fetchall()
    result = {}
    for key, val in rows:
        field = key[len("identity."):]
        result[field] = json.loads(val)["value"]
    return result

def get_introduction() -> str:
    row = conn.execute(
        "SELECT value FROM memory_meta WHERE key = 'introduction'"
    ).fetchone()
    if not row:
        return ""
    return json.loads(row[0]).get("value", "")

def set_introduction(text: str, source: str = "distiller") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO memory_meta (key, value) VALUES (?, ?)",
        ("introduction", json.dumps({
            "value": text,
            "source": source,
            "updated_at": time.time()
        }))
    )
    conn.commit()

def get_active_facts(cluster_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT text FROM memory_facts WHERE cluster_id = ? AND valid_to IS NULL ORDER BY created_at ASC",
        (cluster_id,)
    ).fetchall()
    return [r[0] for r in rows]

def get_all_clusters() -> list[dict]:
    rows = conn.execute(
        "SELECT cluster_id, label, description, fact_count FROM memory_clusters ORDER BY fact_count DESC"
    ).fetchall()
    return [
        {"cluster_id": r[0], "label": r[1], "description": r[2], "fact_count": r[3]}
        for r in rows
    ]
