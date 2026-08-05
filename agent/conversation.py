import uuid
import time
import math
import json
import threading

from core.storage import conn
from core.llm_gateway import gateway, Priority
from core.local_embeddings import embed_text

SUMMARY_MIN_TURNS  = 5
SUMMARY_EVERY_N    = 5
RECENT_TURNS_LIMIT = 8
RECENT_SUMMARIES   = 2
DEEP_SUMMARIES     = 2
SUMMARY_MIN_SIM    = 0.35


SEARCH_HALF_LIFE_DAYS = 14.0
SEARCH_SIM_WEIGHT     = 0.72
SEARCH_RECENCY_WEIGHT = 0.28
SEARCH_MIN_SIM        = 0.22
SEARCH_DEFAULT_LIMIT  = 20






def _cosine_sim(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _embed_and_update(chat_id: str, text: str) -> None:
    """Background thread: embed text and write vector_embedding for a chat row."""
    try:
        vec = embed_text(text)
        conn.execute(
            "UPDATE conversations SET vector_embedding = ? WHERE chat_id = ?",
            (json.dumps(vec), chat_id)
        )
        conn.commit()
    except Exception:
        pass






def save_chat(conversation_id: str, role: str, content: str) -> str:
    """Persist a turn and fire a background embed."""
    chat_id = str(uuid.uuid4())
    try:
        conn.execute(
            """INSERT INTO conversations
               (chat_id, conversation_id, timestamp, role, content)
               VALUES (?, ?, ?, ?, ?)""",
            (chat_id, conversation_id, time.time(), role, content)
        )
        conn.commit()
    except Exception as e:
        return f"Failed to save chat: {e}"

    threading.Thread(
        target=_embed_and_update,
        args=(chat_id, content),
        daemon=True,
    ).start()
    return "Success"






def get_recent_chats(conversation_id: str, limit: int = RECENT_TURNS_LIMIT) -> list[dict]:
    """Last N raw turns, oldest-first, excluding the most recent user message
    (which is already passed explicitly as the final user turn in the messages list)."""
    rows = conn.execute(
        """SELECT role, content FROM conversations
           WHERE conversation_id = ? AND is_summary_chat = 0
           ORDER BY timestamp DESC LIMIT ?""",
        (conversation_id, limit + 1)
    ).fetchall()

    if rows and rows[0][0] == "user":
        rows = rows[1:]
    else:
        rows = rows[:limit]
    rows.reverse()
    return [{"role": r, "content": c} for r, c in rows]


def _truncate_title(raw: str) -> str:
    title = " ".join((raw or "").split())
    if len(title) > 72:
        title = title[:69].rstrip() + "…"
    return title or "New conversation"


def list_conversations() -> list[dict]:
    """All conversations with a display title, most recently active first.

    Title is the first user message (truncated). Falls back to the first
    non-empty turn, then a generic label.
    """
    rows = conn.execute(
        """SELECT
             c.conversation_id,
             MAX(c.timestamp) AS last_ts,
             (
               SELECT content FROM conversations c2
               WHERE c2.conversation_id = c.conversation_id
                 AND c2.is_summary_chat = 0
                 AND c2.role = 'user'
               ORDER BY c2.timestamp ASC LIMIT 1
             ) AS first_user,
             (
               SELECT content FROM conversations c3
               WHERE c3.conversation_id = c.conversation_id
                 AND c3.is_summary_chat = 0
               ORDER BY c3.timestamp ASC LIMIT 1
             ) AS first_any
           FROM conversations c
           WHERE c.is_summary_chat = 0
           GROUP BY c.conversation_id
           ORDER BY last_ts DESC"""
    ).fetchall()

    return [
        {
            "conversation_id": cid,
            "last_timestamp": last_ts,
            "title": _truncate_title(first_user or first_any or ""),
        }
        for cid, last_ts, first_user, first_any in rows
    ]


def delete_conversation(conversation_id: str) -> int:
    """Delete every turn and rolling summary belonging to one conversation."""
    cid = (conversation_id or "").strip()
    if not cid:
        return 0
    cursor = conn.execute(
        "DELETE FROM conversations WHERE conversation_id = ?",
        (cid,),
    )
    conn.commit()
    return max(0, int(cursor.rowcount or 0))


def search_conversations(query: str, limit: int = 20) -> list[dict]:
    """Semantic conversation search with a recency boost.

    Uses existing per-turn / summary vector embeddings (built on save).
    Score = SIM_WEIGHT * max_cosine_sim + RECENCY_WEIGHT * half_life_decay.
    Keyword hits on content get a small similarity bump so exact phrases still surface.
    """
    q = (query or "").strip()
    if not q:
        return list_conversations()[:limit]

    meta = {c["conversation_id"]: c for c in list_conversations()}
    if not meta:
        return []

    q_lower = q.lower()
    now = time.time()

    try:
        q_vec = embed_text(q)
    except Exception:
        q_vec = None

    best_sim: dict[str, float] = {cid: 0.0 for cid in meta}

    if q_vec:
        rows = conn.execute(
            """SELECT conversation_id, content, vector_embedding
               FROM conversations
               WHERE vector_embedding IS NOT NULL"""
        ).fetchall()
        for cid, content, vec_json in rows:
            if cid not in best_sim:
                continue
            try:
                sim = _cosine_sim(q_vec, json.loads(vec_json))
            except Exception:
                continue
            if content and q_lower in content.lower():
                sim = min(1.0, sim + 0.08)
            if sim > best_sim[cid]:
                best_sim[cid] = sim
    else:

        rows = conn.execute(
            """SELECT conversation_id, content FROM conversations
               WHERE is_summary_chat = 0"""
        ).fetchall()
        for cid, content in rows:
            if cid not in best_sim or not content:
                continue
            if q_lower in content.lower():
                best_sim[cid] = max(best_sim[cid], 0.55)


    for cid, info in meta.items():
        if q_lower in (info.get("title") or "").lower():
            best_sim[cid] = max(best_sim[cid], 0.6)

    scored: list[tuple[float, dict]] = []
    for cid, info in meta.items():
        sim = best_sim.get(cid, 0.0)
        if sim < SEARCH_MIN_SIM:
            continue
        age_days = max(0.0, (now - (info.get("last_timestamp") or now)) / 86400.0)
        recency = math.exp(-math.log(2) * age_days / SEARCH_HALF_LIFE_DAYS)
        score = SEARCH_SIM_WEIGHT * sim + SEARCH_RECENCY_WEIGHT * recency
        scored.append((score, {
            "conversation_id": cid,
            "last_timestamp": info["last_timestamp"],
            "title": info["title"],
            "score": round(score, 4),
            "similarity": round(sim, 4),
            "recency": round(recency, 4),
        }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]


def get_conversation_messages(conversation_id: str) -> list[dict]:
    """All raw turns for a conversation, oldest-first (for UI reload)."""
    rows = conn.execute(
        """SELECT role, content, timestamp FROM conversations
           WHERE conversation_id = ? AND is_summary_chat = 0
           ORDER BY timestamp ASC""",
        (conversation_id,),
    ).fetchall()
    return [
        {"role": role, "content": content, "timestamp": ts}
        for role, content, ts in rows
    ]


def get_recent_summaries(conversation_id: str, limit: int = RECENT_SUMMARIES) -> list[str]:
    """Last N rolling summaries (most recent first) — covers the window just before raw turns."""
    rows = conn.execute(
        """SELECT content FROM conversations
           WHERE conversation_id = ? AND is_summary_chat = 1
           ORDER BY timestamp DESC LIMIT ?""",
        (conversation_id, limit)
    ).fetchall()

    return [r[0] for r in reversed(rows)]






def get_relevant_summaries(
    conversation_id: str,
    query_vector: list[float],
    exclude_last_n: int = RECENT_SUMMARIES,
    top_k: int = DEEP_SUMMARIES,
) -> list[str]:
    """Cosine-search older summaries that aren't already injected as recent.
    Returns empty list when history is shallow (≤ exclude_last_n summaries)."""
    all_rows = conn.execute(
        """SELECT content, vector_embedding FROM conversations
           WHERE conversation_id = ? AND is_summary_chat = 1
             AND vector_embedding IS NOT NULL
           ORDER BY timestamp ASC""",
        (conversation_id,)
    ).fetchall()


    if len(all_rows) <= exclude_last_n:
        return []


    candidates = all_rows[:-exclude_last_n] if exclude_last_n > 0 else all_rows

    scored = []
    for content, vec_json in candidates:
        sim = _cosine_sim(query_vector, json.loads(vec_json))
        if sim >= SUMMARY_MIN_SIM:
            scored.append((sim, content))

    scored.sort(reverse=True)
    return [c for _, c in scored[:top_k]]






SUMMARY_SYSTEM_PROMPT = (
    "You are summarizing a conversation between a user and their personal AI assistant.\n"
    "Write a concise 2-4 sentence summary that preserves: the main topics discussed, "
    "any decisions or conclusions reached, open questions, and personal details the user shared.\n"
    "Do not editorialize. Write in third person (e.g. 'The user asked about...')."
)


def _build_summary_text(chats: list[tuple]) -> str:
    """Call LLM to summarize a list of (role, content) tuples."""
    transcript = "\n".join(
        f"{'User' if role == 'user' else 'Clippy'}: {content}"
        for role, content in chats
    )
    body = gateway.chat(
        messages=[
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user",   "content": transcript},
        ],
        model="qwen3:8b",
        think=False,
        options={"temperature": 0},
        priority=Priority.BACKGROUND,
    )
    return body["message"]["content"]


def maybe_summarize(conversation_id: str) -> None:
    """Build and persist a rolling summary after every SUMMARY_EVERY_N turns.
    Also embeds the summary in background so it becomes retrievable.
    Designed to be called in a background thread — all LLM calls use BACKGROUND priority."""
    count = conn.execute(
        """SELECT COUNT(*) FROM conversations
           WHERE conversation_id = ? AND is_summary_chat = 0""",
        (conversation_id,)
    ).fetchone()[0]


    if count < SUMMARY_MIN_TURNS or count % SUMMARY_EVERY_N != 0:
        return

    chats = conn.execute(
        """SELECT role, content FROM conversations
           WHERE conversation_id = ? AND is_summary_chat = 0
           ORDER BY timestamp ASC""",
        (conversation_id,)
    ).fetchall()

    summary_text = _build_summary_text(chats)

    chat_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO conversations
           (chat_id, conversation_id, timestamp, role, content, is_summary_chat)
           VALUES (?, ?, ?, ?, ?, 1)""",
        (chat_id, conversation_id, time.time(), "system", summary_text)
    )
    conn.commit()


    _embed_and_update(chat_id, summary_text)
