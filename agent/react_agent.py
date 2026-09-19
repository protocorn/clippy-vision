import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from agent.conversation import (
    get_recent_chats,
    get_recent_summaries,
    get_relevant_summaries,
    maybe_summarize,
    save_chat,
)
from agent.helpers.time_resolver import resolve_temporal_range
from agent.memory import get_autobiographical_context
from agent.prefetch.memory_query import memory_query
from agent.prefetch.specific_recall import specific_recall
from agent.prefetch.time_anchor import time_anchor_fetch
from agent.prefetch.topic_search import topic_search
from agent.router import classify_query, should_prefetch
from agent.tools import TOOL_SCHEMAS, TOOLS, build_prefetch_tool_schema
from core.distil import ingest_conversation
from core.llm_gateway import Priority, gateway
from core.local_embeddings import embed_text
from core.memory_store import get_unresolved_conflicts
from core.storage import get_user_name
from core.workspace_roots import format_roots_for_prompt

MODEL     = "qwen3:8b"
MAX_STEPS = 10
# Cap what enters the model after a tool call (Qwen renders tool→user; huge dumps steal the question).
MAX_TOOL_RESULT_CHARS = 1500
MAX_EVIDENCE_CARD_CHARS = 1500
MAX_RETRIEVE_STEPS = 4



# Soft cap on user turns. Prefetch/history/profile already consume most of the
# context window; ~4k chars (~1k tokens) leaves room for unknown retrieval size.
USER_MESSAGE_MAX_CHARS = 4000


# Routes that have real prefetch implementations
_PREFETCHABLE = {"time_anchored", "topic_search", "specific_recall", "memory_query"}
MAX_PREFETCH_CONTEXT_CHARS = 8000
_SHORT_FOLLOW_UP_RE = re.compile(
    r"^\s*(?:nothing else|anything else|what else|and|more|go on|continue|that one|"
    r"what about that|what about it)\s*[?.!]*\s*$",
    re.IGNORECASE,
)
_EMPTY_PREFETCH_RE = re.compile(
    r"\b(?:no activity|no matching|no data|nothing matched|could not resolve)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_'/-]{2,}", re.IGNORECASE)

_TOOL_POLICY_WITH_PREFETCH = """Tool Policy (follow in order — small model: do not skip steps):
1. If get_prefetched_context is in your tool list AND the user asks about their activity,
   memory, yesterday/week, a topic they worked on, or a screen artifact: call
   get_prefetched_context FIRST (no arguments). Read its result before any search_*.
2. After that (or if the bundle is empty/off-topic):
   - Day/week/"what did I work on" → search_sessions
   - URL / clipboard / OCR / exact text → search_events
3. Open/find a local file by name:
   - If the user gave a folder/root path → remember_workspace_root first
   - find_files(name=...) under trusted folders (NOT search_events)
   - Then open_path with one absolute path from matches
   - If multiple matches, ask which one — never invent paths
4. Open a URL: resolve via get_prefetched_context or search_events, then open_url.
5. save_identity / save_note / delete_note: only when the user asks to remember or forget.
6. Casual chat / general knowledge: no activity tools.
7. Never claim you already "have" activity data unless a tool just returned it.
8. Do NOT write the final user-facing answer while still gathering evidence — call tools, then stop."""

_TOOL_POLICY_NO_PREFETCH = """Tool Policy (follow in order):
1. Activity overviews (yesterday, this week, what did I work on) → search_sessions first.
2. Artifacts (URL, clipboard, OCR, exact text) → search_events first.
3. Open/find a local file by name:
   - If the user gave a folder/root path → remember_workspace_root first
   - find_files(name=...) (NOT search_events for filesystem location)
   - Then open_path with one absolute path from matches
4. Open a URL: search_events for the URL, then open_url. Never invent URLs.
5. If one activity search is empty, try the other once before giving up.
6. save_identity / save_note / delete_note: only when asked to remember or forget.
7. Casual chat / general knowledge: no activity tools.
8. Do NOT write the final user-facing answer while still gathering evidence — call tools, then stop."""


_RETRIEVE_SYSTEM_TEMPLATE = """You are Clippy's RETRIEVAL step for {USER_NAME}.
Current date/time: {datetime}

Your only job: call tools to gather evidence for the question below.
Do NOT write the final answer to the user in this step. When you have enough evidence (or tools are empty), stop calling tools and reply with a single short line like "Evidence ready."

Question:
{question}

<prefetched_bundle>
{prefetch_status}
</prefetched_bundle>

<workspace_roots>
{workspace_roots}
</workspace_roots>

{tool_policy}
"""

