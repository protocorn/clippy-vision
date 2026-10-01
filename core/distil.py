import json
import math
import time
import uuid
from typing import Optional

from core.chat_model import get_chat_model
from core.llm_gateway import Priority, gateway
from core.local_embeddings import embed_text, embed_texts
from core.memory_store import save_identity_field
from core.storage import conn, get_summaries

CLUSTER_THRESHOLD = 0.75
DISTIL_EVERY_N_SESSIONS = 5  # change to 5 for production
SESSION_GAP_SECONDS    = 30 * 60  # change to 30 * 60 for production
SESSION_MAX_SUMMARIES  = 20  # change to 20 for production

MODEL                  = get_chat_model()


def _session_marked_wrong(summary_id: str) -> bool:
    """A user correction means that summary should not become a stored fact."""
    try:
        row = conn.execute(
            "SELECT user_correction FROM sessions WHERE summary_id = ?",
            (summary_id,),
        ).fetchone()
    except Exception:
        return False
    return bool(row and str(row[0] or "").strip())


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
        embedding = embed_text(note)
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
    summaries = [
        summary for summary in get_summaries(last_distilled_at)
        if not _session_marked_wrong(summary["summary_id"])
    ]

    if not summaries:
        return

    shareable = [summary for summary in summaries if _summary_is_shareable(summary)]
    if not shareable:
        _set_meta("last_distilled_at", time.time())
        return

    from core.memory_consolidation import maybe_consolidate, record_candidate

    # A sitting is one uninterrupted stretch of activity. A gap over 30 minutes starts the next.
    sittings = _group_sittings(shareable)
    stored: set[str] = set()
    try:
        for sitting in sittings:
            observations = _extract_observations(sitting)
            # On-screen content that is not about the person is dropped.
            about_user = [item for item in observations if item.get("about_user")]
            if not about_user:
                continue
            texts = [item["text"] for item in about_user]
            embeddings = embed_texts(texts)
            seen_at = min(float(item["window_start"]) for item in sitting)
            session_ids = [item["summary_id"] for item in sitting]
            # Near-duplicate sentences in this sitting become one candidate.
            for indices in _cluster_batch(embeddings):
                members = [about_user[index] for index in indices]
                vectors = [embeddings[index] for index in indices]
                observation = max(members, key=lambda item: len(item["text"]))["text"]
                kind = "observed" if any(item["kind"] == "observed" for item in members) else "inferred"
                dim = len(vectors[0])
                centroid = [
                    sum(vector[axis] for vector in vectors) / len(vectors)
                    for axis in range(dim)
                ]
                candidate_id = record_candidate(
                    observation,
                    centroid,
                    kind=kind,
                    about_user=True,
                    session_ids=session_ids,
                    seen_at=seen_at,
                    source="screen",
                )
                if candidate_id:
                    stored.add(candidate_id)
    except Exception as exc:
        # Leave last_distilled_at alone so the next run can retry these sessions.
        print(f"  [DISTIL] observations not stored: {exc}")
        return

    print(f"  [DISTIL] {len(stored)} candidate(s) from {len(sittings)} sitting(s)")
    version = _get_meta("profile_version", 0) + 1
    _set_meta("last_distilled_at", time.time())
    _set_meta("profile_version", version)
    _set_meta("distilled_from_sessions",
              _get_meta("distilled_from_sessions", 0) + len(summaries))
    print(f"  [DISTIL] Done — profile v{version}")
    maybe_consolidate()





def _group_sittings(summaries: list[dict]) -> list[list[dict]]:
    """Split summaries into separate sittings. A gap, or a long run, starts another."""
    ordered = sorted(summaries, key=lambda item: float(item["window_start"]))
    if not ordered:
        return []
    groups = [[ordered[0]]]
    for summary in ordered[1:]:
        gap = float(summary["window_start"]) - float(groups[-1][-1]["window_end"])
        if gap > SESSION_GAP_SECONDS or len(groups[-1]) >= SESSION_MAX_SUMMARIES:
            groups.append([summary])
        else:
            groups[-1].append(summary)
    return groups


_OBSERVATION_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string", "enum": ["observed", "inferred"]},
                    "about_user": {"type": "boolean"},
                },
                "required": ["text", "kind", "about_user"],
            },
        }
    },
    "required": ["observations"],
}

_OBSERVATION_SYSTEM = """
You read summaries of what was on a screen during one sitting.
Return short observations of that sitting. Do not write a biography.

Each observation:
- text: one sentence about what happened
- kind: "observed" when the summary states it, "inferred" when you are guessing
- about_user: true only when it is about the person using this computer
  (a routine, preference, relationship, trip, goal, or situation of theirs).
  false when it is something they looked at: an article, a page about someone else,
  another person's document, or on-screen content that is not about them.

One sitting is not a lasting fact about who they are.
If nothing happened, return an empty array.
Return JSON {"observations": [{"text", "kind", "about_user"}]}.
"""


