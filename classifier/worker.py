import hashlib
import json
import threading
import time

from core.backlog import (
    catch_up_allowed,
    deferred_event_skipped,
    note_catch_up_failure,
    note_deferred_event_skip,
    set_catch_up_running,
)
from core.capture_state import get_capture_status
from core.model_residency import can_load_text, can_run_ocr, ensure_text_model
from core.screenshot_enrichment import enrich_screenshot
from core.storage import conn
from core.vision import get_screenshots_near

from .tier_one_classifier import tier1_score
from .tier_two_classifier import classify_with_llm
from .tier_zero_classifier import tier_zero_classifier

POLL_SECS = 2
CATCH_UP_POLL_SECS = 5
CATCH_UP_BATCH = 5
DUPLICATE_LOOKBACK_SECS = 30 * 60
MAX_VISION_WAIT_SECS = 300  # skip vision enrichment if event has been waiting longer than this

DEFAULT_SCREENSHOT_VERDICT = {
    "verdict": "not_interesting",
    "score": 5,
    "reason": "No screenshots found near event time",
    "ocr_text": "",
    "user_activity": "",
    "suggested_action": None,
}

OCR_ONLY_VERDICT = {
    "verdict": "not_interesting",
    "score": 5,
    "reason": "Local accessibility and OCR text capture completed",
    "ocr_text": "",
    "user_activity": "",
    "suggested_action": None,
}


# Column migrations live in core.storage._ensure_column. Do not ALTER here:
# a second ADD COLUMN on some Windows SQLite builds raises SystemError and
# kills API startup.


# -----------------------------------------------------#
# ------------------- Print functions -----------------#
# -----------------------------------------------------#
def _print_verdict(tier: int, event: dict, verdict: dict):
    verdict_str = verdict["verdict"].upper()
    score = verdict["score"]
    reason = verdict["reason"]
    event_type = event["event_type"]
    process_name = event["process_name"] or "unknown"
    print(
        f"  [TIER-{tier}] {verdict_str} (score={score}/10) | {event_type} in {process_name} | {reason}"
    )


def _print_capture_text_verdict(event: dict, verdict: dict):
    verdict_str = verdict["verdict"].upper()
    process_name = event["process_name"] or "unknown"
    print(
        f"  [SCREEN TEXT] {verdict_str} (score={verdict['score']}/10) | "
        f"{event['event_type']} in {process_name} | {verdict['reason']}"
    )
    if verdict.get("user_activity"):
        print(f"    activity : {verdict['user_activity']}")
    if verdict.get("ocr_text"):
        ocr = verdict["ocr_text"].replace("\n", " ")
        preview = ocr[:120] + ("..." if len(ocr) > 120 else "")
        print(f"    ocr      : {preview}")
    if verdict.get("suggested_action"):
        print(f"    next     : {verdict['suggested_action']}")


def apply_verdict(event_id: str, verdict: dict):
    status      = "done"
    interesting = 0 if verdict["verdict"] == "not_interesting" else 1
    cursor = conn.execute(
        """UPDATE events
           SET interesting=?, interest_score=?, interest_reason=?, classification_status=?
           WHERE event_id=?""",
        (interesting, verdict["score"], verdict["reason"], status, event_id),
    )
    conn.commit()
    return bool(cursor.rowcount)


def mark_deferred(event_id: str, reason: str = "Ambiguous - deferred for catch-up") -> bool:
    cursor = conn.execute(
        """UPDATE events
           SET classification_status='deferred',
               interest_reason=?
           WHERE event_id=?
             AND classification_status IN ('pending', 'deferred')""",
        (reason, event_id),
    )
    conn.commit()
    return bool(cursor.rowcount)


