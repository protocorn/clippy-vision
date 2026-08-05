"""Deterministic time-anchor resolution for router pre-fetch.

Given a query already classified as time_anchored (agent/router.py), resolve
the time expression it contains into an absolute [start, end) epoch range
anchored to "now". Returns None when nothing can be confidently resolved —
callers fall back to the existing LLM SQL path. This module only adds a fast
path; it never replaces the fallback.

parsedatetime is unmaintained (last release May 2020), so its known gaps are
patched here permanently:
  - No "weekend" vocabulary at all  -> _WEEKEND_RE handles it before
    parsedatetime is ever called.
  - "last/past N days|weeks|..." resolves to a single FUTURE day
    -> _ROLLING_WINDOW_RE handles rolling lookback windows.
  - Bare weekday mentions ("on Monday") resolve to the NEXT occurrence
    instead of the most recent past one -> snapped back a week.

Typo tolerance uses Damerau-Levenshtein distance against a small temporal
vocabulary only (never general prose). Thresholds are deliberately strict:
1 edit for most words, 2 only for long words — permissive thresholds corrupt
real words ("money" is 2 edits from "monday", "least" is 1 from "last").

The SAME bare time word can mean the future or the past depending only on verb
tense ("what should I do this weekend?" vs "this weekend was so boring") — no
amount of date-phrase parsing can resolve that, because the ambiguity isn't in
the phrase, it's in the sentence's grammar. _detect_intent_tense() scans the
whole query for modal/tense cues, with polite-request guards so "can you tell
me what I did yesterday?" is not mistaken for future intent. Typo correction
covers the cue words too (capped at 1 edit — see _INTENT_VOCAB). "This
weekend" asked on a weekday with no past-tense evidence means the UPCOMING
weekend and is rejected; asked during the weekend it means the current one.
This is a second, independent layer on top of the router classifier's own
training on this same distinction (see docs/router_labelling_policy.md).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import re

import parsedatetime
from rapidfuzz.distance import DamerauLevenshtein

_cal = parsedatetime.Calendar()

_TEMPORAL_VOCAB = [
    "today", "yesterday", "tomorrow", "next", "last", "year", "years",
    "months", "weeks", "days", "hours", "minutes", "seconds",
    "morning", "afternoon", "evening", "night", "midnight", "noon", "breakfast", "lunch", "dinner", "supper",
    "dawn", "dusk", "previous", "prior", "earlier", "before", "after", "past", "weekday", "weekend",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "ago", "coming", "upcoming", "following", "early", "late", "tonight",
]
_TEMPORAL_VOCAB_SET = set(_TEMPORAL_VOCAB)

_MIN_FUZZY_LENGTH = 4
_MIN_CANDIDATE_LENGTH = 4



_PROTECTED_WORDS = {

    "date", "dates", "least", "yeah", "hear", "near", "wear", "pass",
    "mouth", "south", "worth", "cast", "list", "lost", "must", "doing",
    "money", "monkey", "yours", "ours", "error", "remember", "remembered",


    "sent", "spend", "booked", "plane", "plain", "plant", "plants",
    "build", "write", "complete", "manage", "manager", "hopping",
    "matched", "washed", "patched", "batched", "exited", "cold",
    "world", "wound", "wandering", "tasked", "locked", "worker",
    "forked", "browse", "browser", "searches", "cleaner",
    "cast", "vast", "fast", "winner", "winners", "miner", "coping", "nest",
    "text", "sext", "neft", "jute", "pearl", "nearly", "roaming"
}

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]

_MODIFIER_WORDS = ["last", "next", "previous", "prior", "earlier", "before", "past", "coming", "upcoming", "following"]
_FUTURE_MODIFIERS = ("next", "coming", "upcoming", "following")

_WEEKEND_RE = re.compile(
    r'\b(?:(?P<modifier>last|past|previous|prior|next|coming|following|this)\s+)?week[- ]?end\b',
    re.IGNORECASE,
)



_ROLLING_WINDOW_RE = re.compile(
    r'\b(?:(?:over|in|from)\s+(?:the\s+)?)?'
    r'(?:last|past|previous|prior)\s+'
    r'(?P<n>\d+)\s+(?P<unit>days?|weeks?|months?|hours?)\b',
    re.IGNORECASE,
)




_HOURS_AGO_RE = re.compile(
    r'\b(?P<n>\d+)\s+(?P<unit>hours?|minutes?|mins?)\s+(ago|before)\b',
    re.IGNORECASE,
)

_EXACT_NUMERIC_DATETIME_RE = re.compile(
    r"\b(?P<month>\d{1,2})[/-](?P<day>\d{1,2})[/-](?P<year>\d{4})"
    r"(?:\s*,?\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)?)\b",
    re.IGNORECASE,
)















_INTENT_VOCAB = [

    "should", "would", "could", "might", "going", "gonna", "wanna", "gotta",
    "hoping", "planning", "thinking", "wondering", "plans", "wants", "excited",

    "happened", "spent", "worked", "watched", "walked", "talked", "looked",
    "checked", "called", "cooked", "cleaned", "visited", "finished",
    "completed", "enjoyed", "managed", "built", "wrote", "browsed", "searched",
]
_INTENT_VOCAB_SET = set(_INTENT_VOCAB)





_INTENT_PASSTHROUGH = {
    "want", "plan", "hope", "wish", "went", "felt", "will", "shall",
    "did", "was", "were", "had", "met", "saw",
}

_CORRECTION_TARGETS = _TEMPORAL_VOCAB + _INTENT_VOCAB
_PASSTHROUGH_SET = set(_CORRECTION_TARGETS) | _INTENT_PASSTHROUGH




_REQUEST_VERBS = r"(?:see|know|check|review|look|get|have|find|view|recall|remember|tell|show|give|summarize|summarise|pull|fetch|list)"




_PAST_REGRET_RE = re.compile(
    r"\b(?:should|could|would|might|must)(?:\s+(?:i|we|you|he|she|they))?(?:'ve|\s+have|\s+of)\b"
)





_HEDGE_RE = re.compile(r"\b(?:was|were)\s+(?:wondering|thinking|hoping|planning|going|gonna|about)\b")

_PAST_CUES_RE = re.compile(
    r"\b(?:was|were|did|had|went|felt|met|saw|spent|worked|watched|walked|talked"
    r"|looked|checked|called|cooked|cleaned|visited|finished|completed|enjoyed"
    r"|managed|built|wrote|browsed|searched|happened)\b"
    r"|\b(?:have|has)\s+(?:\w+\s+)?been\b"
)

_FUTURE_CUES_RE = re.compile(


    r"\b(?:should|shall|will|might)\s+(?:i|we)\b(?!\s+" + _REQUEST_VERBS + r"\b)"
    r"|\b(?:i|we)\s+(?:should|shall|will|might)\b"
    r"|\bi'll\b|\bwe'll\b"




    r"|\b(?:can|could|would)\s+(?:i|we)\s+(?:like\s+to\s+|love\s+to\s+)?(?!(?:" + _REQUEST_VERBS + r"|not)\b)\w+"
    r"|\b(?:i|we)\s+(?:can|could|would)\s+(?:like\s+to\s+|love\s+to\s+)?(?!(?:" + _REQUEST_VERBS + r"|not)\b)\w+"
    r"|\b(?:gonna|wanna|gotta)\b"
    r"|\b(?:going|want|wants|plan|hope|hoping|planning|intend|wish)\s+to\s+(?!" + _REQUEST_VERBS + r"\b)"
    r"|\bthinking\s+(?:of|about)\s+\w+ing\b"
    r"|\bplans?\s+for\b|\bany\s+plans\b|\babout\s+to\b"
    r"|\blooking\s+forward\b|\bcan'?t\s+wait\b|\bexcited\s+(?:for|about|to)\b"
    r"|\bhave\s+a\s+(?:good|great|nice|fun|wonderful)\b|\bhope\s+you\b|\benjoy\s+your\b|\bgood\s+luck\b"
)


def _detect_intent_tense(query_lower: str) -> str | None:
    """Returns "past", "future", or None (no cue found either way)."""
    if _PAST_REGRET_RE.search(query_lower):
        return "past"
    if _PAST_CUES_RE.search(_HEDGE_RE.sub("", query_lower)):
        return "past"
    if _FUTURE_CUES_RE.search(query_lower):
        return "future"
    return None


@dataclass
class TemporalRange:
    phrase: str
    start_ts: float
    end_ts: float
    granularity: str






def _max_allowed_edits(word_len: int) -> int:
    if word_len <= _MIN_FUZZY_LENGTH:
        return 1
    elif word_len <= 8:
        return 2
    else:
        return 3

def normalize_temporal_words(query: str) -> str:
    """Fuzzy-correct only words that closely and UNAMBIGUOUSLY resemble a
    temporal keyword or a tense/intent cue word. Everything else passes
    through untouched."""
    corrected = []

    for word in query.split():
        clean = word.strip(".,?!:;").lower()

        if (
            len(clean) < _MIN_FUZZY_LENGTH
            or clean in _PASSTHROUGH_SET
            or clean in _PROTECTED_WORDS
        ):
            corrected.append(word)
            continue

        max_edits = _max_allowed_edits(len(clean))
        best_word, best_dist, tied = None, None, False

        for candidate in _CORRECTION_TARGETS:
            if len(candidate) < _MIN_CANDIDATE_LENGTH:
                continue



            cutoff = 1 if candidate in _INTENT_VOCAB_SET else max_edits
            dist = DamerauLevenshtein.distance(clean, candidate, score_cutoff=cutoff)
            if dist > cutoff:
                continue
            if best_dist is None or dist < best_dist:
                best_word, best_dist, tied = candidate, dist, False
            elif dist == best_dist:
                tied = True

        if best_word is not None and not tied:
            corrected.append(best_word)
        else:
            corrected.append(word)

    return " ".join(corrected)







def day_bounds(dt: datetime) -> tuple[datetime, datetime]:
    start = datetime(dt.year, dt.month, dt.day)
    return start, start + timedelta(days=1)


def week_bounds(dt: datetime, offset_weeks: int = 0) -> tuple[datetime, datetime]:
    monday = datetime(dt.year, dt.month, dt.day) - timedelta(days=dt.weekday())
    monday += timedelta(weeks=offset_weeks)
    return monday, monday + timedelta(days=7)


def month_bounds(dt: datetime, offset_months: int = 0) -> tuple[datetime, datetime]:
    year, month = dt.year, dt.month + offset_months
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start, end


def _rolling_window_delta(n: int, unit: str) -> tuple[timedelta, str]:
    """Return (lookback delta, granularity) for 'last N units' phrases."""
    u = unit.lower().rstrip("s")
    if u == "hour":
        return timedelta(hours=n), "hour"
    if u == "day":
        return timedelta(days=n), "day"
    if u == "week":
        return timedelta(weeks=n), "week"


    return timedelta(days=30 * n), "month"


def weekend_bounds(now: datetime, modifier: str | None, intent: str | None) -> tuple[datetime, datetime] | None:
    """Anchor on the CURRENT calendar week's Saturday, not the most recent
    Saturday — asked on a Thursday, "last weekend" means the weekend that just
    passed (5 days ago), not two weekends back.

    Only past/bare modifiers ever reach this function — "next"/"coming"/
    "following" (and any future-intent sentence) are rejected in
    resolve_temporal_range() before this is called.

    Bare "weekend" / "this weekend" is resolved by WHEN the question is asked:
      - during the weekend (Sat/Sun): the current, in-progress weekend;
      - on a weekday WITH past-tense evidence ("this weekend was so boring"
        asked on a Monday): the weekend that just passed;
      - on a weekday with NO past-tense evidence: "this weekend" means the
        UPCOMING weekend — future, no data can exist -> None."""
    today = datetime(now.year, now.month, now.day)
    monday_this_week = today - timedelta(days=today.weekday())
    saturday_this_week = monday_this_week + timedelta(days=5)

    if modifier in ("last", "past", "previous", "prior"):
        start = saturday_this_week - timedelta(weeks=1)
    elif now >= saturday_this_week:
        start = saturday_this_week
    elif intent == "past":
        start = saturday_this_week - timedelta(weeks=1)
    else:
        return None
    return start, start + timedelta(days=2)






def _finalize(phrase: str, start: datetime, end: datetime, granularity: str, now: datetime) -> "TemporalRange | None":
    """Single choke point every branch routes through. This is a past/present
    activity assistant — it can never have data about the future, so a range
    that starts after "now" isn't just unlikely to match anything, it's
    categorically unanswerable. Reject it (return None) rather than silently
    running a query that can only ever come back empty, so callers know to
    treat it as unresolved rather than "no activity found"."""
    if start > now:
        return None
    end = min(end, now)
    return TemporalRange(
        phrase=phrase,
        start_ts=start.timestamp(),
        end_ts=end.timestamp(),
        granularity=granularity,
    )


def resolve_temporal_range(query: str, now: datetime | None = None) -> TemporalRange | None:
    """Resolve the first time expression in `query` into an absolute
    [start, end) epoch range anchored to `now`. Returns None if nothing is
    found, OR if the expression refers to the future — caller falls back to
    the LLM SQL path in the first case, and should tell the user activity
    data doesn't exist yet in the second (both look the same from here by
    design: this module never fabricates a range it can't stand behind)."""
    now = now or datetime.now()
    normalized = normalize_temporal_words(query)







    intent = _detect_intent_tense(normalized.lower())
    if intent == "future":
        return None

    exact_match = _EXACT_NUMERIC_DATETIME_RE.search(normalized)
    if exact_match and exact_match.group("hour") is not None:
        try:
            hour = int(exact_match.group("hour"))
            ampm = (exact_match.group("ampm") or "").lower().replace(".", "")
            if ampm:
                if not 1 <= hour <= 12:
                    return None
                hour = (hour % 12) + (12 if ampm == "pm" else 0)
            elif not 0 <= hour <= 23:
                return None
            target = datetime(
                int(exact_match.group("year")),
                int(exact_match.group("month")),
                int(exact_match.group("day")),
                hour,
                int(exact_match.group("minute")),
                int(exact_match.group("second") or 0),
            )
        except ValueError:
            return None
        tolerance = timedelta(seconds=2 if exact_match.group("second") is not None else 60)
        return _finalize(
            exact_match.group(0).strip(),
            target - tolerance,
            target + tolerance,
            "instant",
            now,
        )


    weekend_match = _WEEKEND_RE.search(normalized)
    if weekend_match:
        modifier = (weekend_match.group("modifier") or "").lower() or None
        if modifier in _FUTURE_MODIFIERS:
            return None
        bounds = weekend_bounds(now, modifier, intent)
        if bounds is None:
            return None
        start, end = bounds
        return _finalize(weekend_match.group(0).strip(), start, end, "weekend", now)

    rolling_match = _ROLLING_WINDOW_RE.search(normalized)
    if rolling_match:
        n = int(rolling_match.group("n"))
        delta, granularity = _rolling_window_delta(n, rolling_match.group("unit"))
        start = now - delta
        return _finalize(rolling_match.group(0).strip(), start, now, granularity, now)

    hours_ago_match = _HOURS_AGO_RE.search(normalized)
    if hours_ago_match:
        n = int(hours_ago_match.group("n"))
        u = hours_ago_match.group("unit").lower().rstrip("s").replace("min", "minute")
        delta = timedelta(hours=n) if u == "hour" else timedelta(minutes=n)
        return _finalize(hours_ago_match.group(0).strip(), now - delta, now, "hour", now)

    try:
        matches = _cal.nlp(normalized, sourceTime=now)
    except Exception:
        matches = None

    if not matches:
        return None

    anchor_dt, _flags, start_idx, _end_idx, matched_text = matches[0]
    phrase = matched_text.strip().lower()




    prefix = normalized[:start_idx].strip().lower()





    if any(mod in phrase for mod in _FUTURE_MODIFIERS) or any(prefix.endswith(mod) for mod in _FUTURE_MODIFIERS):
        return None

    if "yesterday" in phrase:
        start, end = day_bounds(now - timedelta(days=1))
        granularity = "day"
    elif any(k in phrase for k in ("today", "this morning", "this afternoon", "this evening", "tonight")):
        start, end = day_bounds(now)
        granularity = "day"
    elif "last week" in phrase or "previous week" in phrase:
        start, end = week_bounds(now, offset_weeks=-1)
        granularity = "week"
    elif "this week" in phrase:
        start, end = week_bounds(now, offset_weeks=0)
        granularity = "week"
    elif "last month" in phrase or "previous month" in phrase:
        start, end = month_bounds(now, offset_months=-1)
        granularity = "month"
    elif "this month" in phrase:
        start, end = month_bounds(now, offset_months=0)
        granularity = "month"
    elif any(m in phrase for m in _MONTHS):



        start, end = month_bounds(anchor_dt)
        if start > now:
            start, end = month_bounds(anchor_dt, offset_months=-12)
        granularity = "month"
    else:












        has_weekday = any(d in phrase for d in _WEEKDAYS)
        has_modifier = (
            any(mod in phrase for mod in _MODIFIER_WORDS)
            or "this" in phrase
            or any(prefix.endswith(mod) for mod in _MODIFIER_WORDS)
            or prefix.endswith("this")
        )
        if has_weekday and (not has_modifier or intent == "past") and anchor_dt.date() > now.date():
            anchor_dt = anchor_dt - timedelta(weeks=1)
        start, end = day_bounds(anchor_dt)
        granularity = "day"

    return _finalize(matched_text.strip(), start, end, granularity, now)


if __name__ == "__main__":
    while True:
        q = input("Enter a query: ")
        if not q.strip():
            continue
        normalized = normalize_temporal_words(q)
        if normalized != q:
            print(f"  normalized: {normalized}")
        intent = _detect_intent_tense(normalized.lower())
        print(f"  intent: {intent}")
        rng = resolve_temporal_range(q)
        if rng is None:
            print("  -> None")
        else:
            print(
                f"  -> phrase={rng.phrase!r}  granularity={rng.granularity}\n"
                f"     start={datetime.fromtimestamp(rng.start_ts)}  end={datetime.fromtimestamp(rng.end_ts)}"
            )
