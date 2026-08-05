import json
import time
from typing import Optional

try:
    from core.storage import conn
except ImportError:
    from storage import conn

def save_identity_field( field: str, value: str, source: str="agent", op: str="set", items: Optional[list[str]]=None) -> str:
    field = (field or "").strip().lower().replace(" ", "_")
    if not field:
        return "Identity field cannot be empty."
    if field == "name" and op in {"set", "override"}:
        from core.storage import set_user_name
        saved = set_user_name(value)
        return f"Saved name: {saved}"
    key = f"identity.{field}"
    existing_row = conn.execute(
        "SELECT value FROM memory_meta WHERE key = ?",
        (key,)
    ).fetchone()
    if existing_row:
        existing = json.loads(existing_row[0])

        if source == "distiller" and existing.get("source") == "agent":
            return f"Skipped — agent-written value for '{field}' is protected."
    else:
        existing = {}


    now = time.time()


    if op == "set":
        current_count = existing.get("mention_count", 0)



        if current_count >=5 and  existing.get("value") != value:
            existing["mention_count"] = current_count + 1
            existing["updated_at"] = now
            conn.execute(
                "INSERT OR REPLACE INTO memory_meta (key, value) VALUES (?, ?)",
                (key, json.dumps(existing))
            )
            conn.commit()
            return f"Kept existing value for '{field}' (mentioned {existing['mention_count']}x). New value '{value}' ignored — use explicit correction to override."
        else:
            payload = {
                "type": "scalar",
                "value": value,
                "mention_count": current_count + 1,
                "source": source,
                "updated_at": now,
                "field_embedding": None,
            }
            conn.execute(
            "INSERT OR REPLACE INTO memory_meta (key, value) VALUES (?, ?)",
            (key, json.dumps(payload))
        )
        conn.commit()
        return f"Saved {field}: {value}"



    elif op == "add_items":

        if not items:
            return f"add_items called for '{field}' with no items."

        if existing.get("type") == "list":
            current_items = existing.get("items", {})
        else:
            current_items = {}



        for item in items:
            item = item.strip().lower()
            if item in current_items:
                current_items[item]["count"] += 1
                current_items[item]["last_seen"] = now
            else:
                current_items[item] = {"count": 1, "added_at": now, "last_seen": now, "active": True}

        payload = {
            "type":            "list",
            "items":           current_items,
            "source":          source,
            "updated_at":      now,
            "field_embedding": None,
        }
        conn.execute(
            "INSERT OR REPLACE INTO memory_meta (key, value) VALUES (?, ?)",
            (key, json.dumps(payload))
        )
        conn.commit()
        return f"Added to {field}: {', '.join(items)}"



    elif op == "remove_items":
        if not items or existing.get("type") != "list":
            return f"Nothing to remove from '{field}'."
        current_items = existing.get("items", {})
        removed = []
        for item in items:
            item = item.strip().lower()
            if item in current_items:
                current_items[item]["active"] = False
                removed.append(item)
        existing["items"] = current_items
        existing["updated_at"] = now
        existing["field_embedding"] = None
        conn.execute(
            "INSERT OR REPLACE INTO memory_meta (key, value) VALUES (?, ?)",
            (key, json.dumps(existing))
        )
        conn.commit()
        return f"Removed from {field}: {', '.join(removed)}"


    elif op == "override":
        payload = {
            "type":            "scalar",
            "value":           value,
            "mention_count":   1,
            "source":          source,
            "updated_at":      now,
            "field_embedding": None,
        }
        conn.execute(
            "INSERT OR REPLACE INTO memory_meta (key, value) VALUES (?, ?)",
            (key, json.dumps(payload))
        )
        conn.commit()
        return f"Overrode {field}: {value}"
    return f"Unknown op '{op}' for field '{field}'."

def get_identity() -> dict:
    """Return all identity fields as {field: display_string} dict."""
    rows = conn.execute(
        "SELECT key, value FROM memory_meta WHERE key LIKE 'identity.%'"
    ).fetchall()
    result = {}
    for key, val in rows:
        field = key[len("identity."):]
        if field == "name":
            continue
        data = json.loads(val)

        if data.get("type") == "list":

            active = {
                k: v for k, v in data.get("items", {}).items()
                if v.get("active", True)
            }
            sorted_items = sorted(active.keys(), key=lambda k: active[k]["count"], reverse=True)
            result[field] = ", ".join(sorted_items) if sorted_items else ""
        else:
            result[field] = data.get("value", "")


    return {k: v for k, v in result.items() if v}


