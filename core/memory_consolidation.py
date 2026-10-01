"""Turn one-sitting observations into durable claims only when they earn it.

Screen distillation writes candidates. Consolidation, while the machine is
idle, decides whether a candidate repeats a claim, adds a detail, contradicts
one, or is a new pattern seen on an earlier day. A single sitting stays a
candidate. Recall reads claims only.
"""

from __future__ import annotations

import json
import time
import uuid

from core.distil import CLUSTER_THRESHOLD, _cosine_similarity
from core.storage import conn

IDLE_SECONDS = 120


def local_day(stamp: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(float(stamp)))


def _loads_ids(raw) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]


def _union_ids(left: list[str], right: list[str]) -> list[str]:
    merged: list[str] = []
    for item in list(left) + list(right):
        if item and item not in merged:
            merged.append(item)
    return merged


def _loads_embedding(raw) -> list[float]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [float(value) for value in data]


def record_candidate(
    text: str,
    embedding: list,
    *,
    kind: str,
    about_user: bool,
    session_ids: list[str],
    seen_at: float,
    source: str = "screen",
) -> str | None:
    """Store one observation. Same sitting and replays collapse. Not a claim."""
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned or not embedding:
        return None
    if not about_user:
        return None

    kind_name = "observed" if (kind or "").strip().lower() == "observed" else "inferred"
    ids = _union_ids([], session_ids)
    day = local_day(seen_at)
    now = time.time()

    rows = conn.execute(
        """SELECT candidate_id, vector_embedding, session_ids, local_day, status
           FROM memory_candidates
           WHERE status IN ('open', 'flagged', 'promoted', 'absorbed')"""
    ).fetchall()
    for candidate_id, raw_embedding, raw_ids, row_day, status in rows:
        stored = _loads_embedding(raw_embedding)
        if _cosine_similarity(embedding, stored) < CLUSTER_THRESHOLD:
            continue
        existing_ids = _loads_ids(raw_ids)
        # Same observation as a row we already have. Neither case is a new
        # day of evidence, so neither one may insert another candidate.
        # replay: this session was distilled again. Attaching it must not
        # move local_day forward, or a re-summary would look like a later day.
        # same_day: a different session on the same local calendar day. That
        # is still one sitting, even when the summary ids do not overlap.
        replay = bool(ids) and bool(set(ids) & set(existing_ids))
        same_day = status == "open" and row_day == day
        if replay or same_day:
            # An open row can still collect session ids. A promoted, absorbed,
            # or flagged row stays closed. Returning its id is what stops the
            # replay from being stored again as a fresh candidate.
            if status == "open":
                conn.execute(
                    """UPDATE memory_candidates
                       SET session_ids = ?, last_seen = ?
                       WHERE candidate_id = ?""",
                    (json.dumps(_union_ids(existing_ids, ids)), max(float(seen_at), now), candidate_id),
                )
                conn.commit()
            return candidate_id

    candidate_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO memory_candidates
           (candidate_id, text, kind, about_user, session_ids, vector_embedding,
            first_seen, last_seen, local_day, status, source, created_at)
           VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, 'open', ?, ?)""",
        (
            candidate_id,
            cleaned,
            kind_name,
            json.dumps(ids),
            json.dumps(embedding),
            float(seen_at),
            float(seen_at),
            day,
            source,
            now,
        ),
    )
    conn.commit()
    return candidate_id


def _open_candidates() -> list[dict]:
    rows = conn.execute(
        """SELECT candidate_id, text, kind, session_ids, vector_embedding,
                  first_seen, local_day, source
           FROM memory_candidates
           WHERE status = 'open'
           ORDER BY first_seen ASC"""
    ).fetchall()
    return [
        {
            "candidate_id": row[0],
            "text": row[1],
            "kind": row[2],
            "session_ids": _loads_ids(row[3]),
            "embedding": _loads_embedding(row[4]),
            "first_seen": float(row[5]),
            "local_day": row[6],
            "source": row[7],
        }
        for row in rows
    ]


def _active_claims() -> list[dict]:
    rows = conn.execute(
        """SELECT fact_id, cluster_id, text, vector_embedding, support_session_ids,
                  scope, kind, source
           FROM memory_facts
           WHERE valid_to IS NULL"""
    ).fetchall()
    claims = []
    for row in rows:
        embedding = _loads_embedding(row[3])
        if not embedding:
            continue
        claims.append({
            "fact_id": row[0],
            "cluster_id": row[1],
            "text": row[2],
            "embedding": embedding,
            "session_ids": _loads_ids(row[4]),
            "scope": row[5] or "pattern",
            "kind": row[6] or "inferred",
            "source": row[7] or "distiller",
        })
    return claims


def _best_match(embedding: list, rows: list[dict]) -> tuple[dict | None, float]:
    best = None
    best_sim = -1.0
    for row in rows:
        similarity = _cosine_similarity(embedding, row["embedding"])
        if similarity > best_sim:
            best, best_sim = row, similarity
    if best is None or best_sim < CLUSTER_THRESHOLD:
        return None, best_sim if best is not None else 0.0
    return best, best_sim


def _mark(candidate_id: str, status: str) -> None:
    conn.execute(
        "UPDATE memory_candidates SET status = ? WHERE candidate_id = ?",
        (status, candidate_id),
    )
    conn.commit()


def _confirm_claim(claim: dict, session_ids: list[str], now: float) -> None:
    merged = _union_ids(claim["session_ids"], session_ids)
    conn.execute(
        """UPDATE memory_facts
           SET last_confirmed = ?, support_session_ids = ?
           WHERE fact_id = ?""",
        (now, json.dumps(merged), claim["fact_id"]),
    )
    conn.commit()


def _combined_kind(claim_kind: str, candidate_kind: str) -> str:
    if "observed" in (claim_kind, candidate_kind):
        return "observed"
    return "inferred"


def _revise_claim(claim: dict, text: str, embedding: list, session_ids: list[str], kind: str, now: float) -> None:
    from core.distil import _insert_fact, _recompute_centroid

    new_id = str(uuid.uuid4())
    try:
        conn.execute(
            "UPDATE memory_facts SET valid_to = ?, superseded_by = ? WHERE fact_id = ?",
            (now, new_id, claim["fact_id"]),
        )
        _insert_fact(
            claim["cluster_id"],
            text,
            embedding,
            fact_id=new_id,
            source=claim["source"],
            scope=claim["scope"] or "pattern",
            kind=kind,
            session_ids=_union_ids(claim["session_ids"], session_ids),
            last_confirmed=now,
            valid_from=now,
        )
    except Exception:
        conn.rollback()
        raise
    _recompute_centroid(claim["cluster_id"])


def _flag_conflict(claim: dict, candidate: dict, now: float) -> None:
    existing = conn.execute(
        """SELECT 1 FROM memory_conflicts
           WHERE fact_id_a = ? AND candidate_id = ? AND resolved_at IS NULL""",
        (claim["fact_id"], candidate["candidate_id"]),
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO memory_conflicts
               (conflict_id, fact_id_a, fact_id_b, cluster_id, created_at, candidate_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                claim["fact_id"],
                claim["fact_id"],
                claim["cluster_id"],
                now,
                candidate["candidate_id"],
            ),
        )
        conn.commit()
    _mark(candidate["candidate_id"], "flagged")


def _promote(older: dict, newer: dict, now: float) -> None:
    from core.distil import _create_cluster

    sessions = _union_ids(older["session_ids"], newer["session_ids"])
    kind = _combined_kind(older["kind"], newer["kind"])
    _create_cluster(
        newer["text"],
        newer["embedding"],
        source="distiller",
        scope="pattern",
        kind=kind,
        session_ids=sessions,
        last_confirmed=now,
        valid_from=now,
    )
    _mark(older["candidate_id"], "promoted")
    _mark(newer["candidate_id"], "promoted")


def _classify_against_claim(observation: str, claim: str) -> str:
    from core.chat_model import get_chat_model
    from core.llm_gateway import Priority, gateway

    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["duplicate", "compatible", "conflict", "unrelated"]},
        },
        "required": ["action"],
    }
    system = (
        "Compare a NEW observation to ONE existing claim about the user. "
        "duplicate: it repeats the claim and adds nothing. "
        "compatible: it adds a detail that still fits the claim. "
        "conflict: it contradicts the claim. "
        "unrelated: it is a different topic. "
        'Return JSON {"action": "duplicate"|"compatible"|"conflict"|"unrelated"}.'
    )
    body = gateway.chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"observation": observation, "claim": claim})},
        ],
        get_chat_model(),
        format=schema,
        think=False,
        options={"temperature": 0},
        priority=Priority.BACKGROUND,
    )
    content = body["message"]["content"]
    result = json.loads(content) if isinstance(content, str) else content
    action = (result.get("action") or "unrelated").strip().lower()
    if action not in {"duplicate", "compatible", "conflict", "unrelated"}:
        return "unrelated"
    return action


def _compose_compatible(claim: str, observation: str) -> str:
    from core.chat_model import get_chat_model
    from core.llm_gateway import Priority, gateway

    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    system = (
        "Combine an existing claim and one compatible detail into a single sentence. "
        "Use only what is already in those two sentences. "
        'Return JSON {"text": "..."}.'
    )
    body = gateway.chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"claim": claim, "detail": observation})},
        ],
        get_chat_model(),
        format=schema,
        think=False,
        options={"temperature": 0},
        priority=Priority.BACKGROUND,
    )
    content = body["message"]["content"]
    result = json.loads(content) if isinstance(content, str) else content
    text = " ".join((result.get("text") or "").split()).strip()
    return text or claim


def consolidate(*, classify=None, compose=None, now: float | None = None) -> dict:
    """Route open candidates against claims and earlier days."""
    classify_fn = classify or _classify_against_claim
    compose_fn = compose or _compose_compatible
    moment = float(now if now is not None else time.time())
    stats = {"absorbed": 0, "attached": 0, "flagged": 0, "promoted": 0, "left": 0}

    for candidate in _open_candidates():
        claims = _active_claims()
        claim, _similarity = _best_match(candidate["embedding"], claims)
        if claim is not None:
            try:
                action = classify_fn(candidate["text"], claim["text"])
            except Exception as exc:
                print(f"  [MEMORY] route skipped: {exc}")
                stats["left"] += 1
                continue
            if action == "duplicate":
                _confirm_claim(claim, candidate["session_ids"], moment)
                _mark(candidate["candidate_id"], "absorbed")
                stats["absorbed"] += 1
                continue
            if action == "compatible":
                try:
                    revised = compose_fn(claim["text"], candidate["text"])
                except Exception as exc:
                    print(f"  [MEMORY] attach skipped: {exc}")
                    stats["left"] += 1
                    continue
                revised = " ".join((revised or "").split()).strip() or claim["text"]
                if revised == claim["text"]:
                    _confirm_claim(claim, candidate["session_ids"], moment)
                    _mark(candidate["candidate_id"], "absorbed")
                    stats["absorbed"] += 1
                    continue
                if revised == candidate["text"]:
                    embedding = candidate["embedding"]
                else:
                    from core.local_embeddings import embed_text
                    embedding = embed_text(revised)
                _revise_claim(
                    claim,
                    revised,
                    embedding,
                    candidate["session_ids"],
                    _combined_kind(claim["kind"], candidate["kind"]),
                    moment,
                )
                _mark(candidate["candidate_id"], "absorbed")
                stats["attached"] += 1
                continue
            if action == "conflict":
                _flag_conflict(claim, candidate, moment)
                stats["flagged"] += 1
                continue

        earlier = [
            other for other in _open_candidates()
            if other["candidate_id"] != candidate["candidate_id"]
            and other["local_day"] < candidate["local_day"]
        ]
        match, _similarity = _best_match(candidate["embedding"], earlier)
        if match is None:
            stats["left"] += 1
            continue
        try:
            _promote(match, candidate, moment)
        except Exception as exc:
            print(f"  [MEMORY] promote skipped: {exc}")
            stats["left"] += 1
            continue
        stats["promoted"] += 1

    if any(stats.values()):
        print(
            "  [MEMORY] consolidate "
            + " ".join(f"{name}={count}" for name, count in stats.items())
        )
    return stats


def maybe_consolidate() -> dict | None:
    """Run consolidation only while the person is away from the keyboard."""
    pending = conn.execute(
        "SELECT COUNT(*) FROM memory_candidates WHERE status = 'open'"
    ).fetchone()
    if not pending or int(pending[0] or 0) == 0:
        return None
    try:
        from core.platform_support import get_idle_seconds
        idle = get_idle_seconds()
    except Exception:
        return None
    if idle is None or idle < IDLE_SECONDS:
        return None
    return consolidate()
