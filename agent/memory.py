import time
import math
from typing import Optional

from core.memory_store import (
    get_identity, get_introduction, get_all_clusters,
    get_active_facts, save_identity_field
)

from core.distil import save_note_to_memory
from core.llm_gateway import gateway, Priority

EMBED_MODEL      = "nomic-embed-text"
MEMORY_TOP_K     = 8     # max facts to inject per turn
MEMORY_MIN_SIM   = 0.30  # floor — below this a fact is unrelated
MAX_MEMORY_CHARS = 2000  # token budget guard


def _cosine_sim(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def semantic_memory_context(user_message: str) -> str:
    """Per-fact retrieval: embed the query, score every active fact individually,
    return the top-K most relevant facts grouped by cluster. Returns empty string
    when nothing clears the floor — caller omits the section."""
    import json
    import os, sys
    _CORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core")
    if _CORE_DIR not in sys.path:
        sys.path.insert(0, _CORE_DIR)
    from storage import conn as _conn

    # Load all active facts with their vectors and cluster labels in one query
    rows = _conn.execute("""
        SELECT f.fact_id, f.text, f.vector_embedding, f.cluster_id,
               c.label, c.description
        FROM memory_facts f
        JOIN memory_clusters c ON c.cluster_id = f.cluster_id
        WHERE f.valid_to IS NULL
    """).fetchall()

    if not rows:
        return ""

    # Embed the query
    try:
        q_vec = gateway.embed(user_message, embed_model=EMBED_MODEL,
                              priority=Priority.INTERACTIVE)
    except Exception:
        return ""

    # Score every fact individually
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

    # Top-K facts by relevance
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:MEMORY_TOP_K]

    # Group by cluster for readable output, preserving relevance order
    seen_clusters: dict[str, dict] = {}
    for sim, text, cluster_id, label, description in top:
        if cluster_id not in seen_clusters:
            seen_clusters[cluster_id] = {
                "label": label, "description": description,
                "facts": [], "max_sim": sim
            }
        seen_clusters[cluster_id]["facts"].append((sim, text))

    # Build output — clusters ordered by their top fact's similarity
    clusters_ordered = sorted(
        seen_clusters.values(), key=lambda c: c["max_sim"], reverse=True
    )
    sections = []
    chars = 0
    for c in clusters_ordered:
        header = f"[{c['label']}] {c['description']}"
        lines  = [f"  - {text}  ({sim:.2f})" for sim, text in c["facts"]]
        block  = header + "\n" + "\n".join(lines)
        if chars + len(block) > MAX_MEMORY_CHARS:
            break
        sections.append(block)
        chars += len(block)

    return "\n\n".join(sections) if sections else ""

def get_autobiographical_context() -> str:
    """Formatted for injection into system prompt."""
    identity = get_identity()
    intro    = get_introduction()
    if not identity and not intro:
        return "No profile data yet. Ask the user to share more about themselves."
    lines = []
    if intro:
        lines.append(intro)
    for field, value in identity.items():
        lines.append(f"{field}: {value}")
    return "\n".join(lines)

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

def save_identity(field: str, value: str, op: str="set", items: Optional[list[str]]=None) -> str:
    return save_identity_field(field, value=value, source="agent", op=op, items=items)

def save_note(note: str) -> str:
    return save_note_to_memory(note)

def delete_note(note_text: str) -> str:
    """Suppress a memory fact whose text matches note_text (case-insensitive substring).
    Marks the fact as valid_to=now so it no longer appears in retrieval."""
    import json, os, sys
    _CORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core")
    if _CORE_DIR not in sys.path:
        sys.path.insert(0, _CORE_DIR)
    from storage import conn as _conn

    needle = note_text.strip().lower()
    rows = _conn.execute(
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
        _conn.execute(
            "UPDATE memory_facts SET valid_to = ? WHERE fact_id = ?",
            (now, fact_id)
        )
    _conn.commit()
    return f"Deleted {len(matched)} memory fact(s) matching '{note_text}'."