def apply_vision_verdict(
    event_id: str,
    verdict: dict,
    image_embedding: list[float] | None = None,
    image_embedding_model: str | None = None,
    screenshot_filename: str | None = None,
):

    # Vision verdict is authoritative - it can see the screen, so it overrides text-tier classification
    interesting = 0 if verdict["verdict"] == "not_interesting" else 1
    cursor = conn.execute(
        """UPDATE events
           SET vision_ocr_text=?,
               vision_activity=?,
               vision_suggested_action=?,
               vector_embedding=NULL,
               image_embedding=?,
               image_embedding_model=?,
               screenshot_filename=?,
               interesting=CASE WHEN classification_status='done' THEN interesting ELSE ? END,
               interest_score=CASE WHEN classification_status='done' THEN interest_score ELSE ? END,
               interest_reason=CASE WHEN classification_status='done' THEN interest_reason ELSE ? END,
               classification_status='done'
           WHERE event_id=?
             AND classification_status IN ('done', 'screenshot_only')
             AND vision_ocr_text IS NULL
             AND vision_activity IS NULL
             AND vision_suggested_action IS NULL""",
        (
            verdict.get("ocr_text"),
            verdict.get("user_activity"),
            verdict.get("suggested_action"),
            json.dumps(image_embedding) if image_embedding else None,
            image_embedding_model,
            screenshot_filename,
            interesting,
            verdict.get("score"),
            verdict.get("reason"),
            event_id,
        ),
    )
    conn.commit()
    return bool(cursor.rowcount)


def build_capture_text_verdict(event: dict, captured_text: str) -> dict:
    window = event.get("window_context") or {}
    context = " - ".join(
        value for value in (
            str(window.get("process_name") or "").strip(),
            str(window.get("current_window_title") or "").strip(),
        ) if value
    )
    verdict = dict(OCR_ONLY_VERDICT)
    verdict["ocr_text"] = captured_text
    verdict["user_activity"] = context or str(event.get("summary") or "").strip()
    if not captured_text:
        verdict["reason"] = "No accessibility or OCR text was available"
    return verdict


def event_fingerprint(event: dict) -> str:
    """Cheap identity for duplicate ambiguous events - faster than an LLM call."""
    window = event.get("window_context") or {}
    parts = [
        str(event.get("event_type") or "").strip().casefold(),
        str(window.get("process_name") or event.get("process_name") or "").strip().casefold(),
        str(window.get("current_window_title") or "").strip().casefold(),
        str(window.get("active_url") or "").strip().casefold(),
        " ".join(str(event.get("summary") or "").split()).casefold(),
    ]
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def lookup_duplicate_verdict(event: dict, lookback_secs: float = DUPLICATE_LOOKBACK_SECS) -> dict | None:
    """Reuse a recent completed verdict for an identical ambiguous fingerprint."""
    fingerprint = event_fingerprint(event)
    cutoff = float(event.get("timestamp") or time.time()) - lookback_secs
    rows = conn.execute(
        """SELECT event_type, process_name, current_window_title, active_url, summary,
                  interesting, interest_score, interest_reason
           FROM events
           WHERE classification_status = 'done'
             AND interesting IS NOT NULL
             AND timestamp >= ?
             AND timestamp < ?
             AND event_id != ?
           ORDER BY timestamp DESC
           LIMIT 40""",
        (cutoff, event.get("timestamp") or time.time(), event["event_id"]),
    ).fetchall()
    for row in rows:
        candidate = {
            "event_type": row[0],
            "process_name": row[1],
            "summary": row[4],
            "window_context": {
                "process_name": row[1],
                "current_window_title": row[2],
                "active_url": row[3],
            },
        }
        if event_fingerprint(candidate) != fingerprint:
            continue
        interesting = int(row[5] or 0)
        score = int(row[6] if row[6] is not None else (7 if interesting else 2))
        reason = row[7] or "duplicate of recent classified event"
        return {
            "verdict": "interesting" if interesting else "not_interesting",
            "score": score,
            "reason": f"Cached duplicate: {reason}",
        }
    return None


def _row_to_event(row) -> dict:
    (
        event_id,
        timestamp,
        event_type,
        process_name,
        current_window_title,
        active_url,
        prev_process,
        prev_title,
        summary,
        payload,
    ) = row

    return {
        "event_id": event_id,
        "timestamp": timestamp,
        "event_type": event_type,
        "process_name": process_name,
        "summary": summary,
        "payload": payload,
        "window_context": {
            "process_name": process_name,
            "current_window_title": current_window_title,
            "active_url": active_url,
        },
        "previous_window_context": {
            "process_name": prev_process,
            "current_window_title": prev_title,
        }
        if prev_process
        else None,
    }


