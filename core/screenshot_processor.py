import time
import threading
import uuid
from pathlib import Path
from typing import Optional

import imagehash
from PIL import Image

from storage import conn, store_event
from events import get_session_id, Event, WindowMetadata
from classifier.vision_classifier import classify_with_vision
from classifier.worker import apply_vision_verdict


POLL_SECS = 10
PHASH_THRESHOLD = 2              # bit distance: 0-2 = identical, 10+ = very different
NEAREST_EVENT_WINDOW_SECS = 10  # ±10s to find a nearby event
RECENT_THRESHOLD_SECS = 60      # screenshots within this window are processed first

_SCREENSHOT_DIR = Path(__file__).parent / "data" / "screenshots"


# ─────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────

def _get_nearest_event(screenshot_ts: float) -> Optional[dict]:
    row = conn.execute(
        """SELECT event_id, timestamp, event_type,
                  process_name, current_window_title, active_url,
                  summary, payload
           FROM events
           WHERE ABS(timestamp - ?) <= ?
           AND vision_ocr_text IS NULL
           AND vision_activity IS NULL
           AND vision_suggested_action IS NULL
           ORDER BY ABS(timestamp - ?) ASC
           LIMIT 1""",
        (screenshot_ts, NEAREST_EVENT_WINDOW_SECS, screenshot_ts)
    ).fetchone()

    if not row:
        return None

    return {
        "event_id":     row[0],
        "timestamp":    row[1],
        "event_type":   row[2],
        "process_name": row[3],
        "window_context": {
            "process_name":         row[3],
            "current_window_title": row[4],
            "active_url":           row[5],
        },
        "summary": row[6],
        "payload": row[7],
    }


def _get_window_context_at(screenshot_ts: float) -> dict:
    """Infer window context at capture time from the most recent context_change event."""
    row = conn.execute(
        """SELECT process_name, current_window_title, active_url
           FROM events
           WHERE event_type = 'context_change'
           AND timestamp <= ?
           ORDER BY timestamp DESC
           LIMIT 1""",
        (screenshot_ts,)
    ).fetchone()

    if row:
        return {
            "process_name":         row[0],
            "current_window_title": row[1],
            "active_url":           row[2],
        }
    return {"process_name": "unknown", "current_window_title": "", "active_url": None}


def _create_screenshot_event(screenshot_ts: float) -> dict:
    window_ctx = _get_window_context_at(screenshot_ts)
    event_id = str(uuid.uuid4())
    event = Event(
        event_id=event_id,
        session_id=get_session_id(),
        timestamp=screenshot_ts,
        event_type="screenshot_analysis",
        window_context=WindowMetadata(
            timestamp=screenshot_ts,
            current_window_title=window_ctx["current_window_title"],
            active_url=window_ctx["active_url"],
            process_name=window_ctx["process_name"]
        ),
        previous_window_context=None,
        payload={},
        summary=f"Background screenshot of {window_ctx['process_name']} - {window_ctx['current_window_title']}",
        vector_embedding=None,
        interest_score=None,
        interest_reason=None,
        interesting=None
    )
    store_event(event)
    # Lock out of text classifier — vision-only, set to done by apply_vision_verdict
    conn.execute(
        "UPDATE events SET classification_status='screenshot_only' WHERE event_id=?",
        (event_id,)
    )
    conn.commit()

    return {
        "event_id":   event_id,
        "timestamp":  screenshot_ts,
        "event_type": "screenshot_analysis",
        "process_name": window_ctx["process_name"],
        "window_context": {
            "process_name":         window_ctx["process_name"],
            "current_window_title": window_ctx["current_window_title"],
            "active_url":           window_ctx["active_url"],
        },
        "summary": f"Background screenshot of {window_ctx['process_name']} - {window_ctx['current_window_title']}",
    }


# ─────────────────────────────────────────────────────────────
# Screenshot discovery
# ─────────────────────────────────────────────────────────────

def _get_unprocessed_screenshots() -> list[Path]:
    """All unprocessed screenshots sorted oldest-first."""
    return sorted(
        [p for p in _SCREENSHOT_DIR.glob("*.jpg") if "_processed" not in p.stem],
        key=lambda p: int(p.stem)
    )


def _mark_as_processed(path: Path):
    path.rename(path.parent / f"{path.stem}_processed.jpg")


# ─────────────────────────────────────────────────────────────
# Hash + grouping
# ─────────────────────────────────────────────────────────────

