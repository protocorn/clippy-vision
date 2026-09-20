import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.prefetch.topic_search import cosine_similarity
from core.local_embeddings import embed_text
from core.memory_freshness import (
    combined_score,
    fact_freshness,
    unresolved_conflict_fact_ids,
)
from core.storage import conn

MEMORY_TOP_K     = 8
MEMORY_MIN_SIM   = 0.55
CLUSTER_GATE_SIM = 0.38

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
    SELECT f.fact_id, f.text, f.vector_embedding, f.cluster_id, c.label, c.description,
           f.source, f.valid_from, f.created_at
    FROM memory_facts f
    JOIN memory_clusters c ON f.cluster_id = c.cluster_id
    WHERE f.valid_to IS NULL
    AND f.cluster_id IN ({placeholders})
    """, list(surviving_clusters_ids)).fetchall()

    if not fact_rows:
        return ""

    conflict_ids = unresolved_conflict_fact_ids(conn)
    now = time.time()
    scored = []
    for fact_id, text, embedding, cluster_id, label, description, source, valid_from, created_at in fact_rows:
        if not embedding:
            continue
        sim = cosine_similarity(q_vec, json.loads(embedding))
        if sim >= MEMORY_MIN_SIM:
            fresh = fact_freshness(
                text=text,
                source=source,
                valid_from=valid_from,
                created_at=created_at,
                cluster_label=label,
                in_unresolved_conflict=fact_id in conflict_ids,
                now=now,
            )
            score = combined_score(sim, fresh)
            scored.append((score, sim, fresh, text, cluster_id, label, description))

    if not scored:
        return ""

    scored.sort(key=lambda x: x[0], reverse=True)
    top_k = scored[:MEMORY_TOP_K]

    seen_clusters: dict[str, dict] = {}
    for score, sim, fresh, text, cluster_id, label, description in top_k:
        if cluster_id not in seen_clusters:
            seen_clusters[cluster_id] = {
                "label": label,
                "description": description,
                "facts": [],
                "max_score": score,
            }
        seen_clusters[cluster_id]["facts"].append((score, text, fresh))

    clusters_ordered = sorted(
        seen_clusters.values(), key=lambda x: x["max_score"], reverse=True)

    sections = []
    for c in clusters_ordered:
        header = f"[{c['label']}] {c['description']}"
        lines  = [f"  - {text}" for score, text, fresh in c["facts"]]
        block  = header + "\n" + "\n".join(lines)
        sections.append(block)
    return "\n\n".join(sections) if sections else ""


def memory_query(query: str = "", q_vec: list | None = None) -> str:
    """Public entry point. Accepts a pre-computed q_vec to avoid a redundant
    embed call when the caller has already embedded the query."""
    if q_vec is None:
        if not query:
            return "memory_query: no query or vector provided."
        try:
            q_vec = embed_text(query)
        except Exception as e:
            return f"memory_query: embedding failed — {e}"
    result = _fetch_memory(q_vec)
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