def classify_event(event: dict, *, allow_tier2: bool = False):
    """Cheap live path (Tier-0/1 + duplicate cache). Tier-2 only during catch-up."""

    # Tier 0 - rules (instant, no I/O)
    verdict = tier_zero_classifier(event)
    if verdict:
        _print_verdict(0, event, verdict)
        apply_verdict(event["event_id"], verdict)
        return


    # Tier 1 - feature scoring + personal baseline (cheap)
    verdict = tier1_score(event, conn)
    if verdict:
        _print_verdict(1, event, verdict)
        apply_verdict(event["event_id"], verdict)
        return

    cached = lookup_duplicate_verdict(event)
    if cached:
        print(
            f"  [CACHE] Reused verdict for {event['event_type']} in "
            f"{event.get('process_name') or 'unknown'} - skip LLM"
        )
        apply_verdict(event["event_id"], cached)
        return

    if not allow_tier2:
        mark_deferred(event["event_id"])
        print(
            f"  [DEFER] {event['event_type']} in {event.get('process_name') or 'unknown'} "
            f"- waiting for catch-up"
        )
        return

    # Tier 2 - LLM with last-3-event context window (catch-up only)
    recent = conn.execute(
        """SELECT * FROM (
               SELECT event_type, process_name, summary, timestamp FROM events
               WHERE timestamp < (SELECT timestamp FROM events WHERE event_id = ?)
               AND classification_status = 'done'
               ORDER BY timestamp DESC LIMIT 3
           ) ORDER BY timestamp ASC""",
        (event["event_id"],),
    ).fetchall()

    if recent:
        context_str = "\n".join(f"  [{r[0]}] {r[1]}: {r[2]}" for r in recent)
        summary = (
            f"Recent context:\n{context_str}\n\nCurrent event:\n  {event['summary']}"
        )
    else:
        summary = event["summary"]

    try:
        from core.llm_gateway import Priority
        verdict = classify_with_llm(
            summary,
            event["event_type"],
            event["window_context"],
            priority=Priority.BACKGROUND,
        )
    except Exception as e:
        note_catch_up_failure(str(e))
        note_deferred_event_skip(event["event_id"])
        print(f"  [TIER-2] Failed: {e} - leaving deferred (cooldown + skip)")
        mark_deferred(event["event_id"], reason=f"Catch-up deferred: {e}")
        return

    _print_verdict(2, event, verdict)
    apply_verdict(event["event_id"], verdict)

def classify_capture_text_event(event: dict):
    screenshots = get_screenshots_near(event["timestamp"], max_count=1)
    if not screenshots:
        _print_capture_text_verdict(event, DEFAULT_SCREENSHOT_VERDICT)
        apply_vision_verdict(event["event_id"], DEFAULT_SCREENSHOT_VERDICT)
        return


    # Show timing context before sending to the model
    event_ts   = time.strftime("%H:%M:%S", time.localtime(event["timestamp"]))
    shot_ts_ms = int(screenshots[0].stem.split("_", 1)[0])
    shot_ts    = time.strftime("%H:%M:%S", time.localtime(shot_ts_ms / 1000))
    shot_delta = int(event["timestamp"] - shot_ts_ms / 1000)
    vision_lag = int(time.time() - event["timestamp"])
    print(
        f"  [SCREEN TEXT] event@{event_ts} | screenshot@{shot_ts} "
        f"(Δ{shot_delta:+d}s vs event) | processing lag {vision_lag}s"
    )

    try:
        ocr_text, image_embedding, image_embedding_model = enrich_screenshot(screenshots[0])
    except Exception as e:
        print(f"  [SCREEN TEXT] Screenshot enrichment failed: {e}")
        ocr_text, image_embedding, image_embedding_model = "", None, None

    verdict = build_capture_text_verdict(event, ocr_text)
    _print_capture_text_verdict(event, verdict)
    apply_vision_verdict(event["event_id"], verdict, image_embedding, image_embedding_model, screenshots[0].name)


def _fetch_status_rows(status: str, limit: int, newest_first: bool = False):
    order = "DESC" if newest_first else "ASC"
    return conn.execute(
        f"""SELECT event_id, timestamp, event_type,
                  process_name, current_window_title, active_url,
                  previous_process_name, previous_window_title,
                  summary, payload
           FROM events
           WHERE classification_status = ?
           ORDER BY timestamp {order}
           LIMIT ?""",
        (status, limit),
    ).fetchall()


