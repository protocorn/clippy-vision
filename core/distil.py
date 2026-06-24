import time
import json
import math
import uuid
from core.storage import conn, get_summaries
from core.memory_store import save_identity_field
from typing import Optional

from core.llm_gateway import gateway, Priority

CLUSTER_THRESHOLD = 0.75
DISTIL_EVERY_N_SESSIONS = 5 # change to 5 for production
SESSION_GAP_SECONDS    = 30 * 60 # change to 30 * 60 for production
SESSION_MAX_SUMMARIES  = 20 # change to 20 for production

MODEL                  = "qwen3:8b"
EMBED_MODEL            = "nomic-embed-text"


def count_sessions_since_last_distil() -> int:
    
    last_distilled_at = _get_meta("last_distilled_at", 0) 
    summaries = get_summaries(last_distilled_at)

    if not summaries:
        return 0

    # Start at 1 — the first summary is itself the start of the first session
    sessions_count = 1
    previous_window_end = summaries[0]["window_end"]
    summaries_in_session = 1

    for summary in summaries[1:]:
        gap = summary["window_start"] - previous_window_end
        length_cap = summaries_in_session >= SESSION_MAX_SUMMARIES
        if gap > SESSION_GAP_SECONDS or length_cap:
            sessions_count += 1
            summaries_in_session = 1
        else:
            summaries_in_session += 1
        # Always advance the pointer regardless of whether there was a gap
        previous_window_end = summary["window_end"]

    return sessions_count

def _get_meta(key: str, default=None):
    row = conn.execute(
        "SELECT value FROM memory_meta WHERE key = ?", (key,)
    ).fetchone()
    return json.loads(row[0]) if row else default

def _set_meta(key: str, value):
    conn.execute(
        "INSERT OR REPLACE INTO memory_meta (key, value) VALUES (?, ?)",
        (key, json.dumps(value))
    )
    conn.commit()

def save_note_to_memory(note: str) -> str:
    """Route a user-written note directly into the memory cluster system.
    Runs synchronously since it's called from an agent tool — uses INTERACTIVE priority."""
    try:
        embedding = gateway.embed(note, embed_model=EMBED_MODEL, priority=Priority.INTERACTIVE)
    except Exception as e:
        return f"Failed to embed note: {e}"

    cluster_id, sim = _route_fact(embedding)
    if cluster_id and sim >= CLUSTER_THRESHOLD:
        _merge_into_cluster(cluster_id, note, embedding, source="agent")
    else:
        _create_cluster(note, embedding, source="agent")

    return f"Note saved to memory: {note[:80]}{'...' if len(note) > 80 else ''}"

def should_distil() -> bool:
    return count_sessions_since_last_distil() >= DISTIL_EVERY_N_SESSIONS


def distil() -> None:
    if not should_distil():
        return

    last_distilled_at = _get_meta("last_distilled_at", 0)
    summaries = get_summaries(last_distilled_at)

    if not summaries:
        return

    facts = _extract_facts(summaries)
    print(f"  [DISTIL] {len(facts)} facts extracted")

    if not facts:
        return

    # Compute all embeddings upfront so we can pre-cluster the whole batch
    # before touching existing clusters. This prevents the cold-start cascade
    # where fact N blindly joins a cluster that fact N-1 just created.
    embeddings = [
        gateway.embed(f, embed_model=EMBED_MODEL, priority=Priority.BACKGROUND)
        for f in facts
    ]

    groups = _cluster_batch(embeddings)
    print(f"  [DISTIL] {len(groups)} topic group(s) from {len(facts)} facts")

    for indices in groups:
        group_facts = [facts[i] for i in indices]
        group_embs  = [embeddings[i] for i in indices]

        # Route the group by its centroid so one outlier fact can't
        # drag the whole group into the wrong existing cluster.
        dim = len(group_embs[0])
        group_centroid = [
            sum(e[d] for e in group_embs) / len(group_embs)
            for d in range(dim)
        ]

        cluster_id, sim = _route_fact(group_centroid)
        target = cluster_id if (cluster_id and sim >= CLUSTER_THRESHOLD) else None

        for fact, emb in zip(group_facts, group_embs):
            if target:
                _merge_into_cluster(target, fact, emb)
            else:
                # First fact in the group spawns the new cluster;
                # the rest merge into it.
                target = _create_cluster(fact, emb)

    version = _get_meta("profile_version", 0) + 1
    _set_meta("last_distilled_at", time.time())
    _set_meta("profile_version", version)
    _set_meta("distilled_from_sessions",
              _get_meta("distilled_from_sessions", 0) + len(summaries))
    print(f"  [DISTIL] Done — profile v{version}")