_ANSWER_SYSTEM_TEMPLATE = """You are Clippy, {USER_NAME}'s local personal AI assistant.
Current date/time: {datetime}

Answer the user's question using ONLY the EVIDENCE below.
If evidence is thin, off-topic, or missing, say so plainly — do not invent activity, files, or motives.
If the user proposes a link between topics (e.g. research X for project Y), say whether EVIDENCE supports it.
Be concise. Do not mention tools, prefetch, or "evidence blocks".
Address the user naturally by name when appropriate.
"""

_SYSTEM_PROMPT_TEMPLATE = """You are Clippy, {USER_NAME}'s local personal AI assistant.

Your job is to answer from evidence using local memory and activity data. Be accurate before being confident.

Current date and time: {datetime}

<conversation_history>
{conversation_history}
</conversation_history>

<user_profile>
[Autobiographical context: who the user is, their identity, skills, projects, and preferences. Use it to personalise your answers.]
{user_profile}
</user_profile>

<prefetched_bundle>
{prefetch_status}
</prefetched_bundle>

<workspace_roots>
{workspace_roots}
</workspace_roots>

Core Rules:
- <user_profile> is always present. Use it for identity and preference questions without calling any tools.
- Keep explicit personal facts separate from observed activity. App usage can support "what was I doing" but must not be presented as a fact about the user's identity.
- A zero explicit-fact count does not mean there is no memory. If activity, screenshots, conversations, or session counts are present, describe those separately and accurately.
- For an exact screenshot question, describe only the exact screenshot evidence. Never blend nearby before/after activity into what was visible in that frame.
- If exact screenshot evidence includes screenshot_source, the image is locally available. Do not claim the image content or file is unavailable.
- Do not invent activity history, timestamps, files, websites, apps, or user intentions.
- If evidence is weak, partial, or missing, say so plainly — but call tools first when the user asks about activity or asks to open something.
- Address the user naturally. Use their name occasionally, not repeatedly.
- When a follow-up references something already in <conversation_history>, use that as background only — still answer <current_user_question>, not an earlier turn.
- Never mention internal system terms in your response: do not say "prefetch", "prefetched bundle",
  "tool result", "activity summaries", or any other implementation detail.
  Present all information naturally as if you simply know it.

{tool_policy}

Response Style:
- Be concise by default. Use 1-3 sentences for simple answers.
- Avoid repetitive greetings, generic follow-up offers, and decorative emoji.
- Give detailed answers when the user asks for analysis, planning, comparison, or debugging.
- Do not expose raw SQL, tiers, internal tool names, or implementation details unless the user asks.

<current_user_question>
{current_user_question}
</current_user_question>
Your final answer must address <current_user_question> above. Prior turns are context, not the question."""




def _build_combined_query_context(conversation_id: str, user_message: str) -> str:

    recent_turns = get_recent_chats(conversation_id, limit=3)
    if not recent_turns:
        return user_message

    prior = " | ".join(
        f"{'User' if t['role'] == 'user' else 'Clippy'}: {t['content']}"
        for t in recent_turns
    )
    return f"User: {user_message} | Prior turns: {prior}"

_THINKING_BLOCK_RE = re.compile(
    r"<thinking>\s*[\s\S]*?\s*</thinking>\s*",
    re.IGNORECASE,
)
_QUESTION_ANCHOR_PREFIX = "(Answer this question using any tool results above.)\n"


def _visible_chat_content(role: str, content: str) -> str:
    """History for the model must not re-inject prior chain-of-thought."""
    text = (content or "").strip()
    if role == "assistant":
        text = _THINKING_BLOCK_RE.sub("", text).strip()
    return text


def _build_conversation_history(conversation_id: str, q_vec: list|None=None) -> str:
    """Assemble the conversation history block for the system prompt.

    Tier 1 (always): last 2 rolling summaries + last 4 raw turns.
    Tier 2 (when deep): up to 2 older summaries retrieved by cosine similarity.
    """
    parts = []



    # Tier 2 — semantically relevant older summaries (only when history is deep)
    if q_vec:
        try:
            deep = get_relevant_summaries(conversation_id, q_vec)
            if deep:
                parts.append("[Earlier relevant context]\n" + "\n\n".join(deep))
        except Exception:
            pass


    # Tier 1a — recent rolling summaries
    recent_summaries = get_recent_summaries(conversation_id)
    if recent_summaries:
        parts.append("[Recent summary]\n" + "\n\n".join(recent_summaries))


    # Tier 1b — last N raw turns (answers only — never prior <thinking> blocks)
    recent_turns = get_recent_chats(conversation_id)
    if recent_turns:
        lines = []
        for t in recent_turns:
            label = "User" if t["role"] == "user" else "Clippy"
            visible = _visible_chat_content(t["role"], t["content"])
            if visible:
                lines.append(f"{label}: {visible}")
        if lines:
            parts.append("[Recent turns]\n" + "\n".join(lines))

    return "\n\n".join(parts) if parts else "No conversation history yet."


