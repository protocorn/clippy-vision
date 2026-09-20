"""Day Card + Threads in motion — local LLM over collapsed evidence packs.

Prompts: ``docs/insight_cards_prompt_lab.md`` (FINAL generalized).
Evidence: ``agent.evidence_pack.build_day_evidence_pack`` (collapse in code).
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from agent.evidence_pack import build_day_evidence_pack
from core.chat_model import get_chat_model
from core.llm_gateway import Priority, gateway
from core.paths import get_data_dir

DAY_CARD_PROMPT = """You write an automatic DAY CARD for date={{DATE}}.

You receive an evidence pack (only source of truth):
- event_count
- app_time: [{process, minutes}, ...]
- sessions: [{start_local, end_local, active_task, summary}, ...]  // may be []
- urls: [{url, count, title}, ...]  // may be []
- title_sample: [window_title, ...]  // use when sessions is empty
- capture_hint: optional string from the packer

Hard rules:
1. Use ONLY facts present in the evidence pack. Never invent topics, people, or outcomes.
2. Do not judge productivity, mood, success, or what the user "should" do.
3. Ignore process names that look like filenames (*.jpg) or URLs.
4. Sessions are already collapsed into themes — do not invent extra themes.
5. Prefer concrete nouns from titles/summaries/URLs over vague labels like "coding" or "browsing".
6. If event_count is 0 → output exactly: "# {{DATE}} — no capture"
7. If sessions is empty but titles/apps exist → build themes from titles + apps; set capture_note to "events only".
8. Max ~180 words. No preamble outside the template.
9. Prefer capture_hint for Capture note when present.

Output template (markdown):
# {{DATE}}
**Headline:** <≤12 words; concrete; drawn from evidence>
**Active:** <local time span if known, else "partial / unknown">
**Time (approx):** <top 1–3 apps as `Process ~Xm`; skip junk names>
**Themes:**
- <short label from evidence>: <one grounded sentence>
**Artifacts:**
- <url or distinctive title worth keeping, or `(none)`>
**Capture note:** <one of: "session summaries" | "events only" | "thin day" | "noisy/duplicate sessions">
"""

THREADS_PROMPT = """You write a THREADS IN MOTION card from day cards OR raw evidence packs
for dates={{DATES}} (chronological).

A thread = the same project, document, site, person, or topic appearing on
≥2 distinct days — OR a strong single-day arc with a clear lingering artifact
(url, filename, named doc).