EXTRACT_FACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {"type": "string"}
        }
    },
    "required": ["facts"]
}

EXTRACT_FACTS_SYSTEM_PROMPT = """
You extract durable, atomic facts about a person from session summaries.
Each fact must be a single, self-contained sentence about who the person is, what they do,
what they are building, or what they prefer. Do not include transient or trivial details.
If no facts are found, return an empty array.
Return JSON {"facts": [...]}.
"""

def _extract_facts(summaries: list[dict]) -> list[str]:
    context = "\n\n".join(
        f"[{s.get('active_task','')}] {s.get('summary','')}" for s in summaries
    )

    body = gateway.chat(
        [{"role": "system", "content": EXTRACT_FACTS_SYSTEM_PROMPT},
         {"role": "user", "content": context}],
        MODEL, format=EXTRACT_FACTS_SCHEMA,
        think=False, options={"temperature": 0},
        priority=Priority.BACKGROUND,
    )
    content = body["message"]["content"]
    facts = json.loads(content) if isinstance(content, str) else content
    return facts.get("facts", [])


def _cosine_similarity(a, b):
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

def _cluster_batch(embeddings: list[list]) -> list[list[int]]:
    """
    Group fact indices by pairwise similarity before routing to existing clusters.
    Prevents the cold-start cascade where fact N blindly joins a cluster
    that fact N-1 created 100ms earlier in the same batch.
    Uses single-pass greedy assignment: each unassigned fact starts a new group,
    then pulls in any remaining unassigned facts that are similar enough.
    """
    n = len(embeddings)
    assigned = [-1] * n
    group_id = 0
    for i in range(n):
        if assigned[i] != -1:
            continue
        assigned[i] = group_id
        for j in range(i + 1, n):
            if assigned[j] == -1:
                if _cosine_similarity(embeddings[i], embeddings[j]) >= CLUSTER_THRESHOLD:
                    assigned[j] = group_id
        group_id += 1
    groups: dict[int, list[int]] = {}
    for idx, gid in enumerate(assigned):
        groups.setdefault(gid, []).append(idx)
    return list(groups.values())


def load_centroids() -> list[dict]:
    rows = conn.execute(
        "SELECT cluster_id, label, centroid FROM memory_clusters"
    ).fetchall()
    return [{"cluster_id": r[0], "label": r[1], "centroid": json.loads(r[2])} for r in rows]

def _route_fact(embedding: list) -> tuple[str, Optional[float]]:
    clusters = load_centroids()

    if not clusters:
        return None, 0.0
    
    best, best_sim = None, -1.0
    for c in clusters:
        sim = _cosine_similarity(embedding, c["centroid"])
        if sim > best_sim:
            best, best_sim = c["cluster_id"], sim
    return best, best_sim

_WRITE_SYS = (
    "You maintain a list of durable facts about a user. Given a NEW fact and the most "
    "SIMILAR existing facts (each with its index), choose ONE action:\n"
    "- NOOP: the new fact is already represented (exact duplicate or paraphrase).\n"
    "- UPDATE: the new fact supersedes or refines exactly one existing fact (same topic, "
    "more recent or more specific). Set target_index to that fact's index.\n"
    "- CONFLICT: the new fact directly contradicts an existing fact (same topic, "
    "incompatible values — e.g. different job, different city, different name). "
    "Set target_index to the conflicting fact's index. Do NOT silently overwrite — flag it.\n"
    "- ADD: the new fact is genuinely new information with no overlap.\n"
    "Return JSON {\"action\", \"target_index\", \"text\"}. For ADD use target_index null and "
    "text = the new fact. Never invent facts."
)
_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["ADD", "UPDATE", "NOOP", "CONFLICT"]},
        "target_index": {"type": ["integer", "null"]},
        "text": {"type": "string"},
    },
    "required": ["action"],
}