def _build_conflict_notice() -> str:
    """Return a formatted notice of unresolved memory conflicts, or empty string if none."""
    conflicts = get_unresolved_conflicts(limit=3)
    if not conflicts:
        return ""
    lines = ["[Unresolved memory conflicts — ask the user to clarify if relevant:]"]
    for c in conflicts:
        lines.append(f'  • "{c["fact_a"]}"  ↔  "{c["fact_b"]}"')
    return "\n".join(lines)


def _build_system_prompt(
    conversation_id: str,
    user_message: str = "",
    q_vec: list | None = None,
    *,
    prefetch_available: bool = False,
    prefetch_status: str = "",
) -> str:
    now    = time.localtime()
    dt_str = time.strftime("%A %B %d, %Y at %H:%M", now)

    user_profile = get_autobiographical_context(q_vec=q_vec)
    if not user_profile:
        user_profile = "No profile data yet."

    conflict_notice = _build_conflict_notice()
    status = (prefetch_status or "").strip()
    if not status:
        status = (
            "A get_prefetched_context tool is available this turn — call it before answering "
            "activity/memory questions."
            if prefetch_available
            else "No prefetched bundle this turn. Use search_sessions / search_events for activity questions."
        )
    if conflict_notice:
        status = status + "\n\n" + conflict_notice

    tool_policy = (
        _TOOL_POLICY_WITH_PREFETCH if prefetch_available else _TOOL_POLICY_NO_PREFETCH
    )

    history = _build_conversation_history(conversation_id, q_vec) if user_message else "No conversation history yet."

    return _SYSTEM_PROMPT_TEMPLATE.format(
        USER_NAME=get_user_name() or "the user",
        datetime=dt_str,
        user_profile=user_profile,
        prefetch_status=status,
        workspace_roots=format_roots_for_prompt(),
        tool_policy=tool_policy,
        conversation_history=history,
        current_user_question=(user_message or "").strip() or "(empty)",
    )


def _fetch_single_route(
    route: str,
    temporal_range,  # pre-resolved, may be None
    query: str,
    combined: str,
    q_vec: list,
) -> str:
    """Execute one prefetch route and return its string result."""
    if route == "memory_query":
        return memory_query(query=query, q_vec=q_vec)

    if route == "topic_search":
        return topic_search(combined, q_vec=q_vec, temporal_range=temporal_range)

    if route == "time_anchored":
        if temporal_range:
            return time_anchor_fetch(temporal_range, q_vec=q_vec)
        return ""

    if route == "specific_recall":
        return specific_recall(combined, temporal_range=temporal_range, q_vec=q_vec)

    return ""


def _run_prefetch(decision, query: str, combined: str, q_vec: list) -> tuple[str, list[str]]:
    """Fire prefetch for primary + all secondary routes in parallel.

    Returns (context_string, routes_actually_run).

    Time handling:
    - Resolve temporal range once from `combined` (enriched with recent turns).
    - If time_anchored is PRIMARY  → run time_anchor_fetch to get the session view.
    - If time_anchored is SECONDARY → pass temporal_range as a filter to primary;
      do NOT run a separate time_anchor_fetch (the range narrows, not supplements).
    """

    # ── Resolve temporal range once ───────────────────────────────────────────
    all_routes = {decision.primary} | set(decision.secondary)
    needs_time = "time_anchored" in all_routes
    temporal_range = resolve_temporal_range(combined) if needs_time else None




    # ── Build the list of routes to run ───────────────────────────────────────
    # time_anchored as secondary = filter only; don't run it as a standalone fetch
    # Non-prefetchable primaries (e.g. casual) are skipped; their secondaries still run.
    primary_routes = [decision.primary] if decision.primary in _PREFETCHABLE else []
    secondary_routes = [
        s for s in decision.secondary
        if s in _PREFETCHABLE and s != decision.primary and s != "time_anchored"
    ]
    routes_to_run = primary_routes + secondary_routes
    print(f"[prefetch] routes: {routes_to_run}")

    if not routes_to_run:
        return "", []


    # ── Single route — no thread overhead ────────────────────────────────────
    if len(routes_to_run) == 1:
        return (
            _limit_prefetch_context(
                _fetch_single_route(routes_to_run[0], temporal_range, query, combined, q_vec)
            ),
            list(routes_to_run),
        )


    results: dict[str, str] = {}
    # ── Multiple routes — run in parallel ────────────────────────────────────
    with ThreadPoolExecutor(max_workers=len(routes_to_run)) as ex:
        future_to_route = {
            ex.submit(_fetch_single_route, r, temporal_range, query, combined, q_vec): r
            for r in routes_to_run
        }
        for future in as_completed(future_to_route):
            route  = future_to_route[future]
            try:
                result = future.result() or ""
            except Exception as e:
                print(f"[prefetch]   {route} ERROR — {e}")
                result = ""
            print(f"[prefetch]   {route} → {len(result)} chars")
            results[route] = result



    combined_context = "\n\n---\n\n".join(
        results[route] for route in routes_to_run if results.get(route)
    )
    return _limit_prefetch_context(combined_context), list(routes_to_run)


