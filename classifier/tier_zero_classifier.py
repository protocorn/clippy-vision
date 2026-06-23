import json
from typing import Optional, TypedDict
import math

UNINTERESTING_PROCESSES = {"msiexec.exe", "SearchHost.exe", "unknown"}

MEANINGFUL_RATIO_THRESHOLD = 0.30
MIN_WORDS = 2

class TierZeroClassification(TypedDict):
    verdict: str
    score: int
    reason: str

def tier_zero_classifier(event: dict) -> Optional[TierZeroClassification]:
    event_type       = event["event_type"]
    payload          = json.loads(event["payload"]) if event["payload"] else {}
    window_context   = event["window_context"]
    prev_context     = event["previous_window_context"]

    # ------------------------------------------------------------------ #
    # Obvious NOT INTERESTING ------------------------------------------ #
    # ------------------------------------------------------------------ #

    if event_type == "typing_burst":
        word_count  = payload.get("word_count", 0)
        char_count  = payload.get("character_count", 0)
        key_count   = payload.get("key_down_count", 1)
        ratio       = char_count / key_count if key_count > 0 else 0.0

        # No real words typed — volume keys, arrows, Ctrl+C etc.
        if word_count < MIN_WORDS or ratio < MEANINGFUL_RATIO_THRESHOLD:
            return TierZeroClassification(
                verdict="not_interesting",
                score=0,
                reason=f"Non-typing burst (words={word_count}, char_ratio={ratio:.2f})"
            )

    # Context change to a background system process
    if event_type == "context_change" and window_context["process_name"] in UNINTERESTING_PROCESSES:
        return TierZeroClassification(
            verdict="not_interesting",
            score=0,
            reason="System process context change"
        )

    # Duplicate context change — same process and same title (e.g. Chrome tab reload)
    if (event_type == "context_change"
            and prev_context is not None
            and window_context["process_name"] == prev_context["process_name"]
            and window_context["current_window_title"] == prev_context["current_window_title"]):
        return TierZeroClassification(
            verdict="not_interesting",
            score=0,
            reason="Context change to same window"
        )
    
    if event_type in ("paste", "clipboard_change"):
        content = payload.get("content") or payload.get("pasted_content") or ""
        if len(content.split()) < 3:
            return TierZeroClassification(
                verdict="not_interesting",
                score=1,
                reason="Trivial clipboard content"
            )

    # ------------------------------------------------------------------ #
    # Obvious INTERESTING ---------------------------------------------- #
    # ------------------------------------------------------------------ #

    # Anomalous deviation (baseline already computed the σ)
    if event_type == "deviation" and payload.get("anomaly") is True:
        return TierZeroClassification(
            verdict="interesting",
            score=9,
            reason=f"Anomalous deviation {payload.get('overall_deviation')}σ"
        )

    # Ambiguous — pass to Tier 1
    return None