def _extract_observations(summaries: list[dict]) -> list[dict]:
    context = "\n\n".join(
        f"[{s.get('active_task','')}] {s.get('summary','')}" for s in summaries
    )
    body = gateway.chat(
        [{"role": "system", "content": _OBSERVATION_SYSTEM},
         {"role": "user", "content": context}],
        MODEL, format=_OBSERVATION_SCHEMA,
        think=False, options={"temperature": 0},
        priority=Priority.BACKGROUND,
    )
    content = body["message"]["content"]
    payload = json.loads(content) if isinstance(content, str) else content
    observations = []
    for item in payload.get("observations") or []:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split()).strip()
        if not text:
            continue
        kind = "observed" if str(item.get("kind") or "").strip().lower() == "observed" else "inferred"
        observations.append({
            "text": text,
            "kind": kind,
            "about_user": bool(item.get("about_user")),
        })
    return observations


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

def _route_fact(embedding: list) -> tuple[str, float | None]:
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
        if isinstance(target, int) and 0 <= target < len(rows):
            conn.execute(
                "UPDATE memory_facts SET last_confirmed = ? WHERE fact_id = ?",
                (time.time(), rows[target][0]),
            )
            conn.commit()
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
        new_emb     = embed_text(text)

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


def _summary_is_shareable(summary: dict) -> bool:
    """False when the session touched a private window, or we can no longer tell."""
    from core.cloud_provenance import resolve_session_private

    flag = resolve_session_private(
        summary["summary_id"],
        float(summary["window_start"]),
        float(summary["window_end"]),
        summary.get("private"),
    )
    return flag == 0


def _insert_fact(
    cluster_id: str,
    fact: str,
    embedding: list,
    fact_id: str | None = None,
    source: str = "distiller",
    private: int | None = 0,
    scope: str | None = None,
    kind: str | None = None,
    session_ids: list | None = None,
    last_confirmed: float | None = None,
    valid_from: float | None = None,
) -> str:
    now = time.time()
    fact_id = fact_id or str(uuid.uuid4())
    if scope is None:
        scope = "stated" if source in {"agent", "user"} else "pattern"
    if last_confirmed is None and scope == "stated":
        last_confirmed = now
    conn.execute(
        """INSERT INTO memory_facts
           (fact_id, cluster_id, text, vector_embedding, valid_from, created_at, source, private,
            support_session_ids, last_confirmed, scope, kind)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fact_id, cluster_id, fact, json.dumps(embedding),
            valid_from if valid_from is not None else now, now, source, private,
            json.dumps(session_ids or []), last_confirmed, scope, kind,
        )
    )
    conn.commit()
    return fact_id

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
    return saved


def ingest_conversation(user_message: str, agent_reply: str) -> dict:
    """Store what the user explicitly said.

    Biographical statements go straight into identity and are not copied into
    a claim that ages. A one-sitting situation becomes a candidate, not a claim.
    Only the user message is the source. Designed for a background thread.
    Returns {"facts": [...], "profile": [...]} and may include "error"."""
    try:
        return _ingest_conversation(user_message, agent_reply)
    except OSError as exc:
        # Soft defer (RAM/CPU gates) must never crash the chat turn's background thread.
        print(f"  [DISTIL/agent] deferred: {exc}")
        return {"facts": [], "profile": [], "error": str(exc)}
    except Exception as exc:
        print(f"  [DISTIL/agent] ingest failed: {exc}")
        return {"facts": [], "profile": [], "error": str(exc)}


def _ingest_conversation(user_message: str, agent_reply: str) -> dict:
    turn_text = f"USER: {user_message}"
    # agent_reply is accepted so older callers can pass the turn, and is not stored.


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
        return {"facts": [], "profile": []}

    # Always try to update the long-term user profile with any biographical info.
    # Runs regardless of whether atomic facts are also found.
    profile = _update_profile_from_message(user_message) or []


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
        return {"facts": [], "profile": profile}

    # Identity already holds the biography. Do not also file a decaying claim.
    if profile:
        print(f"\n  [DISTIL/agent] identity updated ({', '.join(profile)}); no cluster copy")
        return {"facts": [], "profile": profile}

    from core.memory_consolidation import record_candidate

    print(f"\n  [DISTIL/agent] {len(facts)} situation(s) kept as candidates")
    seen_at = time.time()
    embeddings = embed_texts(facts)
    for fact, embedding in zip(facts, embeddings):
        record_candidate(
            fact,
            embedding,
            kind="observed",
            about_user=True,
            session_ids=[],
            seen_at=seen_at,
            source="user",
        )
    return {"facts": list(facts), "profile": profile}


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

def _label_for_fact(fact: str) -> tuple[str, str]:
    body = gateway.chat(
        [{"role": "system", "content": _LABEL_SYS},
         {"role": "user", "content": fact}],
        MODEL, format=_LABEL_SCHEMA,
        think=False, options={"temperature": 0},
        priority=Priority.BACKGROUND,
    )
    content = body["message"]["content"]
    meta = json.loads(content) if isinstance(content, str) else content
    return meta.get("label", "misc"), meta.get("description", "")


def _create_cluster(fact: str, embedding: list, source: str = "distiller", **fact_kwargs) -> str:
    label = fact_kwargs.pop("label", None)
    description = fact_kwargs.pop("description", None)
    if not label:
        label, description = _label_for_fact(fact)

    now        = time.time()
    cluster_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO memory_clusters
           (cluster_id, label, description, centroid, created_at, updated_at, fact_count)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (cluster_id, label or "misc", description or "",
         json.dumps(embedding), now, now, 1)
    )
    conn.commit()
    _insert_fact(cluster_id, fact, embedding, source=source, **fact_kwargs)
    return cluster_id