def _limit_prefetch_context(context: str) -> str:
    """Bound retrieval context so one broad query cannot crowd out reasoning."""
    context = (context or "").strip()
    if len(context) <= MAX_PREFETCH_CONTEXT_CHARS:
        return context
    return (
        context[:MAX_PREFETCH_CONTEXT_CHARS].rstrip()
        + "\n\n[Retrieved context truncated for space. Use the search tools for missing detail.]"
    )


def _prefetch_looks_empty(context: str) -> bool:
    text = (context or "").strip()
    if not text:
        return True
    # Real content headers from formatters — treat as non-empty even if short.
    if re.search(
        r"\[(?:raw events|activity summaries|activity memory|memory)",
        text,
        re.IGNORECASE,
    ):
        # Still empty if the body is only a no-activity line under those tags
        if _EMPTY_PREFETCH_RE.search(text) and "summary:" not in text.casefold():
            return True
        return False
    return bool(_EMPTY_PREFETCH_RE.search(text))


def _prefetch_synopsis(context: str, routes: list[str], max_chars: int = 360) -> str:
    """Short plain-language hint for the dynamic tool description (not the full dump)."""
    text = (context or "").strip()
    if not text:
        return "empty bundle"
    if _prefetch_looks_empty(text):
        return (text[:max_chars] + "…").replace("\n", " ")

    bits: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.casefold()
        if line.startswith("[") and line.endswith("]"):
            bits.append(line.strip("[]"))
        elif low.startswith("summary:"):
            bits.append(line[8:].strip()[:120])
        elif low.startswith("task:"):
            bits.append(line)
        elif low.startswith("url:") or low.startswith("active_url:"):
            bits.append(line)
        elif low.startswith("- ") and len(line) > 20:
            bits.append(line[2:100])
        if sum(len(b) for b in bits) > max_chars:
            break
    synopsis = " | ".join(bits) if bits else text[:max_chars].replace("\n", " ")
    if len(synopsis) > max_chars:
        synopsis = synopsis[: max_chars - 1].rstrip() + "…"
    route_bit = ",".join(routes) if routes else "?"
    return f"[{route_bit}] {synopsis}"


def _cap_tool_result(result: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Hard-truncate tool payloads before they enter any model context."""
    text = str(result or "")
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return (
        text[:limit].rstrip()
        + f"\n\n[truncated — {omitted} chars omitted; narrow the query or search again for detail]"
    )


_EVIDENCE_STOPWORDS = frozenset({
    "the", "and", "for", "what", "did", "this", "week", "with", "about", "that",
    "have", "was", "were", "from", "into", "your", "you", "how", "why", "when",
    "who", "which", "are", "is", "my", "me", "a", "an", "of", "to", "in", "on",
    "it", "or", "do", "does", "can", "could", "would", "should", "please", "tell",
    "show", "list", "summarize", "summary", "any", "all", "just", "like", "some",
    "today", "yesterday", "morning", "afternoon", "evening", "night", "days",
    "hours", "minutes", "there", "here", "been", "being", "will", "than", "then",
})


def _question_terms(question: str) -> set[str]:
    words = {w.casefold() for w in _WORD_RE.findall(question or "")}
    return {w for w in words if w not in _EVIDENCE_STOPWORDS}


def _split_evidence_blocks(text: str) -> list[str]:
    """Split a tool payload into rankable chunks."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"\n\s*---+\s*\n", text)
    blocks: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) > 900:
            # Prefer session/bullet-sized units when the dump is huge.
            chunks = re.split(r"\n(?=(?:\[\d|\- |\* |Session |summary:|task:))", part, flags=re.IGNORECASE)
            for chunk in chunks:
                chunk = chunk.strip()
                if chunk:
                    blocks.append(chunk)
        else:
            blocks.append(part)
    return blocks or [text]


def _score_evidence_block(block: str, terms: set[str]) -> float:
    if not terms:
        return 0.0
    low = block.casefold()
    hits = sum(1 for t in terms if t in low)
    return hits / float(len(terms))


