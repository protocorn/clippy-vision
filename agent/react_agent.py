import json
import re
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from agent.tools import TOOLS, TOOL_SCHEMAS
from agent.memory import get_autobiographical_context
from agent.router import classify_query, should_prefetch


from core.llm_gateway import gateway, Priority
from core.local_embeddings import embed_text
from core.distil import ingest_conversation
from agent.conversation import (
    save_chat, maybe_summarize,
    get_recent_chats, get_recent_summaries, get_relevant_summaries,
)
from core.memory_store import get_unresolved_conflicts

from core.storage import get_user_name
from agent.prefetch.topic_search import topic_search
from agent.prefetch.specific_recall import specific_recall
from agent.prefetch.time_anchor import time_anchor_fetch
from agent.prefetch.memory_query import memory_query
from agent.helpers.time_resolver import resolve_temporal_range

MODEL     = "qwen3:8b"
MAX_STEPS = 10



USER_MESSAGE_MAX_CHARS = 4000


_PREFETCHABLE = {"time_anchored", "topic_search", "specific_recall", "memory_query"}
MAX_PREFETCH_CONTEXT_CHARS = 8000
_SHORT_FOLLOW_UP_RE = re.compile(
    r"^\s*(?:nothing else|anything else|what else|and|more|go on|continue|that one|"
    r"what about that|what about it)\s*[?.!]*\s*$",
    re.IGNORECASE,
)

_TOOL_POLICY_PREFETCH = """Tool Policy:
- <prefetch_context> above is a concise starting point for this query. Read it first, but treat it
  as evidence rather than a complete answer.
- Search tools remain available. Use search_events or search_sessions when the context is truncated,
  missing an exact artifact, ambiguous, or when the user asks for more detail. Do not repeat a search
  that the context already answers.
- Use save_identity, save_note, and delete_note immediately when the user asks you to remember or
  forget something."""

_TOOL_POLICY_FALLBACK = """Tool Policy:
- No context was pre-fetched. Use tools to retrieve data before answering.
- Activity questions (time refs, "what did I work on", "what was I doing"):
  call search_sessions first for summaries, then search_events for granular detail.
- For URLs, clipboard, OCR text, or any specific artifact: call search_events directly.
- If one tool returns no results, try the other before giving up.
- For ANY link, URL, article, or browser activity question: search_events only —
  session summaries never contain raw URLs.
- When a prior result already has an exact timestamp, use that date in the next call.
- save_identity / save_note: call immediately when the user shares personal info or asks
  you to remember something.
- delete_note: call immediately when the user asks you to forget something.
- Do not call activity tools for casual chat or general knowledge questions."""

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

<prefetch_context>
{prefetch_context}
</prefetch_context>

Core Rules:
- <user_profile> is always present. Use it for identity and preference questions without calling any tools.
- Keep explicit personal facts separate from observed activity. App usage can support "what was I doing" but must not be presented as a fact about the user's identity.
- A zero explicit-fact count does not mean there is no memory. If activity, screenshots, conversations, or session counts are present, describe those separately and accurately.
- For an exact screenshot question, describe only the exact screenshot evidence. Never blend nearby before/after activity into what was visible in that frame.
- If exact screenshot evidence includes screenshot_source, the image is locally available. Do not claim the image content or file is unavailable.
- Do not invent activity history, timestamps, files, websites, apps, or user intentions.
- If evidence is weak, partial, or missing, say so plainly.
- Address the user naturally. Use their name occasionally, not repeatedly.
- When the user asks a follow-up referencing something already in <conversation_history>, use that context directly.
- Never mention internal system terms in your response: do not say "prefetch context", "prefetch_context",
  "tool result", "activity summaries", "<prefetch_context>", or any other implementation detail.
  Present all information naturally as if you simply know it.

{tool_policy}