def worker_loop():
    """Live intake: Tier-0/1 + duplicate cache only. Never calls the LLM."""
    print("[worker] Live classification worker started (Tier-0/1 only)")
    while True:
        rows = _fetch_status_rows("pending", limit=10)
        if not rows:
            time.sleep(POLL_SECS)
            continue
        for row in rows:
            classify_event(_row_to_event(row), allow_tier2=False)


def catch_up_loop():
    """API-process drain for stranded pending + deferred Tier-2.

    Live capture owns cheap Tier-0/1 on ``pending``. When capture is off those
    rows never move, so this worker clears ``pending`` first (no LLM), then
    runs Tier-2 on ``deferred``.
    """
    print("[worker] Deferred catch-up worker started")
    while True:
        if not catch_up_allowed():
            set_catch_up_running(False)
            time.sleep(CATCH_UP_POLL_SECS)
            continue

        capture_active = bool(get_capture_status().get("active"))

        # Capture off: pending would otherwise sit forever (live worker is down).
        if not capture_active:
            pending_rows = _fetch_status_rows("pending", limit=CATCH_UP_BATCH * 4)
            if pending_rows:
                set_catch_up_running(True)
                for row in pending_rows:
                    if not catch_up_allowed() or get_capture_status().get("active"):
                        break
                    # Tier-0/1 only — ambiguous events become deferred for phase 2.
                    classify_event(_row_to_event(row), allow_tier2=False)
                set_catch_up_running(False)
                time.sleep(0.2)
                continue

        if not can_load_text():
            if ensure_text_model():
                print("[worker] text model warmed — catch-up continuing")
            else:
                note_catch_up_failure("text model unavailable")
                set_catch_up_running(False)
                time.sleep(CATCH_UP_POLL_SECS)
                continue

        # Fetch extra so per-event skip after timeouts can rotate the queue.
        rows = _fetch_status_rows("deferred", limit=CATCH_UP_BATCH * 4)
        rows = [r for r in rows if not deferred_event_skipped(r[0])][:CATCH_UP_BATCH]
        if not rows:
            set_catch_up_running(False)
            time.sleep(CATCH_UP_POLL_SECS)
            continue

        set_catch_up_running(True)
        for row in rows:
            if not catch_up_allowed():
                break
            if not can_load_text():
                if not ensure_text_model():
                    note_catch_up_failure("text model unavailable")
                    break
            classify_event(_row_to_event(row), allow_tier2=True)
        set_catch_up_running(False)
        time.sleep(1.0)


def capture_text_worker_loop():
    """Legacy OCR path kept for tests; production OCR is API screenshot_processor."""
    print("[worker] Screen text worker started")
    while True:
        if not can_run_ocr():
            time.sleep(5)
            continue
        rows = conn.execute(
            """SELECT event_id, timestamp, event_type,
                      process_name, current_window_title, active_url,
                      previous_process_name, previous_window_title,
                      summary, payload
               FROM events
               WHERE classification_status = 'screenshot_only'
               ORDER BY timestamp DESC
               LIMIT 5"""
        ).fetchall()
        if not rows:
            time.sleep(5)
            continue
        for row in rows:
            event = _row_to_event(row)
            age_secs = time.time() - event["timestamp"]
            if age_secs > MAX_VISION_WAIT_SECS:
                conn.execute(
                    "UPDATE events SET classification_status='done' WHERE event_id=?",
                    (event["event_id"],),
                )
                conn.commit()
                event_ts = time.strftime("%H:%M:%S", time.localtime(event["timestamp"]))
                print(f"  [SCREEN TEXT] Skipped stale event@{event_ts} ({age_secs:.0f}s old > {MAX_VISION_WAIT_SECS}s limit)")
                continue
            classify_capture_text_event(event)


def start_live_worker():
    t = threading.Thread(target=worker_loop, daemon=True, name="classify-live")
    t.start()
    return t


def start_catch_up_worker():
    t = threading.Thread(target=catch_up_loop, daemon=True, name="classify-catch-up")
    t.start()
    return t


def start_worker():
    """Backward-compatible alias for capture process: live cheap classification only."""
    return start_live_worker()