def _build_evidence_card(
    question: str,
    tool_results: list[tuple[str, str]],
    *,
    max_chars: int = MAX_EVIDENCE_CARD_CHARS,
) -> str:
    """Harness-built Phase B card: ranked excerpts, not a raw tool dump."""
    if not tool_results:
        return "EVIDENCE: (none — no tools returned data this turn.)"

    terms = _question_terms(question)
    scored: list[tuple[float, str, str]] = []
    for name, payload in tool_results:
        for block in _split_evidence_blocks(str(payload or "")):
            scored.append((_score_evidence_block(block, terms), name, block.strip()))

    if not scored:
        return "EVIDENCE: (tools returned empty payloads.)"

    scored.sort(key=lambda row: (-row[0],))
    matched = [row for row in scored if row[0] > 0]
    pool = matched if matched else scored

    header = (
        f"EVIDENCE (ranked for question terms) — {len(matched)} of {len(scored)} blocks matched:"
        if matched
        else "EVIDENCE (no strong keyword match — showing top tool excerpts):"
    )
    lines = [header]
    related_lines: list[str] = []
    budget = max(200, max_chars)

    def _snip(block: str, limit: int = 360) -> str:
        block = re.sub(r"\s+", " ", block).strip()
        if len(block) <= limit:
            return block
        return block[: limit - 1].rstrip() + "…"

    for score, name, block in pool:
        entry = f"- [{name}] {_snip(block)}"
        tentative = "\n".join(lines + [entry])
        if len(tentative) > budget - 120:
            break
        lines.append(entry)

    if matched:
        zeros = [row for row in scored if row[0] <= 0]
        for _score, name, block in zeros[:4]:
            entry = f"- [{name}] {_snip(block, 220)}"
            tentative = "\n".join(lines + ["", "RELATED (weaker match):"] + related_lines + [entry])
            if len(tentative) > budget:
                break
            related_lines.append(entry)
        if related_lines:
            lines.extend(["", "RELATED (weaker match):", *related_lines])

    card = "\n".join(lines)
    if len(card) > max_chars:
        card = card[: max_chars - 1].rstrip() + "…"
    return card


def _build_retrieve_system(
    user_message: str,
    *,
    prefetch_available: bool,
    prefetch_status: str,
) -> str:
    now = time.localtime()
    dt_str = time.strftime("%A %B %d, %Y at %H:%M", now)
    tool_policy = (
        _TOOL_POLICY_WITH_PREFETCH if prefetch_available else _TOOL_POLICY_NO_PREFETCH
    )
    status = (prefetch_status or "").strip() or (
        "No prefetched bundle this turn. Use search_sessions / search_events as needed."
    )
    return _RETRIEVE_SYSTEM_TEMPLATE.format(
        USER_NAME=get_user_name() or "the user",
        datetime=dt_str,
        question=(user_message or "").strip() or "(empty)",
        prefetch_status=status,
        workspace_roots=format_roots_for_prompt(),
        tool_policy=tool_policy,
    )


def _build_answer_phase_messages(
    user_message: str,
    evidence_card: str,
    conversation_id: str,
) -> list[dict]:
    """Fresh short context for Phase B — no tool dump, no Phase A transcript."""
    now = time.localtime()
    dt_str = time.strftime("%A %B %d, %Y at %H:%M", now)
    system = _ANSWER_SYSTEM_TEMPLATE.format(
        USER_NAME=get_user_name() or "the user",
        datetime=dt_str,
    )

    history_note = ""
    try:
        recent = get_recent_chats(conversation_id, limit=4)
        visible: list[str] = []
        for turn in recent:
            # Skip the user turn we just persisted for this question.
            if turn.get("role") == "user" and (turn.get("content") or "").strip() == (user_message or "").strip():
                continue
            text = _visible_chat_content(turn.get("role", ""), turn.get("content") or "")
            if not text:
                continue
            label = "User" if turn.get("role") == "user" else "Clippy"
            visible.append(f"{label}: {text[:240]}")
        # Keep at most last 2 visible prior turns.
        visible = visible[-2:]
        if visible:
            history_note = "Recent context (optional):\n" + "\n".join(visible) + "\n\n"
    except Exception:
        history_note = ""

    user_block = (
        f"{history_note}"
        f"Question:\n{(user_message or '').strip()}\n\n"
        f"{evidence_card}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_block},
    ]


