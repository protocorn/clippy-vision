"""
Keyword/regex baseline router.

Implements the labelling policy (docs/router_labelling_policy.md) as deterministic
rules. Zero ML, ~0ms latency. Used to answer: does the trained MiniLM classifier
beat what a page of regexes gets for free?
"""

import re

# ── Time anchors (calendar-resolvable per policy) ────────────
_TIME_ANCHOR_RE = re.compile(
    r"\b("
    r"yesterday|yesteday|yesturday|today|tonight|last night|"
    r"this (morning|mroning|afternoon|evening|week|month|weekend|friday|monday|tuesday|wednesday|thursday|saturday|sunday)|"
    r"last (week|weke|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend)|"
    r"\d+\s*(hour|hr|day|week|month)s?\s*ago|"
    r"(an?\s+(hour|day|week))\s+ago|"
    r"earlier today|just now|"
    r"(on\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"my (monday|tuesday|wednesday|thursday|friday|saturday|sunday|day|week)\b|"
    r"\d{4}-\d{2}-\d{2}|"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}|"
    r"\d{1,2}(st|nd|rd|th)?\s+of\s+(january|february|march|april|may|june|july|august|september|october|november|december|july)|"
    r"over the last \d+|past \d+\s*(hour|day|week)|"
    r"day before yesterday|"
    r"at\s+(around\s+)?\d{1,2}\s*(am|pm|:\d{2})"
    r")\b",
    re.IGNORECASE,
)

# Vague time words that are NOT anchors (policy: topic_search)
_VAGUE_TIME_RE = re.compile(
    r"\b(lately|recently|these days|the other day|before (sleeping|the meeting|lunch)|usually|normally)\b",
    re.IGNORECASE,
)

_AGGREGATION_RE = re.compile(
    r"\b(how many|how mnay|how much|how often|how long|how frequently|total|count|average|"
    r"on average|breakdown|break (that|it) down|summary|stats|statistics|fraction|percent|"
    r"per (day|week|month)|most|least|longest|shortest|more time .* or)\b",
    re.IGNORECASE,
)

_SPECIFIC_RECALL_RE = re.compile(
    r"\b(url|link|coppied|copied|paste|pasted|clipboard|error message|error in|exact|"
    r"command i ran|file was i|filename|article|the title|repo|package|port|password|"
    r"promo code|tracking number|phone number|flight number|address i looked|price of|"
    r"popup|font|docs page|pdf|search(ed)? for|quote i)\b",
    re.IGNORECASE,
)

_MEMORY_RE = re.compile(
    r"\b(what do you know about me|knwo about me|do you remember my|what did i tell you|"
    r"i told you|told you about|have i told you|what did i say|i said my|did i mention|"
    r"my (name|skills|skill|goals|hobbies|job|birthday|allergies|dietary|dog's name)|"
    r"facts you have stored|preferences have i shared|you remember about|"
    r"where do i (work|live)|which city did i say|what team am i|"
    r"languages do i know|recall what i told)\b",
    re.IGNORECASE,
)

# Personal-activity signal: first-person + doing/working verbs
_ACTIVITY_RE = re.compile(
    r"\b(what (did|was|have|am) i|did i (do|get|work|ever)|was i (doing|working|up to|on|reading|researching)|"
    r"i (was|have been) (doing|working|reading|researching)|my (activity|day|screen|apps?)|"
    r"show me (what|my|everything)|walk me through my|catch me up|tell me about my|"
    r"any progress|progress .* (on|made)|how'?s my|am i still working|"
    r"whatever happened with|whats going on with my|i need to (write|fill|log))\b",
    re.IGNORECASE,
)

_FOLLOWUP_RE = re.compile(
    r"^(what about|and (the|last|second)|anything else|tell me more|no,? (not|the|something)|"
    r"check (it|again)|look again|go deeper|that'?s not|you are not understanding|"
    r"what else|before that|break that down|more about (it|that))",
    re.IGNORECASE,
)


def baseline_classify(text: str) -> str:
    """Return primary category using policy priority:
    specific_recall > time_anchored > aggregation > topic_search > memory_query > casual > follow_up_inherit
    (with follow_up detection first since it's structural, not semantic)."""
    # Multi-turn context => classify the last user turn
    if "\nUser:" in text or text.startswith("User:"):
        last = text.split("User:")[-1].strip()
        return (
            "follow_up_inherit"
            if _FOLLOWUP_RE.search(last) or len(last.split()) <= 6
            else baseline_classify(last)
        )

    if _FOLLOWUP_RE.search(text.strip()):
        return "follow_up_inherit"

    has_memory = bool(_MEMORY_RE.search(text))
    has_recall = bool(_SPECIFIC_RECALL_RE.search(text))
    has_time = bool(_TIME_ANCHOR_RE.search(text))
    has_agg = bool(_AGGREGATION_RE.search(text))
    has_activity = bool(_ACTIVITY_RE.search(text))
    has_vague = bool(_VAGUE_TIME_RE.search(text))

    personal = has_activity or has_memory or has_recall or has_vague

    if not personal and not (has_time and has_activity):
        # No personal-data signal at all => casual
        if not has_memory and not has_recall and not has_activity:
            if not has_time or not has_activity:
                return "casual"

    # Priority order from the policy
    if has_recall:
        return "specific_recall"
    if has_time and (has_activity or has_agg):
        return "aggregation" if has_agg else "time_anchored"
    if has_agg and (has_activity or "i " in text.lower() or "my" in text.lower()):
        return "aggregation"
    if has_memory:
        return "memory_query"
    if has_activity or has_vague:
        return "topic_search"
    return "casual"