def get_profile() -> dict:
    from core.storage import get_user_name
    return {
        "name": get_user_name(),
        "introduction": get_introduction(),
        "identity": get_identity(),
    }

def get_introduction_meta() -> dict:
    """Return introduction payload: {value, source, updated_at}. Empty defaults if missing."""
    row = conn.execute(
        "SELECT value FROM memory_meta WHERE key = 'introduction'"
    ).fetchone()
    if not row:
        return {"value": "", "source": "", "updated_at": 0.0}
    data = json.loads(row[0])
    return {
        "value": data.get("value", "") or "",
        "source": data.get("source", "") or "",
        "updated_at": float(data.get("updated_at") or 0),
    }


def get_introduction() -> str:
    return get_introduction_meta()["value"]

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


def count_identity_updates_since(since: float) -> int:
    """Count identity.* fields whose updated_at is strictly after `since`."""
    rows = conn.execute(
        "SELECT value FROM memory_meta WHERE key LIKE 'identity.%'"
    ).fetchall()
    n = 0
    for (val,) in rows:
        data = json.loads(val)
        if float(data.get("updated_at") or 0) > since:
            n += 1
    return n


def get_fact_delta_since(since: float, limit: int = 40) -> list[str]:
    """Active memory facts created after `since`, newest first, capped."""
    rows = conn.execute(
        """SELECT text FROM memory_facts
           WHERE valid_to IS NULL AND created_at > ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (since, limit),
    ).fetchall()
    return [r[0] for r in rows]


def count_facts_since(since: float) -> int:
    row = conn.execute(
        """SELECT COUNT(*) FROM memory_facts
           WHERE valid_to IS NULL AND created_at > ?""",
        (since,),
    ).fetchone()
    return int(row[0]) if row else 0

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


def get_unresolved_conflicts(limit: int = 3) -> list[dict]:
    """Return unresolved memory conflicts with both fact texts for agent injection."""
    rows = conn.execute(
        """SELECT mc.conflict_id, f_a.text, f_b.text
           FROM memory_conflicts mc
           JOIN memory_facts f_a ON f_a.fact_id = mc.fact_id_a
           JOIN memory_facts f_b ON f_b.fact_id = mc.fact_id_b
           WHERE mc.resolved_at IS NULL
           ORDER BY mc.created_at DESC
           LIMIT ?""",
        (limit,)
    ).fetchall()
    return [{"conflict_id": r[0], "fact_a": r[1], "fact_b": r[2]} for r in rows]


def resolve_conflicts_for_fact(fact_id: str, resolution: str) -> None:
    """Mark all unresolved conflicts involving this fact as resolved.
    Called when the user explicitly overrides a fact via save_identity op='override'."""
    conn.execute(
        """UPDATE memory_conflicts
           SET resolved_at = ?, resolution = ?
           WHERE (fact_id_a = ? OR fact_id_b = ?) AND resolved_at IS NULL""",
        (time.time(), resolution, fact_id, fact_id)
    )
    conn.commit()


def get_identity_for_semantic_profile() -> list[dict]:
    """Return all non-empty identity fields with their cached embeddings.

    Each entry: {"field": str, "display": str, "embedding": list|None}
    embedding is None when the field has been written but not yet embedded (dirty).
    """
    rows = conn.execute(
        "SELECT key, value FROM memory_meta WHERE key LIKE 'identity.%'"
    ).fetchall()
    result = []
    for key, val in rows:
        field = key[len("identity."):]
        if field == "name":
            continue
        data  = json.loads(val)
        if data.get("type") == "list":
            active = {k: v for k, v in data.get("items", {}).items() if v.get("active", True)}
            sorted_items = sorted(active.keys(), key=lambda k: active[k]["count"], reverse=True)
            display = ", ".join(sorted_items) if sorted_items else ""
        else:
            display = data.get("value", "")
        if not display:
            continue
        result.append({
            "field":     field,
            "display":   display,
            "embedding": data.get("field_embedding"),
        })
    return result


def update_field_embedding(field: str, embedding: list) -> None:
    """Persist a freshly computed embedding back into the identity field's JSON blob."""
    key = f"identity.{field}"
    row = conn.execute("SELECT value FROM memory_meta WHERE key = ?", (key,)).fetchone()
    if not row:
        return
    data = json.loads(row[0])
    data["field_embedding"] = embedding
    conn.execute(
        "INSERT OR REPLACE INTO memory_meta (key, value) VALUES (?, ?)",
        (key, json.dumps(data))
    )
    conn.commit()
