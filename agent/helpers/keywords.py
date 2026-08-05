import re





STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "need",
    "dare", "ought", "used", "it", "its", "this", "that", "these", "those",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "his", "her",
    "they", "their", "them", "what", "which", "who", "whom", "how", "when",
    "where", "why", "all", "any", "both", "each", "few", "more", "most",
    "other", "some", "such", "no", "not", "only", "own", "same", "so",
    "than", "too", "very", "just", "also", "back", "there", "then",
    "get", "got", "go", "see", "saw", "say", "said", "make", "made",
    "know", "think", "want", "use", "look", "come", "take", "give",
    "now", "here", "up", "out", "about", "over", "after", "before",
    "into", "through", "during", "until", "while", "since", "between",

    "related", "show", "tell", "find", "search", "give", "describe",
    "exactly", "specific", "recent", "days", "last", "first", "ago",
    "screen", "seeing", "looking", "viewed", "opened",
    "working", "doing",
})


def content_keywords(tokens: list[str]) -> list[str]:
    """Return only content-bearing tokens, dropping stopwords and single chars.

    Args:
        tokens: Raw tokens from a query (already lowercased).

    Returns:
        Filtered list containing only meaningful search terms.
    """
    return [t for t in tokens if t.lower() not in STOPWORDS and len(t) > 1]


def keywords_from_query(query: str) -> list[str]:
    """Tokenise a query and return content keywords (stopwords removed).

    Quoted phrases are preserved as single tokens.
    """
    tokens = re.findall(r'"[^"]+"|\b\w+\b', query.lower())
    raw = [t.strip('"') for t in tokens if len(t.strip('"')) > 2]
    return content_keywords(raw)
