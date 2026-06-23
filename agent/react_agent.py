import json
import time
import uuid
import threading
from agent.tools import TOOLS, TOOL_SCHEMAS
from agent.memory import get_autobiographical_context, semantic_memory_context

from core.llm_gateway import gateway, Priority
from core.distil import ingest_conversation
from agent.conversation import (
    save_chat, maybe_summarize,
    get_recent_chats, get_recent_summaries, get_relevant_summaries,
)

MODEL     = "qwen3:8b"
MAX_STEPS = 10
EMBED_MODEL = "nomic-embed-text"
# - When the user asks a follow-up that references something established earlier in the conversation (e.g. a time, place, or calculation), use that context directly — do not say evidence is unavailable if it is already in <conversation_history>.

_SYSTEM_PROMPT_TEMPLATE = """You are Clippy, Sahil's local personal AI assistant.

Your job is to answer from evidence using local memory and activity data. Be accurate before being confident.

Current date and time: {datetime}

<conversation_history>
{conversation_history}
</conversation_history>

<user_profile>
{user_profile}
</user_profile>

<memory_context>
{memory_context}
</memory_context>

Core Rules:
- Use injected context first when it is sufficient.
- Do not invent activity history, timestamps, files, websites, apps, or user intentions.
- If evidence is weak, partial, or missing, say so plainly.
- Address the user naturally. Use their name occasionally, not repeatedly.

Tool Policy:
- <memory_context> above is already pre-retrieved. If it answers the question, answer directly — do NOT call recall_memory or fetch_cluster again.
- recall_memory / fetch_cluster: use ONLY when the question needs a cluster not visible in <memory_context>, or the user asks about something not covered there.
- Activity search — choose the right tool first, then escalate if needed:
  • search_sessions → broad time windows, daily/weekly overviews, project topics, "what did I work on"
  • search_events  → specific messages, OCR text, URLs, clipboard, app-level detail, WhatsApp/email content
  • If a tool returns "no matching records" or its result does not contain the answer, call the OTHER tool before giving up.
  • Read the result header — it tells you which table was searched and how many rows matched. Use that to decide whether to escalate.
- Save tools: use immediately when the user asks you to remember something or shares personal identity information.
- Do not call activity tools for casual chat or general advice that requires no evidence.
- If both tools return no useful results, say so plainly — do not invent an answer.

Response Style:
- Be concise by default.
- Use 1-3 sentences for simple answers.
- Give detailed answers when the user asks for analysis, planning, comparison, debugging, or when the evidence requires nuance.
- Do not expose raw SQL, validators, tiers, or internal implementation details unless the user asks."""


def _build_conversation_history(conversation_id: str, user_message: str) -> str:
    """Assemble the conversation history block for the system prompt.

    Tier 1 (always): last 2 rolling summaries + last 4 raw turns.
    Tier 2 (when deep): up to 2 older summaries retrieved by cosine similarity.
    """
    parts = []

    # Tier 2 — semantically relevant older summaries (only when history is deep)
    try:
        q_vec = gateway.embed(user_message, embed_model=EMBED_MODEL, priority=Priority.INTERACTIVE)
        deep = get_relevant_summaries(conversation_id, q_vec)
        if deep:
            parts.append("[Earlier relevant context]\n" + "\n\n".join(deep))
    except Exception:
        pass

    # Tier 1a — recent rolling summaries
    recent_summaries = get_recent_summaries(conversation_id)
    if recent_summaries:
        parts.append("[Recent summary]\n" + "\n\n".join(recent_summaries))

    # Tier 1b — last N raw turns
    recent_turns = get_recent_chats(conversation_id)
    if recent_turns:
        lines = []
        for t in recent_turns:
            label = "User" if t["role"] == "user" else "Clippy"
            lines.append(f"{label}: {t['content']}")
        parts.append("[Recent turns]\n" + "\n".join(lines))

    return "\n\n".join(parts) if parts else "No conversation history yet."


def _build_system_prompt(conversation_id: str, user_message: str = "") -> str:
    now    = time.localtime()
    dt_str = time.strftime("%A %B %d, %Y at %H:%M", now)

    mem_ctx = semantic_memory_context(user_message) if user_message else ""
    if not mem_ctx:
        mem_ctx = "No relevant memory found for this query."

    history = _build_conversation_history(conversation_id, user_message) if user_message else "No conversation history yet."

    return _SYSTEM_PROMPT_TEMPLATE.format(
        datetime=dt_str,
        user_profile=get_autobiographical_context(),
        memory_context=mem_ctx,
        conversation_history=history,
    )


def _call_ollama(messages: list[dict]) -> dict:
    return gateway.chat(messages, MODEL, tools=TOOL_SCHEMAS, priority=Priority.INTERACTIVE, timeout=180, think=True)


def _compress_old_tool_messages(messages: list[dict], keep_last: int = 1) -> None:
    """Compress old tool messages to keep only the last N."""
    tool_indices = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    for i in tool_indices[:-keep_last]:
        content = messages[i]["content"]
        row_count = max(0, content.count("\n"))
        messages[i]["content"] = f"[prior tool result: ~{row_count} rows, already processed]"


def run(user_message: str, conversation_id: str) -> str:
    """Run the ReAct agent loop for a single user turn."""
    messages = [
        {"role": "system", "content": _build_system_prompt(conversation_id, user_message)},
        {"role": "user",   "content": user_message},
    ]

    for step in range(MAX_STEPS):
        _compress_old_tool_messages(messages)
        response = _call_ollama(messages)
        msg = response["message"]
        tool_calls = msg.get("tool_calls") or []

        thinking = msg.get("thinking", "").strip()
        if thinking:
            print(f"\n[think]\n{thinking}\n[/think]\n")

        if not tool_calls:
            answer = msg.get("content", "").strip()
            # Background: extract facts into long-term memory + build rolling summary
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

        messages.append(msg)

        for tc in tool_calls:
            name      = tc["function"]["name"]
            arguments = tc["function"]["arguments"]

            print(f"[tool] {name}({arguments})")

            if name not in TOOLS:
                result = f"Error: unknown tool '{name}'. Available: {list(TOOLS.keys())}"
                print(f"[tool] ERROR — {result}")
            else:
                try:
                    result = TOOLS[name](**arguments)
                    print(f"[tool result]\n{str(result)[:800]}\n[/tool result]")
                except Exception as exc:
                    result = f"Error: tool '{name}' raised {type(exc).__name__}: {exc}"
                    print(f"[tool] ERROR — {result}")

            messages.append({"role": "tool", "content": str(result)})

    print(f"[agent] WARNING — hit MAX_STEPS ({MAX_STEPS}) without a final answer")
    return "I wasn't able to produce an answer within the step limit. Try rephrasing your question."


if __name__ == "__main__":
    print("Clippy Vision Agent (type 'exit' to quit)\n")
    conversation_id = str(uuid.uuid4())  # one ID for the whole session
    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() == "exit":
            break
        # Save user turn BEFORE run() so history is available on this call
        save_chat(conversation_id=conversation_id, role="user", content=user_input)
        answer = run(user_input, conversation_id)
        save_chat(conversation_id=conversation_id, role="assistant", content=answer)
        print(f"\nAgent: {answer}\n")
