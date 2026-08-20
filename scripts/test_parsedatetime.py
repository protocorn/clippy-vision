from datetime import datetime

import parsedatetime

_cal = parsedatetime.Calendar()


def _extract_temporal_hint(query: str) -> dict | None:
    """Deterministically resolve a time expression in the query, anchored to now.
    Only called when the classifier already signals time_anchored — see gating below."""
    matches = _cal.nlp(query, sourceTime=datetime.now())
    if not matches:
        return None
    dt, flags, start_idx, end_idx, matched_text = matches[0]  # first/strongest match
    return {
        "phrase": matched_text.strip(),
        "resolved": dt.isoformat(),
    }


if __name__ == "__main__":
    while True:
        query = input("Enter a query: ")
        hint = _extract_temporal_hint(query)
        print(hint)