def _merge_into_cluster(cluster_id: str, fact: str, embedding: list, source: str = "distiller") -> None:
    rows = conn.execute(
        "SELECT fact_id, text, vector_embedding FROM memory_facts WHERE cluster_id = ? AND valid_to IS NULL",
        (cluster_id,)
    ).fetchall()

    # if cluster somehow has no active facts, just add directly
    if not rows:
        _insert_fact(cluster_id, fact, embedding, fact_id=None, source=source)
        _recompute_centroid(cluster_id)
        return

    similar = [{"index": i, "text": r[1]} for i, r in enumerate(rows)]
    body = gateway.chat(
        [{"role": "system", "content": _WRITE_SYS},
         {"role": "user", "content": json.dumps({"new_fact": fact, "similar": similar})}],
        MODEL, format=_WRITE_SCHEMA,
        think=False, options={"temperature": 0},
        priority=Priority.BACKGROUND,
    )

    content = body["message"]["content"]
    result = json.loads(content) if isinstance(content, str) else content

    action = (result.get("action") or "ADD").upper()
    target = result.get("target_index")
    text = result.get("text") or fact

    if action == "NOOP":
        return

    if action == "CONFLICT" and isinstance(target, int) and 0 <= target < len(rows):
        conflicting_fact_id = rows[target][0]
        # Store the incoming fact as a new active fact — do NOT suppress either side
        new_fact_id = str(uuid.uuid4())
        _insert_fact(cluster_id, fact, embedding, fact_id=new_fact_id, source=source)
        # Record the conflict for later user resolution
        conn.execute(
            """INSERT INTO memory_conflicts
               (conflict_id, fact_id_a, fact_id_b, cluster_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), conflicting_fact_id, new_fact_id, cluster_id, time.time())
        )
        conn.commit()
        _recompute_centroid(cluster_id)
        print(f"  [DISTIL] CONFLICT flagged — '{rows[target][1]}' ↔ '{fact}'")
        return
    
    if action == "UPDATE" and isinstance(target, int) and 0 <= target < len(rows):
        old_fact_id = rows[target][0]
        new_fact_id = str(uuid.uuid4())
        new_emb     = gateway.embed(text, embed_model=EMBED_MODEL, priority=Priority.BACKGROUND)
        # supersede the old fact
        conn.execute(
            "UPDATE memory_facts SET valid_to = ?, superseded_by = ? WHERE fact_id = ?",
            (time.time(), new_fact_id, old_fact_id)
        )

        # insert the replacement
        _insert_fact(cluster_id, text, new_emb, fact_id=new_fact_id, source=source)
        _recompute_centroid(cluster_id)
        return

    # ADD
    _insert_fact(cluster_id, fact, embedding, fact_id=None, source=source)
    _recompute_centroid(cluster_id)
    return


def _insert_fact(cluster_id: str, fact: str, embedding: list, fact_id: Optional[str] = None, source: str = "distiller") -> None:
    now = time.time()
    conn.execute(
        """INSERT INTO memory_facts
           (fact_id, cluster_id, text, vector_embedding, valid_from, created_at, source)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (fact_id or str(uuid.uuid4()), cluster_id, fact,
         json.dumps(embedding), now, now, source)
    )
    conn.commit()

def _recompute_centroid(cluster_id: str) -> None:
    rows = conn.execute(
        "SELECT vector_embedding FROM memory_facts WHERE cluster_id = ? AND valid_to IS NULL",
        (cluster_id,)
    ).fetchall()
    if not rows:
        return
    vecs = [json.loads(r[0]) for r in rows]
    dim  = len(vecs[0])
    centroid = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
    conn.execute(
        "UPDATE memory_clusters SET centroid = ?, updated_at = ?, fact_count = ? WHERE cluster_id = ?",
        (json.dumps(centroid), time.time(), len(vecs), cluster_id)
    )
    conn.commit()