Rules:
1. Name each thread using words that appear in the evidence (titles, urls, paths).
2. For each thread list: name · days seen · last day · one factual sentence · optional artifact.
3. No advice, plans, or "next steps."
4. If <2 multi-day threads, say so; put singles under "One-off signals".
5. Ignore duplicate session spam; count calendar days, not row counts.
6. Under ~150 words. Markdown only.
"""

_HEADLINE_RE = re.compile(
    r"^\*\*Headline:\*\*\s*(.+)$", re.IGNORECASE | re.MULTILINE
)


def _cards_dir() -> Path:
    d = get_data_dir() / "insight_cards"
    d.mkdir(parents=True, exist_ok=True)
    return d


def day_card_path(day: str) -> Path:
    return _cards_dir() / f"day_{day}.json"


def threads_card_path(key: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_-]+", "_", key)[:120]
    return _cards_dir() / f"threads_{safe}.json"


def _local_today() -> str:
    return date.today().isoformat()


def _parse_day(day: str) -> date:
    return datetime.strptime(day, "%Y-%m-%d").date()


def list_recent_days(n: int = 14) -> list[str]:
    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(max(1, n))]


def load_day_card(day: str) -> dict[str, Any] | None:
    path = day_card_path(day)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_day_card(card: dict[str, Any]) -> dict[str, Any]:
    day = card["date"]
    path = day_card_path(day)
    path.write_text(json.dumps(card, indent=2, ensure_ascii=False), encoding="utf-8")
    return card


def load_threads_card(key: str) -> dict[str, Any] | None:
    path = threads_card_path(key)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_threads_card(card: dict[str, Any]) -> dict[str, Any]:
    key = card.get("key") or "_".join(card.get("dates") or [])
    card = {**card, "key": key}
    path = threads_card_path(key)
    path.write_text(json.dumps(card, indent=2, ensure_ascii=False), encoding="utf-8")
    return card


def _prompt_pack(pack: dict[str, Any]) -> dict[str, Any]:
    """Strip internal collapse meta before prompting."""
    sessions = []
    for s in pack.get("sessions") or []:
        sessions.append(
            {
                "start_local": s.get("start_local"),
                "end_local": s.get("end_local"),
                "active_task": s.get("active_task"),
                "summary": s.get("summary"),
                "event_count": s.get("event_count"),
            }
        )
    return {
        "event_count": pack.get("event_count", 0),
        "app_time": pack.get("app_time") or [],
        "sessions": sessions,
        "urls": pack.get("urls") or [],
        "title_sample": pack.get("title_sample") or [],
        "capture_hint": pack.get("capture_hint"),
    }


def _extract_message_text(body: dict | None) -> str:
    if not body:
        return ""
    msg = body.get("message") or {}
    content = msg.get("content") or ""
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
            elif isinstance(part, str):
                parts.append(part)
        content = "".join(parts)
    return str(content).strip()


def _headline_from_markdown(md: str) -> str:
    m = _HEADLINE_RE.search(md or "")
    if m:
        return m.group(1).strip()[:120]
    for line in (md or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:120]
    return ""


def generate_day_card(day: str, *, force: bool = False) -> dict[str, Any]:
    """Build evidence → local chat model → persisted day card."""
    day = (_parse_day(day)).isoformat()
    if not force:
        existing = load_day_card(day)
        if existing and existing.get("markdown"):
            return existing

    pack = build_day_evidence_pack(day)
    event_count = int(pack.get("event_count") or 0)
    model = get_chat_model()

    if event_count == 0:
        md = f"# {day} — no capture"
        card = {
            "ok": True,
            "date": day,
            "markdown": md,
            "headline": "no capture",
            "model": model,
            "generated_at": time.time(),
            "event_count": 0,
            "capture_hint": "thin day",
            "collapse": pack.get("collapse"),
        }
        return save_day_card(card)

    prompt = DAY_CARD_PROMPT.replace("{{DATE}}", day)
    user = "EVIDENCE PACK (JSON):\n" + json.dumps(_prompt_pack(pack), indent=2, ensure_ascii=False)
    body = gateway.chat(
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user},
        ],
        model=model,
        think=False,
        options={"temperature": 0.2, "num_predict": 600},
        priority=Priority.FOREGROUND,
        timeout=180,
    )
    md = _extract_message_text(body)
    if not md:
        raise RuntimeError("Day card model returned empty content")

    card = {
        "ok": True,
        "date": day,
        "markdown": md,
        "headline": _headline_from_markdown(md),
        "model": model,
        "generated_at": time.time(),
        "event_count": event_count,
        "capture_hint": pack.get("capture_hint"),
        "collapse": pack.get("collapse"),
    }
    return save_day_card(card)


def get_or_generate_day_card(day: str, *, force: bool = False) -> dict[str, Any]:
    return generate_day_card(day, force=force)


def build_threads_input(dates: list[str]) -> dict[str, Any]:
    """Prefer stored day cards; fall back to compact evidence packs."""
    rows: list[dict[str, Any]] = []
    for day in dates:
        card = load_day_card(day)
        if card and card.get("markdown"):
            rows.append(
                {
                    "date": day,
                    "headline": card.get("headline") or _headline_from_markdown(card["markdown"]),
                    "markdown_excerpt": (card.get("markdown") or "")[:900],
                }
            )
            continue
        pack = build_day_evidence_pack(day)
        themes = [
            {
                "active_task": s.get("active_task"),
                "summary": (s.get("summary") or "")[:200],
            }
            for s in (pack.get("sessions") or [])[:8]
        ]
        rows.append(
            {
                "date": day,
                "event_count": pack.get("event_count"),
                "themes": themes,
                "urls": (pack.get("urls") or [])[:8],
                "title_sample": (pack.get("title_sample") or [])[:10],
                "capture_hint": pack.get("capture_hint"),
            }
        )
    return {"dates": dates, "day_cards_or_evidence": rows}


def generate_threads_card(
    dates: list[str] | None = None,
    *,
    lookback_days: int = 7,
    force: bool = False,
) -> dict[str, Any]:
    if dates:
        days = [(_parse_day(d)).isoformat() for d in dates]
    else:
        # Chronological oldest → newest for the prompt
        days = list(reversed(list_recent_days(lookback_days)))

    key = f"{days[0]}_{days[-1]}" if days else "empty"
    if not force:
        existing = load_threads_card(key)
        if existing and existing.get("markdown"):
            return existing

    # Ensure day cards exist for days with capture (best-effort, skip failures)
    for day in days:
        try:
            pack = build_day_evidence_pack(day)
            if int(pack.get("event_count") or 0) > 0 and not load_day_card(day):
                generate_day_card(day, force=False)
        except Exception as exc:
            print(f"[insight_cards] day pre-gen {day}: {exc}")

    evidence = build_threads_input(days)
    model = get_chat_model()
    prompt = THREADS_PROMPT.replace("{{DATES}}", ", ".join(days))
    user = "EVIDENCE PACK (JSON):\n" + json.dumps(evidence, indent=2, ensure_ascii=False)
    body = gateway.chat(
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user},
        ],
        model=model,
        think=False,
        options={"temperature": 0.2, "num_predict": 500},
        priority=Priority.FOREGROUND,
        timeout=180,
    )
    md = _extract_message_text(body)
    if not md:
        raise RuntimeError("Threads model returned empty content")

    card = {
        "ok": True,
        "key": key,
        "dates": days,
        "markdown": md,
        "model": model,
        "generated_at": time.time(),
        "lookback_days": lookback_days,
    }
    return save_threads_card(card)


def list_insight_home(*, limit: int = 14) -> dict[str, Any]:
    """Days for the home UI: coverage + whether a card is cached."""
    from agent.mcp_query import activity_coverage

    days_out = []
    for day in list_recent_days(limit):
        cov = activity_coverage(day, day, bucket_hours=24.0)
        events = int(cov.get("total_events") or 0)
        card = load_day_card(day)
        days_out.append(
            {
                "date": day,
                "event_count": events,
                "has_card": bool(card and card.get("markdown")),
                "headline": (card or {}).get("headline") or None,
            }
        )
    return {
        "ok": True,
        "today": _local_today(),
        "days": days_out,
    }


def ensure_recent_day_cards(*, lookback: int = 2) -> None:
    """Background: generate missing cards for recent days with events."""
    for day in list_recent_days(lookback):
        if load_day_card(day):
            continue
        try:
            pack = build_day_evidence_pack(day)
            if int(pack.get("event_count") or 0) <= 0:
                continue
            print(f"[insight_cards] generating day card for {day}")
            generate_day_card(day, force=False)
        except Exception as exc:
            print(f"[insight_cards] skipped {day}: {exc}")
