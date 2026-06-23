import uuid
import time
import math
import json
import threading

from core.storage import conn
from core.llm_gateway import gateway, Priority

EMBED_MODEL        = "nomic-embed-text"
SUMMARY_MIN_TURNS  = 5    # build first summary after this many turns
SUMMARY_EVERY_N    = 5    # build a new summary every N turns thereafter
RECENT_TURNS_LIMIT = 8    # raw turns always injected (4 full exchanges)
RECENT_SUMMARIES   = 2    # summaries always injected (most recent first)
DEEP_SUMMARIES     = 2    # additional summaries via semantic retrieval
SUMMARY_MIN_SIM    = 0.35 # floor for deep summary retrieval


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _cosine_sim(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _embed_and_update(chat_id: str, text: str) -> None:
    """Background thread: embed text and write vector_embedding for a chat row."""
    try:
        vec = gateway.embed(text, embed_model=EMBED_MODEL, priority=Priority.BACKGROUND)
        conn.execute(
            "UPDATE conversations SET vector_embedding = ? WHERE chat_id = ?",
            (json.dumps(vec), chat_id)
        )
        conn.commit()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────
# Read — Tier 1 (always injected, fixed cost)
# ─────────────────────────────────────────────────────────────

def get_recent_chats(conversation_id: str, limit: int = RECENT_TURNS_LIMIT) -> list[dict]:
    """Last N raw turns, oldest-first, excluding the most recent user message
    (which is already passed explicitly as the final user turn in the messages list)."""
    rows = conn.execute(
        """SELECT role, content FROM conversations
           WHERE conversation_id = ? AND is_summary_chat = 0
           ORDER BY timestamp DESC LIMIT ?""",
        (conversation_id, limit + 1)  # fetch one extra to drop the current user message
    ).fetchall()
    # rows[0] is the most recent — if it's the user message just saved, drop it
    if rows and rows[0][0] == "user":
        rows = rows[1:]
    else:
        rows = rows[:limit]
    rows.reverse()  # oldest-first for natural reading order
    return [{"role": r, "content": c} for r, c in rows]


def get_recent_summaries(conversation_id: str, limit: int = RECENT_SUMMARIES) -> list[str]:
    """Last N rolling summaries (most recent first) — covers the window just before raw turns."""
    rows = conn.execute(
        """SELECT content FROM conversations
           WHERE conversation_id = ? AND is_summary_chat = 1
           ORDER BY timestamp DESC LIMIT ?""",
        (conversation_id, limit)
    ).fetchall()
    # Reverse so they read chronologically in the prompt
    return [r[0] for r in reversed(rows)]


# ─────────────────────────────────────────────────────────────
# Read — Tier 2 (semantic retrieval, only when history is deep)
# ─────────────────────────────────────────────────────────────

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

    # Not enough summaries to go beyond the recent window — skip
    if len(all_rows) <= exclude_last_n:
        return []

    # Only score summaries outside the recent window
    candidates = all_rows[:-exclude_last_n] if exclude_last_n > 0 else all_rows

    scored = []
    for content, vec_json in candidates:
        sim = _cosine_sim(query_vector, json.loads(vec_json))
        if sim >= SUMMARY_MIN_SIM:
            scored.append((sim, content))

    scored.sort(reverse=True)
    return [c for _, c in scored[:top_k]]


# ─────────────────────────────────────────────────────────────
# Summarization
# ─────────────────────────────────────────────────────────────

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

    # Gate: only fire at exactly 5, 10, 15, ...
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

    # Embed the summary so get_relevant_summaries can retrieve it
    _embed_and_update(chat_id, summary_text)