# ─────────────────────────────────────────────
# Agent conversation ingestion
# ─────────────────────────────────────────────

_GATE_SYSTEM = (
    "Decide whether this user message contains at least one durable, personal fact "
    "about the writer — something about who they are, what they are building, their goals, "
    "skills, history, preferences, or beliefs. "
    "Ignore questions, small-talk, and requests with no personal content. "
    'Return JSON {"contains_facts": true} or {"contains_facts": false}.'
)
_GATE_SCHEMA = {
    "type": "object",
    "properties": {"contains_facts": {"type": "boolean"}},
    "required": ["contains_facts"],
}

_CONVO_EXTRACT_SYSTEM = """
You extract durable, atomic facts about a person from what they wrote.

Rules:
- Each fact is a single self-contained sentence about who the person is, what they build,
  what they have done, or what they know/prefer. No transient details.
- Only extract facts the user explicitly stated — do not infer or embellish.
- Do not include facts about third parties unless they define the user's context.
- If no durable personal facts exist, return an empty array.
Return JSON {"facts": [...]}.
"""
_CONVO_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {"type": "string"}}},
    "required": ["facts"],
}

_BIO_UPDATE_SYSTEM = """
You maintain a long-term biographical profile of a person based on what they tell you.

You will receive:
1. CURRENT PROFILE — what we already know (field: value pairs, may be empty)
2. NEW MESSAGE — something the person just wrote

Decide what to ADD or UPDATE in the profile using these operations:
- op "set"          : for scalar facts with one answer (name, current_location, current_university, current_job, goal).
                      Only use this for fields that have a single value at a time.
- op "add_items"    : for additive list facts (hobbies, skills, languages, tools, preferences, previous_jobs, previous_universities, previous_locations).
                      Use this when the message adds to an existing list — do NOT replace the whole list.
- op "remove_items" : when the user explicitly says they no longer have/like/do something in a list.
- op "override"     : ONLY when the user explicitly corrects a fact (uses words like "actually",
                      "I meant", "correction", "I was wrong", "not X, it's Y").

Rules:
- Durable facts only: who they are, where they live/study/work, what they are good at,
  what they like or dislike, their goals, background, relationships, major life events.
- NOT situational: skip "currently debugging X", "asked about Y today", one-off tasks.
- Use concise snake_case field names (e.g. current_role, university, location, skills, hobbies).
- If nothing biographical is present, return an empty array.

turn JSON {"updates": [{"field": "...", "value": "..."}]}.
"""
_BIO_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "updates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "op":    {"type": "string", "enum": ["set", "add_items", "remove_items", "override"]},
                    "value": {"type": ["string", "null"]},
                    "items": {
                        "oneOf": [
                            {"type": "array", "items": {"type": "string"}},
                            {"type": "null"}
                        ]
                    },
                },
                "required": ["field", "op"],
            },
        }
    },
    "required": ["updates"],
}


def _update_profile_from_message(user_message: str) -> None:
    """Dynamically update the long-term user profile from a new message.
    The LLM sees the current profile and decides what to add or update."""
    from core.memory_store import get_identity

    current_profile = get_identity()
    profile_text = (
        "\n".join(f"{k}: {v}" for k, v in current_profile.items())
        if current_profile else "(empty — no profile yet)"
    )

    prompt = f"CURRENT PROFILE:\n{profile_text}\n\nNEW MESSAGE:\n{user_message}"

    body = gateway.chat(
        [{"role": "system", "content": _BIO_UPDATE_SYSTEM},
         {"role": "user",   "content": prompt}],
        MODEL, format=_BIO_UPDATE_SCHEMA,
        think=False, options={"temperature": 0},
        priority=Priority.BACKGROUND,
    )
    content = body["message"]["content"]
    result = json.loads(content) if isinstance(content, str) else content
    updates = result.get("updates", [])

    saved = []
    for item in updates:
        field = (item.get("field") or "").strip().lower().replace(" ", "_")
        op    = (item.get("op")    or "set").strip().lower()
        items = item.get("items") or []
        value = (item.get("value") or "").strip()
        
        if op in ("set", "override") and value:
            save_identity_field(field, value=value, source="distiller", op=op)
            saved.append(field)
        elif op in ("add_items", "remove_items") and items:
            save_identity_field(field, source="distiller", op=op, items=items)
            saved.append(f"{field}[{op}]")

    if saved:
        print(f"\n  [DISTIL/profile] updated: {', '.join(saved)}")


