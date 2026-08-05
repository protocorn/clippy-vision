import json
import re
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.prefetch.topic_search import cosine_similarity
from core.local_embeddings import embed_text
from core.storage import conn
from core.memory_store import get_profile

MEMORY_TOP_K     = 8
MEMORY_MIN_SIM   = 0.55
CLUSTER_GATE_SIM = 0.38
_MEMORY_OVERVIEW_RE = re.compile(
    r"\b(?:what (?:do|did) you know about me|what do you remember about me|"
    r"what (?:is|do you have) stored|local memory|your memory (?:for|about) me|"
    r"who am i|my (?:profile|preferences|skills|goals|interests)|nothing else|anything else)\b",
    re.IGNORECASE,
)


def _memory_inventory() -> str:
    profile = get_profile()
    identity = profile["identity"]
    introduction = profile["introduction"]
    user_name = profile["name"]
    facts = conn.execute("SELECT COUNT(*) FROM memory_facts WHERE valid_to IS NULL").fetchone()[0]
    conversations = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    screenshots = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'screenshot_analysis'"
    ).fetchone()[0]
    sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    lines = [
        "[personal memory inventory]",
        f"explicit_profile_fields: {len(identity) + (1 if user_name else 0) + (1 if introduction else 0)}",
        f"explicit_semantic_facts: {facts}",
        f"conversation_turns_stored: {conversations}",
        f"activity_events_stored: {events}",
        f"screenshot_events_stored: {screenshots}",
        f"session_summaries_stored: {sessions}",
        "interpretation: Explicit profile facts are user-provided identity or preferences. Activity and screenshots are observed episodic history, not personal facts.",
    ]
    if user_name:
        lines.append(f"display_name: {user_name}")
    if identity:
        lines.append("explicit_profile: " + json.dumps(identity, ensure_ascii=False))
    if introduction:
        lines.append(f"stored_introduction: {introduction}")
    return "\n".join(lines)

def _fetch_memory(q_vec: list) -> str:


    cluster_rows =  conn.execute("""
    SELECT cluster_id, label, description, centroid FROM memory_clusters
    """).fetchall()

    if not cluster_rows:
        return "No semantic memory clusters found."


    surviving_clusters_ids = set()

    for cluster_id, label, description, centroid in cluster_rows:
        if not centroid:

            surviving_clusters_ids.add(cluster_id)
            continue
        centroid = json.loads(centroid)
        if cosine_similarity(q_vec, centroid) >= CLUSTER_GATE_SIM:
            surviving_clusters_ids.add(cluster_id)

    if not surviving_clusters_ids:
        return "No relevant semantic memory clusters found."


    placeholders = ",".join("?" * len(surviving_clusters_ids))
    fact_rows = conn.execute(f"""
    SELECT f.fact_id, f.text, f.vector_embedding, f.cluster_id, c.label, c.description
    FROM memory_facts f
    JOIN memory_clusters c ON f.cluster_id = c.cluster_id
    WHERE f.valid_to IS NULL
    AND f.cluster_id IN ({placeholders})
    """, list(surviving_clusters_ids)).fetchall()

    if not fact_rows:
        return ""

    scored = []
    for fact_id, text, embedding, cluster_id, label, description in fact_rows:
        if not embedding:
            continue

        sim = cosine_similarity(q_vec, json.loads(embedding))

        if sim >= MEMORY_MIN_SIM:
            scored.append((sim, text, cluster_id, label, description))

    if not scored:
        return ""




    scored.sort(key=lambda x: x[0], reverse=True)
    top_k = scored[:MEMORY_TOP_K]

    seen_clusters: dict[str, dict] = {}
    for sim, text, cluster_id, label, description in top_k:
        if cluster_id not in seen_clusters:
            seen_clusters[cluster_id] = {
                "label": label,
                "description": description,
                "facts": [],
                "max_sim": sim,
            }
        seen_clusters[cluster_id]["facts"].append((sim, text))

    clusters_ordered = sorted(
        seen_clusters.values(), key=lambda x: x["max_sim"], reverse=True)


    sections = []
    chars = 0
    for c in clusters_ordered:
        header = f"[{c['label']}] {c['description']}"
        lines  = [f"  - {text}" for sim, text in c["facts"]]
        block  = header + "\n" + "\n".join(lines)
        sections.append(block)
        chars += len(block)
    return "\n\n".join(sections) if sections else ""


def memory_query(query: str = "", q_vec: list | None = None) -> str:
    """Public entry point. Accepts a pre-computed q_vec to avoid a redundant
    embed call when the caller (react_agent) has already embedded the query."""
    if q_vec is None:
        if not query:
            return "memory_query: no query or vector provided."
        try:
            q_vec = embed_text(query)
        except Exception as e:
            return f"memory_query: embedding failed — {e}"
    result = _fetch_memory(q_vec)
    if _MEMORY_OVERVIEW_RE.search(query or ""):
        return _memory_inventory() + "\n\n" + result
    return result if result else "No relevant memory facts found."


def memory_query_from_vec(q_vec: list) -> str:
    """Kept for backwards compatibility — prefer memory_query(q_vec=q_vec)."""
    return memory_query(q_vec=q_vec)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    while True:
        query = input("Query: ").strip()
        if not query:
            break
        print()
        print(memory_query(query))
        print()
