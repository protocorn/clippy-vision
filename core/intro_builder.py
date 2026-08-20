"""Weekly auto-rebuild of the autobiographical introduction narrative.

Identity-first + fact-delta inputs; skips user-authored intros; gated by
7-day cadence and a minimum delta of identity/fact changes.
"""

from __future__ import annotations

import json
import threading
import time

from core.llm_gateway import Priority, gateway
from core.memory_store import (
    count_facts_since,
    count_identity_updates_since,
    get_all_clusters,
    get_fact_delta_since,
    get_introduction_meta,
    get_profile,
    set_introduction,
)

MODEL = "qwen3:8b"

REBUILD_INTERVAL_SECONDS = 7 * 24 * 60 * 60
MIN_FACT_DELTA = 8
FACT_DELTA_CAP = 40
MAX_INTRO_CHARS = 1200

_INTRO_SYSTEM = """
You have towrite a short autobiographical profile of a person for an AI assistant to use.

You receive:
1. CURRENT INTRODUCTION — previous narrative (may be empty)
2. IDENTITY FIELDS — structured durable facts (canonical)
3. MEMORY CLUSTERS — topic labels describing what is known
4. RECENT FACT DELTA — new facts since the last rewrite (supplementary)

Write a single cohesive introduction (120–200 words, soft max 1200 characters).

Rules:
- Durable identity only: who they are, where they live/study/work, skills,
  preferences, goals, ongoing projects, relationships, major background.
- Do NOT list every identity field; synthesize into prose.
- Prefer IDENTITY FIELDS over recent facts when they conflict.
- Skip situational noise ("asked about X today", one-off debugging).
- Keep continuity with CURRENT INTRODUCTION when present (same person voice).
- Default to third person ("They are…" / use their name if known).
- If CURRENT INTRODUCTION uses first person, keep first person.
- If there is almost no signal, return a very short honest stub, not fiction.
- Return JSON only: {"introduction": "..."}.
"""

_INTRO_SCHEMA = {
    "type": "object",
    "properties": {
        "introduction": {"type": "string"},
    },
    "required": ["introduction"],
}

_lock = threading.Lock()
_started = False


def gather_intro_inputs() -> dict:
    meta = get_introduction_meta()
    since = float(meta.get("updated_at") or 0)
    profile = get_profile()
    identity = dict(profile["identity"])
    if profile["name"]:
        identity["name"] = profile["name"]
    clusters = get_all_clusters()
    fact_delta = get_fact_delta_since(since, limit=FACT_DELTA_CAP)
    return {
        "meta": meta,
        "identity": identity,
        "clusters": [
            {
                "label": c["label"],
                "description": c["description"],
                "fact_count": c["fact_count"],
            }
            for c in clusters
        ],
        "fact_delta": fact_delta,
        "identity_updates": count_identity_updates_since(since),
        "fact_updates": count_facts_since(since),
    }


def should_rebuild_introduction(inputs: dict | None = None) -> bool:
    data = inputs if inputs is not None else gather_intro_inputs()
    meta = data["meta"]
    source = (meta.get("source") or "").strip().lower()
    if source == "user":
        return False

    updated_at = float(meta.get("updated_at") or 0)
    value = (meta.get("value") or "").strip()
    now = time.time()


    # First intro: rebuild when any identity or facts exist
    if not value or updated_at <= 0:
        return bool(data["identity"] or data["clusters"] or data["fact_delta"])

    if now - updated_at < REBUILD_INTERVAL_SECONDS:
        return False

    return data["identity_updates"] >= 1 or data["fact_updates"] >= MIN_FACT_DELTA


def _format_builder_prompt(inputs: dict) -> str:
    meta = inputs["meta"]
    current = (meta.get("value") or "").strip() or "(empty — no introduction yet)"

    identity = inputs["identity"]
    if identity:
        identity_text = "\n".join(f"{k}: {v}" for k, v in identity.items())
    else:
        identity_text = "(empty)"

    clusters = inputs["clusters"]
    if clusters:
        cluster_text = "\n".join(
            f"- [{c['label']}] {c['description']} ({c['fact_count']} facts)"
            for c in clusters
        )
    else:
        cluster_text = "(none)"

    facts = inputs["fact_delta"]
    if facts:
        fact_text = "\n".join(f"- {f}" for f in facts)
    else:
        fact_text = "(none since last rewrite)"

    return (
        f"CURRENT INTRODUCTION:\n{current}\n\n"
        f"IDENTITY FIELDS:\n{identity_text}\n\n"
        f"MEMORY CLUSTERS:\n{cluster_text}\n\n"
        f"RECENT FACT DELTA:\n{fact_text}"
    )


def rebuild_introduction(inputs: dict | None = None) -> str | None:
    """Run the LLM rewrite and persist. Returns new text, or None on skip/failure."""
    data = inputs if inputs is not None else gather_intro_inputs()
    if not should_rebuild_introduction(data):
        return None

    prompt = _format_builder_prompt(data)
    try:
        body = gateway.chat(
            [
                {"role": "system", "content": _INTRO_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            MODEL,
            format=_INTRO_SCHEMA,
            think=False,
            options={"temperature": 0.2},
            priority=Priority.BACKGROUND,
        )
    except Exception as e:
        print(f"\n  [INTRO] rebuild failed: {e}")
        return None

    if not body:
        print("\n  [INTRO] rebuild failed: empty gateway response")
        return None

    content = body.get("message", {}).get("content")
    try:
        result = json.loads(content) if isinstance(content, str) else content
        text = (result.get("introduction") or "").strip()
    except Exception as e:
        print(f"\n  [INTRO] rebuild parse failed: {e}")
        return None

    if not text:
        return None

    if len(text) > MAX_INTRO_CHARS:
        text = text[: MAX_INTRO_CHARS - 1].rsplit(" ", 1)[0] + "…"


    # Re-fetch source after LLM so a mid-flight Settings save is not overwritten
    if (get_introduction_meta().get("source") or "").strip().lower() == "user":
        print("\n  [INTRO] skipped write — user-authored introduction protected")
        return None

    set_introduction(text, source="distiller")
    print(f"\n  [INTRO] rebuilt ({len(text)} chars)")
    return text


def maybe_rebuild_introduction() -> str | None:
    """Public entry: gather, gate, rebuild if due. Safe to call from background."""
    with _lock:
        try:
            inputs = gather_intro_inputs()
            if not should_rebuild_introduction(inputs):
                return None
            return rebuild_introduction(inputs)
        except Exception as e:
            print(f"\n  [INTRO] maybe_rebuild error: {e}")
            return None


def start_intro_rebuild_daemon(check_every_seconds: int = 6 * 60 * 60) -> None:
    """Start a background loop that periodically runs maybe_rebuild_introduction.

    Also runs one check immediately on start. Idempotent across calls.
    """
    global _started
    with _lock:
        if _started:
            return
        _started = True

    def _loop():
        while True:
            maybe_rebuild_introduction()
            time.sleep(check_every_seconds)

    threading.Thread(target=_loop, daemon=True, name="intro-rebuild").start()