def _stream_ollama(
    messages: list[dict],
    schemas: list[dict] | None = None,
    *,
    think: bool = True,
):
    """Stream one Ollama step.

    Yields:
      ("thinking", str_delta)
      ("content", str_delta)
      ("final", message_dict)  — always last

    schemas=None → default TOOL_SCHEMAS.
    schemas=[]   → no tools (Phase B answer).
    """
    if schemas is None:
        tools = list(TOOL_SCHEMAS)
    elif len(schemas) == 0:
        tools = None
    else:
        tools = schemas
    thinking = ""
    content = ""
    tool_calls = []

    def merge_tool_call_deltas(incoming: list[dict]) -> None:
        """Assemble OpenAI-compatible streamed tool-call fragments.

        Ollama normally emits a complete tool call near the end of its stream,
        but assembling fragments here also tolerates fields split across chunks.
        """
        for call in incoming:
            index = call.get("index", len(tool_calls))
            try:
                index = int(index)
            except (TypeError, ValueError):
                index = len(tool_calls)
            while len(tool_calls) <= index:
                tool_calls.append({
                    "id": None,
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
            target = tool_calls[index]
            if call.get("id"):
                target["id"] = call["id"]
            function = call.get("function") or {}
            if function.get("name"):
                target["function"]["name"] = function["name"]
            arguments = function.get("arguments", "")
            if isinstance(arguments, dict):
                target["function"]["arguments"] = arguments
            elif arguments:
                current = target["function"].get("arguments", "")
                if isinstance(current, dict):
                    current = json.dumps(current)
                target["function"]["arguments"] = f"{current}{arguments}"

    for chunk in gateway.chat_stream(
        messages, MODEL,
        tools=tools,
        priority=Priority.INTERACTIVE,
        timeout=180,
        think=think,
    ):
        gateway_status = chunk.get("_gateway_status")
        if gateway_status:
            yield ("status", gateway_status)
            continue
        msg = chunk.get("message") or {}
        t_delta = msg.get("thinking") or ""
        c_delta = msg.get("content") or ""
        if t_delta:
            thinking += t_delta
            yield ("thinking", t_delta)
        if c_delta:
            content += c_delta
            yield ("content", c_delta)
        if msg.get("tool_calls"):
            merge_tool_call_deltas(msg["tool_calls"])

    normalized_tool_calls = []
    for call in tool_calls:
        function = call.get("function") or {}
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                arguments = {}
        normalized_tool_calls.append({
            "id": call.get("id"),
            "type": call.get("type", "function"),
            "function": {
                "name": function.get("name", ""),
                "arguments": arguments,
            },
        })

    raw_msg = {
        "role": "assistant",
        "content": content,
        "thinking": thinking,
    }
    if normalized_tool_calls:
        raw_msg["tool_calls"] = normalized_tool_calls
    yield ("final", raw_msg)


def _format_assistant_content(thinking: str, answer: str) -> str:
    """Persist thinking alongside the visible answer for later UI reload."""
    thinking = (thinking or "").strip()
    answer = (answer or "").strip()
    if thinking:
        return f"<thinking>\n{thinking}\n</thinking>\n\n{answer}"
    return answer


def _compress_old_tool_messages(messages: list[dict], keep_last: int = 1) -> None:
    """Compress old tool messages to keep only the last N."""
    tool_indices = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    for i in tool_indices[:-keep_last]:
        content = messages[i]["content"]
        row_count = max(0, content.count("\n"))
        messages[i]["content"] = f"[prior tool result: ~{row_count} rows, already processed]"


def _reanchor_current_question(messages: list[dict], user_message: str) -> None:
    """Keep the live user ask at the end of the thread after tool rounds.

    Small tool-calling models often re-attend to earlier greeting turns once a
    large tool payload arrives. A single trailing reminder beats raising
    router thresholds or other cello-tape.
    """
    question = (user_message or "").strip()
    if not question:
        return
    # Drop any previous anchors from earlier steps in this turn.
    while messages and messages[-1].get("role") == "user" and str(
        messages[-1].get("content") or ""
    ).startswith(_QUESTION_ANCHOR_PREFIX):
        messages.pop()
    messages.append({
        "role": "user",
        "content": f"{_QUESTION_ANCHOR_PREFIX}{question}",
    })


def _prepare_turn(user_message: str, conversation_id: str):
    """Shared setup for run / run_stream: persist user turn, embed, prefetch, seed messages.

    Returns (messages, active_tools, schemas, user_message, mode) where mode is
    "retrieve" (Phase A tool loop → Phase B answer) or "direct" (single-shot casual).
    """
    save_chat(conversation_id=conversation_id, role="user", content=user_message)

    combined = _build_combined_query_context(conversation_id, user_message)
    q_vec: list | None = None
    try:
        q_vec = embed_text(combined)
    except Exception:
        pass

    prefetch_context = ""
    prefetch_routes: list[str] = []
    decision, confidence = classify_query(user_message)
    if decision is None and _SHORT_FOLLOW_UP_RE.match(user_message):
        decision, confidence = classify_query(combined)

    if decision:
        print(f"[router] {decision.primary} (conf={confidence:.2f}) secondary={decision.secondary}")

    if decision and should_prefetch(decision, confidence):
        try:
            prefetch_context, prefetch_routes = _run_prefetch(
                decision, user_message, combined, q_vec
            )
            print(
                f"[prefetch] {decision.primary} → {len(prefetch_context)} chars "
                f"routes={prefetch_routes}"
            )
        except Exception as e:
            print(f"[prefetch] ERROR — {e}")
            prefetch_context, prefetch_routes = "", []

    active_tools = dict(TOOLS)
    schemas = [dict(s) for s in TOOL_SCHEMAS]
    prefetch_available = bool((prefetch_context or "").strip())

    if prefetch_available:
        empty = _prefetch_looks_empty(prefetch_context)
        synopsis = _prefetch_synopsis(prefetch_context, prefetch_routes)
        bundle = prefetch_context  # closure copy for the tool

        def get_prefetched_context(**_kwargs) -> str:
            return bundle

        active_tools["get_prefetched_context"] = get_prefetched_context
        schemas.append(
            build_prefetch_tool_schema(
                routes=prefetch_routes,
                char_count=len(prefetch_context),
                synopsis=synopsis,
                empty=empty,
            )
        )
        status = (
            f"get_prefetched_context is AVAILABLE (~{len(prefetch_context)} chars; "
            f"routes={','.join(prefetch_routes) or 'n/a'}). "
            f"Synopsis: {synopsis} "
            "Call it before search_* for matching activity/memory questions. "
            "Skip it for open-only actions or casual chat."
        )
        print(f"[prefetch] exposed as tool — empty={empty} synopsis={synopsis[:160]}")
    else:
        status = (
            "No prefetched bundle this turn. "
            "Use search_sessions / search_events for activity questions."
        )

    # Non-casual / prefetched turns use retrieve→evidence→answer.
    # Pure casual (no bundle) keeps the full personality prompt in one shot.
    use_retrieve = bool(
        prefetch_available
        or (decision is not None and decision.primary != "casual")
    )
    mode = "retrieve" if use_retrieve else "direct"

    if mode == "retrieve":
        messages = [
            {
                "role": "system",
                "content": _build_retrieve_system(
                    user_message,
                    prefetch_available=prefetch_available,
                    prefetch_status=status,
                ),
            },
            {"role": "user", "content": user_message},
        ]
        print("[agent] mode=retrieve (Phase A tools → Phase B answer)")
    else:
        messages = [
            {
                "role": "system",
                "content": _build_system_prompt(
                    conversation_id,
                    user_message,
                    q_vec=q_vec,
                    prefetch_available=prefetch_available,
                    prefetch_status=status,
                ),
            },
            {"role": "user", "content": user_message},
        ]
        print("[agent] mode=direct (single-shot)")

    return messages, active_tools, schemas, user_message, mode


def _finalize_answer(user_message: str, conversation_id: str, thinking: str, answer: str) -> str:
    stored = _format_assistant_content(thinking, answer)
    save_chat(conversation_id=conversation_id, role="assistant", content=stored)
    threading.Thread(
        target=ingest_conversation,
        args=(user_message, answer),
        daemon=True,
    ).start()
    threading.Thread(
        target=maybe_summarize,
        args=(conversation_id,),
        daemon=True,
    ).start()
    return answer


def _run_answer_phase(
    user_message: str,
    conversation_id: str,
    tool_results: list[tuple[str, str]],
):
    """Phase B: fresh context, harness evidence card, no tools, think=False."""
    evidence = _build_evidence_card(user_message, tool_results)
    print(f"[agent] Phase B evidence card ({len(evidence)} chars)\n{evidence[:600]}")
    messages = _build_answer_phase_messages(user_message, evidence, conversation_id)

    thinking = ""
    content = ""
    raw_msg = None
    yield {"type": "status", "text": "Answering"}

    for kind, payload in _stream_ollama(messages, schemas=[], think=False):
        if kind == "thinking":
            thinking += payload
            yield {"type": "thinking", "delta": payload}
        elif kind == "status":
            yield {"type": "status", "text": payload}
        elif kind == "content":
            content += payload
            yield {"type": "content", "delta": payload}
        elif kind == "final":
            raw_msg = payload

    if raw_msg is None:
        raw_msg = {"role": "assistant", "content": content, "thinking": thinking}

    thinking = (raw_msg.get("thinking") or thinking or "").strip()
    content = (raw_msg.get("content") or content or "").strip()
    _finalize_answer(user_message, conversation_id, thinking, content)
    yield {"type": "done", "result": content}


def run_stream(user_message: str, conversation_id: str):
    """Yield SSE-ready event dicts while running retrieve → evidence → answer."""
    yield {"type": "status", "text": "Preparing response"}
    messages, active_tools, schemas, user_message, mode = _prepare_turn(
        user_message, conversation_id
    )

    tool_results: list[tuple[str, str]] = []
    used_tools = False
    max_steps = MAX_RETRIEVE_STEPS if mode == "retrieve" else MAX_STEPS

    for step in range(max_steps):
        _compress_old_tool_messages(messages)

        thinking = ""
        content = ""
        raw_msg = None
        content_started = False

        # Phase A retrieve: keep think on for tool planning; never stream prose answers.
        # Direct casual: stream as before.
        stream_content = mode == "direct"

        for kind, payload in _stream_ollama(messages, schemas=schemas, think=True):
            if kind == "thinking":
                thinking += payload
                yield {"type": "thinking", "delta": payload}
            elif kind == "status":
                yield {"type": "status", "text": payload}
            elif kind == "content":
                content += payload
                if stream_content:
                    content_started = True
                    yield {"type": "content", "delta": payload}
            elif kind == "final":
                raw_msg = payload

        if raw_msg is None:
            raw_msg = {"role": "assistant", "content": content, "thinking": thinking}

        thinking = (raw_msg.get("thinking") or thinking or "").strip()
        content = (raw_msg.get("content") or content or "").strip()
        tool_calls = raw_msg.get("tool_calls") or []

        if thinking:
            print(f"\n[think]\n{thinking}\n[/think]\n")

        if not tool_calls:
            # Retrieve turn that already gathered tools → discard Phase A prose; answer from card.
            if mode == "retrieve" and used_tools:
                if content_started:
                    yield {"type": "reset_content"}
                yield from _run_answer_phase(user_message, conversation_id, tool_results)
                return

            # No tools this turn — use model content (casual / answered without retrieve).
            if content and not content_started:
                yield {"type": "content", "delta": content}
            _finalize_answer(user_message, conversation_id, thinking, content)
            yield {"type": "done", "result": content}
            return

        # Tool step — drop any speculative answer text from the UI
        used_tools = True
        if content_started:
            yield {"type": "reset_content"}
        yield {"type": "status", "text": "Using tools"}
        messages.append(raw_msg)

        for tc in tool_calls:
            name = tc["function"]["name"]
            arguments = tc["function"]["arguments"]
            if not isinstance(arguments, dict):
                arguments = {}
            print(f"[tool] {name}({arguments})")

            if name not in active_tools:
                result = f"Error: unknown tool '{name}'. Available: {list(active_tools.keys())}"
                print(f"[tool] ERROR — {result}")
            else:
                try:
                    result = active_tools[name](**arguments)
                    print(f"[tool result]\n{str(result)[:800]}\n[/tool result]")
                except TypeError:
                    # get_prefetched_context takes no args; some models pass {}
                    try:
                        result = active_tools[name]()
                        print(f"[tool result]\n{str(result)[:800]}\n[/tool result]")
                    except Exception as exc:
                        result = f"Error: tool '{name}' raised {type(exc).__name__}: {exc}"
                        print(f"[tool] ERROR — {result}")
                except Exception as exc:
                    result = f"Error: tool '{name}' raised {type(exc).__name__}: {exc}"
                    print(f"[tool] ERROR — {result}")

            capped = _cap_tool_result(str(result))
            tool_results.append((name, capped))
            messages.append({"role": "tool", "content": capped})

        _reanchor_current_question(messages, user_message)
        yield {"type": "status", "text": "Thinking"}

    # Step budget exhausted.
    if mode == "retrieve" and used_tools:
        print(f"[agent] retrieve step cap ({max_steps}) — forcing Phase B from gathered tools")
        yield from _run_answer_phase(user_message, conversation_id, tool_results)
        return

    print(f"[agent] WARNING — hit MAX_STEPS ({max_steps}) without a final answer")
    fallback = "I wasn't able to produce an answer within the step limit. Try rephrasing your question."
    _finalize_answer(user_message, conversation_id, "", fallback)
    yield {"type": "content", "delta": fallback}
    yield {"type": "done", "result": fallback}


def run(user_message: str, conversation_id: str) -> str:
    """Run the ReAct agent loop for a single user turn (non-streaming)."""
    result = ""
    for event in run_stream(user_message, conversation_id):
        if event.get("type") == "done":
            result = event.get("result") or ""
    return result


if __name__ == "__main__":
    print("Clippy Vision Agent (type 'exit' to quit)\n")
    conversation_id = str(uuid.uuid4())  # one ID for the whole session
    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() == "exit":
            break
        answer = run(user_input, conversation_id)
        print(f"\nAgent: {answer}\n")
