"""Router-driven prefetch, shared by the chat agent and the MCP server.

`run_prefetch` is the retrieval half of a chat turn; `routed_context` wraps it with
classification so a caller holding nothing but a question gets the same routing the
in-app agent applies.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from agent.helpers.time_resolver import resolve_temporal_range
from agent.prefetch.memory_query import memory_query
from agent.prefetch.specific_recall import specific_recall
from agent.prefetch.time_anchor import time_anchor_fetch
from agent.prefetch.topic_search import topic_search
from agent.router import classify_query, should_prefetch
from core.llm_gateway import gateway, Priority

EMBED_MODEL = "nomic-embed-text"

# Routes that have real prefetch implementations
PREFETCHABLE = {"time_anchored", "topic_search", "specific_recall", "memory_query"}
MAX_PREFETCH_CONTEXT_CHARS = 8000


def _fetch_single_route(
    route: str,
    temporal_range,          # pre-resolved, may be None
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


def run_prefetch(decision, query: str, combined: str, q_vec: list) -> str:
    """Fire prefetch for primary + all secondary routes in parallel.

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
    primary_routes = [decision.primary] if decision.primary in PREFETCHABLE else []
    secondary_routes = [
        s for s in decision.secondary
        if s in PREFETCHABLE and s != decision.primary and s != "time_anchored"
    ]
    routes_to_run = primary_routes + secondary_routes
    print(f"[prefetch] routes: {routes_to_run}")

    if not routes_to_run:
        return ""

    # ── Single route — no thread overhead ────────────────────────────────────
    if len(routes_to_run) == 1:
        return _limit_prefetch_context(
            _fetch_single_route(routes_to_run[0], temporal_range, query, combined, q_vec)
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


def build_combined_query(question: str, prior_turns: Iterable[str] | None = None) -> str:
    """Pair a question with its recent turns so "that", "it", or "the day before"
    still resolve during time parsing, topic extraction, and embedding."""
    prior = " | ".join(turn for turn in (prior_turns or ()) if turn)
    return f"User: {question} | Prior turns: {prior}" if prior else question


_USER_TURN_PREFIX = re.compile(r"^\s*user\s*:\s*", re.IGNORECASE)
# Acknowledgements ("thanks", "ok cool") end a topic; they must not re-trigger
# retrieval just because the previous turn was about activity.
_FOLLOW_UP_CUE = re.compile(
    r"\?|\b(what|which|when|where|who|why|how|that|those|these|it|them|other|same|more|again|check|instead)\b",
    re.IGNORECASE,
)
_ANCHOR_MAX_CHARS = 200


def _context_anchor(prior_turns: list[str]) -> str:
    """What a vague follow-up refers back to. The last user turn carries the topic;
    assistant text is longer and noisier, so it is only a fallback."""
    for turn in reversed(prior_turns):
        if _USER_TURN_PREFIX.match(turn):
            return _USER_TURN_PREFIX.sub("", turn).strip()[:_ANCHOR_MAX_CHARS]
    return prior_turns[-1].strip()[:_ANCHOR_MAX_CHARS] if prior_turns else ""


def _reroute_with_context(question: str, prior_turns: list[str]):
    """Second routing attempt for follow-ups that mean nothing on their own.

    The classifier was trained on single turns, so a labelled transcript routes
    worse than plain text; prepending the previous question instead reads as one
    self-contained query. Returns None unless the retry clears the gate by itself,
    which keeps this strictly additive to the first decision.
    """
    if not prior_turns or not _FOLLOW_UP_CUE.search(question):
        return None

    anchor = _context_anchor(prior_turns)
    if not anchor:
        return None

    decision, confidence = classify_query(f"{anchor} {question}")
    confidence = confidence or 0.0
    if decision is None or not should_prefetch(decision, confidence):
        return None

    print(f"[router] follow-up re-routed with context → {decision.primary} ({confidence:.2f})")
    return decision, confidence


@dataclass(slots=True)
class RoutedContext:
    """Outcome of classify → gate → prefetch for a single question."""
    route: str
    confidence: float
    context: str = ""
    secondary: list[str] = field(default_factory=list)
    routed: bool = True

    @property
    def prefetched(self) -> bool:
        return bool(self.context)


def routed_context(question: str, prior_turns: Iterable[str] | None = None) -> RoutedContext:
    """Classify `question`, then prefetch only if the router clears its threshold.

    Classification sees the bare question first, because the router was trained on
    single turns. Only if that fails the prefetch gate do `prior_turns` get a say in
    routing, and they always enrich the text used for embedding and retrieval — the
    same way the chat agent uses its own history. The embedding is computed lazily
    so questions that never reach prefetch cost nothing.
    """
    turns = [turn.strip() for turn in (prior_turns or ()) if turn and turn.strip()]

    decision, confidence = classify_query(question)
    if decision is None:
        return RoutedContext("unavailable", 0.0, routed=False)

    confidence = confidence or 0.0
    if not should_prefetch(decision, confidence):
        rerouted = _reroute_with_context(question, turns)
        if rerouted is None:
            return RoutedContext(decision.primary, confidence, secondary=decision.secondary)
        decision, confidence = rerouted

    combined = build_combined_query(question, turns)
    try:
        q_vec = gateway.embed(combined, embed_model=EMBED_MODEL, priority=Priority.INTERACTIVE)
    except Exception as exc:
        print(f"[prefetch] embed failed — {exc}")
        q_vec = None

    context = run_prefetch(decision, question, combined, q_vec) if q_vec else ""
    return RoutedContext(decision.primary, confidence, context, decision.secondary)