Response Style:
- Be concise by default. Use 1-3 sentences for simple answers.
- Avoid repetitive greetings, generic follow-up offers, and decorative emoji.
- Give detailed answers when the user asks for analysis, planning, comparison, or debugging.
- Do not expose raw SQL, tiers, internal tool names, or implementation details unless the user asks."""



def _build_combined_query_context(conversation_id: str, user_message: str) -> str:

    recent_turns = get_recent_chats(conversation_id, limit=3)
    if not recent_turns:
        return user_message

    prior = " | ".join(
        f"{'User' if t['role'] == 'user' else 'Clippy'}: {t['content']}"
        for t in recent_turns
    )
    return f"User: {user_message} | Prior turns: {prior}"

def _build_conversation_history(conversation_id: str, q_vec: list|None=None) -> str:
    """Assemble the conversation history block for the system prompt.

    Tier 1 (always): last 2 rolling summaries + last 4 raw turns.
    Tier 2 (when deep): up to 2 older summaries retrieved by cosine similarity.
    """
    parts = []



    if q_vec:
        try:
            deep = get_relevant_summaries(conversation_id, q_vec)
            if deep:
                parts.append("[Earlier relevant context]\n" + "\n\n".join(deep))
        except Exception:
            pass


    recent_summaries = get_recent_summaries(conversation_id)
    if recent_summaries:
        parts.append("[Recent summary]\n" + "\n\n".join(recent_summaries))


    recent_turns = get_recent_chats(conversation_id)
    if recent_turns:
        lines = []
        for t in recent_turns:
            label = "User" if t["role"] == "user" else "Clippy"
            lines.append(f"{label}: {t['content']}")
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
    prefetch_context: str = "",
) -> str:
    now    = time.localtime()
    dt_str = time.strftime("%A %B %d, %Y at %H:%M", now)

    user_profile = get_autobiographical_context(q_vec=q_vec)
    if not user_profile:
        user_profile = "No profile data yet."

    conflict_notice = _build_conflict_notice()
    ctx = prefetch_context or "No pre-fetched context for this query."
    if conflict_notice:
        ctx += "\n\n" + conflict_notice

    tool_policy = _TOOL_POLICY_PREFETCH if prefetch_context else _TOOL_POLICY_FALLBACK

    history = _build_conversation_history(conversation_id, q_vec) if user_message else "No conversation history yet."

    return _SYSTEM_PROMPT_TEMPLATE.format(
        USER_NAME=get_user_name() or "the user",
        datetime=dt_str,
        user_profile=user_profile,
        prefetch_context=ctx,
        tool_policy=tool_policy,
        conversation_history=history,
    )


def _fetch_single_route(
    route: str,
    temporal_range,
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


def _run_prefetch(decision, query: str, combined: str, q_vec: list) -> str:
    """Fire prefetch for primary + all secondary routes in parallel.

    Time handling:
    - Resolve temporal range once from `combined` (enriched with recent turns).
    - If time_anchored is PRIMARY  → run time_anchor_fetch to get the session view.
    - If time_anchored is SECONDARY → pass temporal_range as a filter to primary;
      do NOT run a separate time_anchor_fetch (the range narrows, not supplements).
    """

    all_routes = {decision.primary} | set(decision.secondary)
    needs_time = "time_anchored" in all_routes
    temporal_range = resolve_temporal_range(combined) if needs_time else None




    primary_routes = [decision.primary] if decision.primary in _PREFETCHABLE else []
    secondary_routes = [
        s for s in decision.secondary
        if s in _PREFETCHABLE and s != decision.primary and s != "time_anchored"
    ]
    routes_to_run = primary_routes + secondary_routes
    print(f"[prefetch] routes: {routes_to_run}")


    if len(routes_to_run) == 1:
        return _limit_prefetch_context(
            _fetch_single_route(routes_to_run[0], temporal_range, query, combined, q_vec)
        )


    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(routes_to_run)) as ex:
        future_to_route = {
            ex.submit(_fetch_single_route, r, temporal_range, query, combined, q_vec): r
            for r in routes_to_run
        }
        for future in as_completed(future_to_route):
            route  = future_to_route[future]
            result = future.result()
            print(f"[prefetch]   {route} → {len(result)} chars")
            results[route] = result



    combined_context = "\n\n---\n\n".join(
        results[route] for route in routes_to_run if results.get(route)
    )
    return _limit_prefetch_context(combined_context)


def _limit_prefetch_context(context: str) -> str:
    """Bound retrieval context so one broad query cannot crowd out reasoning."""
    context = (context or "").strip()
    if len(context) <= MAX_PREFETCH_CONTEXT_CHARS:
        return context
    return (
        context[:MAX_PREFETCH_CONTEXT_CHARS].rstrip()
        + "\n\n[Retrieved context truncated for space. Use the search tools for missing detail.]"
    )


def _stream_ollama(messages: list[dict], prefetch_active: bool = False):
    """Stream one Ollama step.

    Yields:
      ("thinking", str_delta)
      ("content", str_delta)
      ("final", message_dict)  — always last
    """


    schemas = TOOL_SCHEMAS
    thinking = ""
    content = ""
    tool_calls = []

    def merge_tool_call_deltas(incoming: list[dict]) -> None:
        """Assemble OpenAI-compatible streamed tool-call fragments.

        Ollama emits a complete tool call near the end of its stream, while
        OpenAI-compatible servers commonly emit the name and JSON arguments in
        separate deltas. Keeping the normalization here lets both providers
        feed the same agent loop.
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
        tools=schemas,
        priority=Priority.INTERACTIVE,
        timeout=180,
        think=True,
    ):
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