def ingest_conversation(user_message: str, agent_reply: str) -> None:
    """Extract facts from a completed agent turn and route them into memory clusters.
    Also extracts structured biographical fields and saves them to identity memory
    (always-injected) so they are reliably available without semantic retrieval.
    Only the user message is used as the fact source — the agent reply is output,
    not ground truth about the user.
    Designed to run in a background thread — all LLM calls use Priority.BACKGROUND."""

    turn_text = f"USER: {user_message}"

    # Gate: skip turns with no personal content
    gate_body = gateway.chat(
        [{"role": "system", "content": _GATE_SYSTEM},
         {"role": "user",   "content": turn_text}],
        MODEL, format=_GATE_SCHEMA,
        think=False, options={"temperature": 0},
        priority=Priority.BACKGROUND,
    )
    gate_content = gate_body["message"]["content"]
    gate = json.loads(gate_content) if isinstance(gate_content, str) else gate_content
    if not gate.get("contains_facts"):
        return

    # Always try to update the long-term user profile with any biographical info.
    # Runs regardless of whether atomic facts are also found.
    _update_profile_from_message(user_message)

    # Extract atomic facts and route into semantic clusters
    extract_body = gateway.chat(
        [{"role": "system", "content": _CONVO_EXTRACT_SYSTEM},
         {"role": "user",   "content": turn_text}],
        MODEL, format=_CONVO_EXTRACT_SCHEMA,
        think=False, options={"temperature": 0},
        priority=Priority.BACKGROUND,
    )
    extract_content = extract_body["message"]["content"]
    extracted = json.loads(extract_content) if isinstance(extract_content, str) else extract_content
    facts = extracted.get("facts", [])

    if not facts:
        return

    print(f"\n  [DISTIL/agent] {len(facts)} fact(s) from conversation turn")

    embeddings = [
        gateway.embed(f, embed_model=EMBED_MODEL, priority=Priority.BACKGROUND)
        for f in facts
    ]

    groups = _cluster_batch(embeddings)

    for indices in groups:
        group_facts = [facts[i] for i in indices]
        group_embs  = [embeddings[i] for i in indices]

        dim = len(group_embs[0])
        group_centroid = [
            sum(e[d] for e in group_embs) / len(group_embs)
            for d in range(dim)
        ]

        cluster_id, sim = _route_fact(group_centroid)
        target = cluster_id if (cluster_id and sim >= CLUSTER_THRESHOLD) else None

        for fact, emb in zip(group_facts, group_embs):
            if target:
                _merge_into_cluster(target, fact, emb, source="agent")
            else:
                target = _create_cluster(fact, emb, source="agent")


_LABEL_SYS = (
    "Give a short 1-3 word snake_case label and one-sentence description "
    "for the topic of this fact about a user. "
    "Return JSON {\"label\": \"...\", \"description\": \"...\"}."
)
_LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "description": {"type": "string"}
    },
    "required": ["label", "description"]
}

def _create_cluster(fact: str, embedding: list, source: str = "distiller") -> str:
    body = gateway.chat(
        [{"role": "system", "content": _LABEL_SYS},
         {"role": "user", "content": fact}],
        MODEL, format=_LABEL_SCHEMA,
        think=False, options={"temperature": 0},
        priority=Priority.BACKGROUND,
    )
    content = body["message"]["content"]
    meta    = json.loads(content) if isinstance(content, str) else content

    now        = time.time()
    cluster_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO memory_clusters
           (cluster_id, label, description, centroid, created_at, updated_at, fact_count)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (cluster_id, meta.get("label", "misc"), meta.get("description", ""),
         json.dumps(embedding), now, now, 1)
    )
    conn.commit()
    _insert_fact(cluster_id, fact, embedding, source=source)
    return cluster_id