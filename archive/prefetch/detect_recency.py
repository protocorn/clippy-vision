import re

RECENCY_WINDOW_HINT = (
    r'\b('
    r'few days? ago|past few days?|just recently|very recently'
    r'|not too long ago|short(ly)? (ago|before)'
    r'|earlier today|just now|moments? ago|minutes? ago|hours? ago'
    r'|earlier this (morning|afternoon|evening|week)'
    r')\b'
)

RECENCY_SOFT_HINT = (
    r'\b('

    # classic recency adverbs
    r'lately|recently|recent|these days|nowadays|of late'
    r'|not long ago|a while ago|some time ago'


    # "last" as recency signal — "last chatbot", "last link", "last thing", "last time"
    # Exclude calendar "last week/month/year" which time_resolver handles
    r'|last\s+(?!week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)'

    # "earlier" without a specific time reference
    r'|earlier(?!\s+(today|this\s+(morning|afternoon|evening|week)))'

    # "previous", "prior", "before" as loose recency
    r'|previous(?!\s+(week|month|year))'
    r'|the\s+last\s+(?:one|time|thing|link|url|article|page|site|message|email|file|image)'

    # "just" as recency — "what did I just copy", "link I just visited"
    r'|just\s+(?:now|visited|opened|copied|pasted|read|used|saw|looked)'
    r'|i\s+just\b'

    # "before" in temporal context
    r'|(?:right\s+)?before\s+(?:this|now|today)'

    # "haven't seen it in a while" style
    r'|a\s+while\s+back|some\s+time\s+back'
    r')\b'
)


def detect_recency_hint(query: str) -> str | None:
    """Returns 'soft' for vague recency, 'window' for semi-specific, None otherwise."""
    q = query.lower()


    # Semi-specific: implies ~7 day window
    if re.search(RECENCY_WINDOW_HINT, q):
        return "window"


    # Vague: boost recency score but no hard filter
    if re.search(RECENCY_SOFT_HINT, q):
        return "soft"

    return None