def _compute_all_hashes(paths: list[Path]) -> dict[str, imagehash.ImageHash]:
    """Compute perceptual hash for each screenshot. Skips unreadable files."""
    hashes: dict[str, imagehash.ImageHash] = {}
    for p in paths:
        try:
            hashes[p.stem] = imagehash.phash(Image.open(p))
        except Exception as e:
            print(f"  [screenshot_processor] Hash failed for {p.name}: {e}")
    return hashes


def _group_by_similarity(
    paths: list[Path],
    hashes: dict[str, imagehash.ImageHash],
) -> list[list[Path]]:
    """
    Group screenshots that look visually identical (pHash distance ≤ PHASH_THRESHOLD)
    using Union-Find. Each group is sorted oldest-first; the last element is the
    most recent (used as the vision representative).
    """
    valid = [p for p in paths if p.stem in hashes]

    # Union-Find
    parent = {p.stem: p.stem for p in valid}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path compression
            x = parent[x]
        return x

    def union(x: str, y: str):
        parent[find(x)] = find(y)

    # O(n²) pairwise — fast enough for typical screenshot counts (<200)
    for i, pa in enumerate(valid):
        for pb in valid[i + 1:]:
            if (hashes[pa.stem] - hashes[pb.stem]) <= PHASH_THRESHOLD:
                union(pa.stem, pb.stem)

    # Collect groups
    groups: dict[str, list[Path]] = {}
    for p in valid:
        root = find(p.stem)
        groups.setdefault(root, []).append(p)

    # Sort each group oldest → newest so group[-1] is always the representative
    for g in groups.values():
        g.sort(key=lambda p: int(p.stem))

    return list(groups.values())


# ─────────────────────────────────────────────────────────────
# Processing
# ─────────────────────────────────────────────────────────────

def _process_group(group: list[Path]) -> bool:
    """
    Run vision once on the most recent screenshot in the group (the representative),
    then copy the OCR/activity verdict to all other screenshots in the group.
    Each screenshot still looks up its own nearest event independently so
    window_context and process_name remain accurate per event.

    Returns True if successful (all members marked processed).
    Returns False if the representative failed (no members marked processed).
    """
    representative = group[-1]  # most recent = best context
    rep_ts = int(representative.stem) / 1000.0

    rep_event = _get_nearest_event(rep_ts)
    if rep_event is None:
        print(f"  [screenshot_processor] No nearby event for {representative.name} — creating screenshot_analysis event")
        rep_event = _create_screenshot_event(rep_ts)
    else:
        print(
            f"  [screenshot_processor] {representative.name} → attaching to "
            f"{rep_event['event_type']} [{rep_event['event_id'][:8]}]"
            + (f" | group of {len(group)}" if len(group) > 1 else "")
        )

    try:
        verdict = classify_with_vision(rep_event, [representative])
        apply_vision_verdict(rep_event["event_id"], verdict)
        activity = verdict.get("user_activity", "")[:80]
        print(f"  [screenshot_processor] {verdict['verdict']} | {activity}")
    except Exception as e:
        print(f"  [screenshot_processor] Vision failed for {representative.name}: {e}")
        return False  # do not mark any member processed — retry next cycle

    # Copy verdict to all other group members (different timestamps, same screen content)
    for path in group[:-1]:
        ts = int(path.stem) / 1000.0
        other_event = _get_nearest_event(ts)
        if other_event is None:
            other_event = _create_screenshot_event(ts)
        apply_vision_verdict(other_event["event_id"], verdict)
        _mark_as_processed(path)
        print(f"  [screenshot_processor] Copied verdict to duplicate {path.name[:20]}... [{other_event['event_id'][:8]}]")

    _mark_as_processed(representative)
    return True


# ─────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────

def screenshot_processor_loop():
    print("[screenshot_processor] Started")

    while True:
        time.sleep(POLL_SECS)

        all_unprocessed = _get_unprocessed_screenshots()
        if not all_unprocessed:
            continue

        hashes = _compute_all_hashes(all_unprocessed)
        groups = _group_by_similarity(all_unprocessed, hashes)

        # Sort groups: most recent representative first
        groups.sort(key=lambda g: int(g[-1].stem), reverse=True)

        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - RECENT_THRESHOLD_SECS * 1000

        recent_groups = [g for g in groups if int(g[-1].stem) >= cutoff_ms]
        old_groups    = [g for g in groups if int(g[-1].stem) <  cutoff_ms]

        for group in recent_groups:
            _process_group(group)

        if not recent_groups and old_groups:
            # Process the oldest group first when idle
            _process_group(old_groups[-1])


def start_screenshot_processor() -> threading.Thread:
    t = threading.Thread(target=screenshot_processor_loop, daemon=True)
    t.start()
    return t
