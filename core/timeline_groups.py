"""Mark nearby timeline cards that describe the same stretch of work.

Each summary stays its own session. A card joins the previous one only when
it starts within 15 minutes and the two summary embeddings are very close.
The chain is one hop at a time, so an unrelated card in the middle cannot
pull two similar sessions into the same group.
"""

import math

# Same-task retellings from a real evening sat around 0.80–0.95.
# Unrelated neighbors were usually below 0.60.
GROUP_SIMILARITY = 0.80
GROUP_GAP_SECONDS = 15 * 60


def _cosine(left: list, right: list) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


def assign_similar_session_groups(sessions: list[dict], embeddings: list) -> None:
    """Set group_id on runs of similar neighbors. A lone session gets None."""
    for session in sessions:
        session["group_id"] = None
    if len(sessions) < 2:
        return

    order = sorted(
        range(len(sessions)),
        key=lambda index: (
            float(sessions[index].get("window_start") or 0),
            str(sessions[index].get("summary_id") or ""),
        ),
    )
    run: list[int] = []
    previous = None
    for index in order:
        joined = False
        if previous is not None:
            gap = float(sessions[index].get("window_start") or 0) - float(
                sessions[previous].get("window_end") or 0
            )
            left = embeddings[previous] if previous < len(embeddings) else None
            right = embeddings[index] if index < len(embeddings) else None
            if (
                gap <= GROUP_GAP_SECONDS
                and left
                and right
                and _cosine(left, right) >= GROUP_SIMILARITY
            ):
                joined = True
        if joined:
            run.append(index)
        else:
            _close_run(sessions, run)
            run = [index]
        previous = index
    _close_run(sessions, run)


def _close_run(sessions: list[dict], run: list[int]) -> None:
    if len(run) < 2:
        return
    group_id = str(sessions[run[0]].get("summary_id") or run[0])
    for index in run:
        sessions[index]["group_id"] = group_id