def _prepare_turn(user_message: str, conversation_id: str):
    """Shared setup for run / run_stream: persist user turn, embed, prefetch, seed messages."""
    save_chat(conversation_id=conversation_id, role="user", content=user_message)

    combined = _build_combined_query_context(conversation_id, user_message)
    q_vec: list | None = None
    try:
        q_vec = embed_text(combined)
    except Exception:
        pass

    prefetch_context = ""
    decision, confidence = classify_query(user_message)
    if decision is None and _SHORT_FOLLOW_UP_RE.match(user_message):
        decision, confidence = classify_query(combined)

    if decision:
        print(f"[router] {decision.primary} (conf={confidence:.2f}) secondary={decision.secondary}")

    if decision and should_prefetch(decision, confidence):
        try:
            prefetch_context = _run_prefetch(decision, user_message, combined, q_vec)
            print(f"[prefetch] {decision.primary} → {len(prefetch_context)} chars")
        except Exception as e:
            print(f"[prefetch] ERROR — {e}")

    prefetch_active = bool(prefetch_context)


    active_tools = TOOLS

    messages = [
        {"role": "system", "content": _build_system_prompt(
            conversation_id, user_message, q_vec=q_vec, prefetch_context=prefetch_context
        )},
        {"role": "user", "content": user_message},
    ]
    return messages, active_tools, prefetch_active, user_message


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


def run_stream(user_message: str, conversation_id: str):
    """Yield SSE-ready event dicts while running the ReAct loop with streamed thinking."""
    yield {"type": "status", "text": "Thinking"}
    messages, active_tools, prefetch_active, user_message = _prepare_turn(user_message, conversation_id)

    for step in range(MAX_STEPS):
        _compress_old_tool_messages(messages)

        thinking = ""
        content = ""
        raw_msg = None

        content_started = False

        for kind, payload in _stream_ollama(messages, prefetch_active=prefetch_active):
            if kind == "thinking":
                thinking += payload
                yield {"type": "thinking", "delta": payload}
            elif kind == "content":
                content += payload

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
            if content and not content_started:
                yield {"type": "content", "delta": content}
            _finalize_answer(user_message, conversation_id, thinking, content)
            yield {"type": "done", "result": content}
            return


        if content_started:
            yield {"type": "reset_content"}
        yield {"type": "status", "text": "Using tools"}
        messages.append(raw_msg)

        for tc in tool_calls:
            name      = tc["function"]["name"]
            arguments = tc["function"]["arguments"]
            print(f"[tool] {name}({arguments})")

            if name not in active_tools:
                result = f"Error: unknown tool '{name}'. Available: {list(active_tools.keys())}"
                print(f"[tool] ERROR — {result}")
            else:
                try:
                    result = active_tools[name](**arguments)
                    print(f"[tool result]\n{str(result)[:800]}\n[/tool result]")
                except Exception as exc:
                    result = f"Error: tool '{name}' raised {type(exc).__name__}: {exc}"
                    print(f"[tool] ERROR — {result}")

            messages.append({"role": "tool", "content": str(result)})

        yield {"type": "status", "text": "Thinking"}

    print(f"[agent] WARNING — hit MAX_STEPS ({MAX_STEPS}) without a final answer")
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
    conversation_id = str(uuid.uuid4())
    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() == "exit":
            break
        answer = run(user_input, conversation_id)
        print(f"\nAgent: {answer}\n")
