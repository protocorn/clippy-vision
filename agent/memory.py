import json
import time
import math
from typing import Optional

from core.memory_store import (
    get_identity, get_introduction, get_all_clusters, get_profile,
    get_active_facts, save_identity_field,
    get_identity_for_semantic_profile, update_field_embedding,
)

from core.distil import save_note_to_memory
from core.local_embeddings import embed_text
from core.storage import conn

MEMORY_TOP_K     = 8
MEMORY_MIN_SIM   = 0.55
MAX_MEMORY_CHARS = 2000


_PROFILE_ALWAYS_ON = {"name", "location"}
_PROFILE_TOP_K     = 5
_PROFILE_MIN_SIM   = 0.25


def _cosine_sim(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def semantic_memory_context_from_vec(q_vec: list) -> str:
    """Same as semantic_memory_context but accepts a pre-computed query vector.
    Use this when the caller has already embedded the query to avoid a second embed call."""
    rows = conn.execute("""
        SELECT f.fact_id, f.text, f.vector_embedding, f.cluster_id,
               c.label, c.description
        FROM memory_facts f
        JOIN memory_clusters c ON c.cluster_id = f.cluster_id
        WHERE f.valid_to IS NULL
    """).fetchall()

    if not rows:
        return ""

    scored = []
    for fact_id, text, vec_json, cluster_id, label, description in rows:
        if not vec_json:
            continue
        f_vec = json.loads(vec_json)
        sim = _cosine_sim(q_vec, f_vec)
        if sim >= MEMORY_MIN_SIM:
            scored.append((sim, text, cluster_id, label, description))

    if not scored:
        return ""

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:MEMORY_TOP_K]

    seen_clusters: dict[str, dict] = {}
    for sim, text, cluster_id, label, description in top:
        if cluster_id not in seen_clusters:
            seen_clusters[cluster_id] = {
                "label": label, "description": description,
                "facts": [], "max_sim": sim
            }
        seen_clusters[cluster_id]["facts"].append((sim, text))

    clusters_ordered = sorted(
        seen_clusters.values(), key=lambda c: c["max_sim"], reverse=True
    )
    sections = []
    chars = 0
    for c in clusters_ordered:
        header = f"[{c['label']}] {c['description']}"
        lines  = [f"  - {text}" for sim, text in c["facts"]]
        block  = header + "\n" + "\n".join(lines)
        if chars + len(block) > MAX_MEMORY_CHARS:
            break
        sections.append(block)
        chars += len(block)

    return "\n\n".join(sections) if sections else ""

def get_autobiographical_context(q_vec: list | None = None) -> str:
    """Formatted for injection into system prompt.

    When q_vec is provided, identity fields are ranked by cosine similarity to the
    current query. Always-on fields (name, location) are always included. Up to
    _PROFILE_TOP_K additional fields are selected by relevance. Dirty fields
    (newly written, no cached embedding) are re-embedded and cached on the fly.

    When q_vec is None (no embedding available), all fields are returned as before.
    """
    profile = get_profile()
    intro = profile["introduction"]
    user_name = profile["name"]


    if q_vec is None:
        identity = profile["identity"]
        if not identity and not intro and not user_name:
            return "No profile data yet. Ask the user to share more about themselves."
        lines = []
        if user_name:
            lines.append(f"name: {user_name}")
        if intro:
            lines.append(intro)
        for field, value in identity.items():
            lines.append(f"{field}: {value}")
        return "\n".join(lines)


    fields = get_identity_for_semantic_profile()
    if not fields and not intro and not user_name:
        return "No profile data yet. Ask the user to share more about themselves."


    for f in fields:
        if f["embedding"] is None:
            try:
                emb = embed_text(f"{f['field']}: {f['display']}")
                f["embedding"] = emb
                update_field_embedding(f["field"], emb)
            except Exception:
                pass

    always_on: list[dict] = []
    scoreable: list[tuple[float, dict]] = []

    for f in fields:
        if f["field"] in _PROFILE_ALWAYS_ON:
            always_on.append(f)
        elif f["embedding"] is not None:
            sim = _cosine_sim(q_vec, f["embedding"])
            if sim >= _PROFILE_MIN_SIM:
                scoreable.append((sim, f))

    scoreable.sort(key=lambda x: x[0], reverse=True)
    top_fields = [f for _, f in scoreable[:_PROFILE_TOP_K]]

    lines = []
    if user_name:
        lines.append(f"name: {user_name}")
    if intro:
        lines.append(intro)
    for f in always_on:
        lines.append(f"{f['field']}: {f['display']}")
    for f in top_fields:
        lines.append(f"{f['field']}: {f['display']}")

    return "\n".join(lines) if lines else "No profile data yet. Ask the user to share more about themselves."

def recall_memory() -> str:
    """List clusters for the recall_memory tool."""
    clusters = get_all_clusters()
    if not clusters:
        return "No memory clusters yet."
    lines = ["Memory clusters:"]
    for c in clusters:
        lines.append(f"  [{c['label']}] {c['description']} ({c['fact_count']} facts)")
    return "\n".join(lines)

def fetch_cluster(label: str) -> str:
    """Get all facts in a named cluster."""
    clusters = get_all_clusters()
    match = next((c for c in clusters if c["label"].lower() == label.strip().lower()), None)
    if not match:
        return f"No cluster found with label '{label}'."
    facts = get_active_facts(match["cluster_id"])
    if not facts:
        return f"Cluster '{label}' exists but has no active facts."
    return "\n".join(f"- {f}" for f in facts)

def save_identity(field: str, value: str = "", op: str = "set", items: Optional[list[str]] = None) -> str:
    return save_identity_field(field, value=value, source="agent", op=op, items=items)

def save_note(note: str) -> str:
    return save_note_to_memory(note)

def delete_note(note_text: str) -> str:
    """Suppress a memory fact whose text matches note_text (case-insensitive substring).
    Marks the fact as valid_to=now so it no longer appears in retrieval."""
    needle = note_text.strip().lower()
    rows = conn.execute(
        "SELECT fact_id, text FROM memory_facts WHERE valid_to IS NULL"
    ).fetchall()

    matched = [
        fact_id for fact_id, text in rows
        if needle in text.lower()
    ]

    if not matched:
        return f"No active memory found matching: '{note_text}'"

    now = time.time()
    for fact_id in matched:
        conn.execute(
            "UPDATE memory_facts SET valid_to = ? WHERE fact_id = ?",
            (now, fact_id)
        )
    conn.commit()
    return f"Deleted {len(matched)} memory fact(s) matching '{note_text}'."
