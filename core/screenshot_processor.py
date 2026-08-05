import time
import threading
import uuid
from pathlib import Path
from typing import Optional

import imagehash
from PIL import Image

try:
    from core.storage import conn, store_event
    from core.events import get_session_id, Event, WindowMetadata
except ImportError:
    from storage import conn, store_event
    from events import get_session_id, Event, WindowMetadata
from classifier.vision_classifier import classify_with_vision
from classifier.worker import OCR_ONLY_VERDICT, apply_vision_verdict
from core.screenshot_enrichment import enrich_screenshot, merge_ocr_text


POLL_SECS = 10
PHASH_THRESHOLD = 2
BURST_COLLAPSE_WINDOW_MS = 30_000
NEAREST_EVENT_WINDOW_SECS = 10
RECENT_THRESHOLD_SECS = 60
HASH_CACHE_MAX = 512

try:
    from core.paths import get_screenshots_dir
except ImportError:
    from paths import get_screenshots_dir

_SCREENSHOT_DIR = get_screenshots_dir()
_HASH_CACHE: dict[str, tuple[int, int, str]] = {}


def _screenshot_timestamp_ms(path: Path) -> int | None:
    try:
        return int(path.stem.split("_", 1)[0])
    except ValueError:
        return None






def _get_nearest_event(screenshot_ts: float) -> Optional[dict]:
    row = conn.execute(
        """SELECT event_id, timestamp, event_type,
                  process_name, current_window_title, active_url,
                  summary, payload
           FROM events
           WHERE ABS(timestamp - ?) <= ?
           AND classification_status IN ('awaiting_vision', 'screenshot_only')
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
        image_embedding=None,
        image_embedding_model=None,
        interest_score=None,
        interest_reason=None,
        interesting=None
    )
    store_event(event)

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






def _get_unprocessed_screenshots() -> list[Path]:
    """All unprocessed screenshots sorted oldest-first."""
    candidates = [
        p for p in _SCREENSHOT_DIR.glob("*.jpg")
        if "_processed" not in p.stem and _screenshot_timestamp_ms(p) is not None
    ]
    return sorted(candidates, key=lambda p: _screenshot_timestamp_ms(p) or 0)


def _mark_as_processed(path: Path):
    if not path.exists():
        return False
    target = path.parent / f"{path.stem}_processed.jpg"
    if target.exists():
        path.unlink(missing_ok=True)
        return True
    path.rename(target)
    return True






def _compute_all_hashes(paths: list[Path]) -> dict[str, imagehash.ImageHash]:
    """Compute perceptual hash for each screenshot. Skips unreadable files."""
    hashes: dict[str, imagehash.ImageHash] = {}
    for p in paths:
        try:
            stat = p.stat()
            cache_key = str(p)
            cached = _HASH_CACHE.get(cache_key)
            if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                hashes[p.stem] = imagehash.hex_to_hash(cached[2])
                continue

            with Image.open(p) as image:
                digest = imagehash.phash(image)
            hashes[p.stem] = digest
            _HASH_CACHE[cache_key] = (stat.st_mtime_ns, stat.st_size, str(digest))
        except Exception as e:
            print(f"  [screenshot_processor] Hash failed for {p.name}: {e}")




    if len(_HASH_CACHE) > HASH_CACHE_MAX:
        live = {str(p) for p in paths}
        for key in list(_HASH_CACHE):
            if key not in live:
                del _HASH_CACHE[key]
        for key in list(_HASH_CACHE)[:-HASH_CACHE_MAX]:
            del _HASH_CACHE[key]
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


    parent = {p.stem: p.stem for p in valid}
    group_bounds = {
        p.stem: (
            (_screenshot_timestamp_ms(p) or 0),
            (_screenshot_timestamp_ms(p) or 0),
        )
        for p in valid
    }

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> bool:
        root_x = find(x)
        root_y = find(y)
        if root_x == root_y:
            return True
        low = min(group_bounds[root_x][0], group_bounds[root_y][0])
        high = max(group_bounds[root_x][1], group_bounds[root_y][1])
        if high - low > BURST_COLLAPSE_WINDOW_MS:
            return False
        parent[root_x] = root_y
        group_bounds[root_y] = (low, high)
        return True


    for i, pa in enumerate(valid):
        for pb in valid[i + 1:]:
            pa_ts = _screenshot_timestamp_ms(pa)
            pb_ts = _screenshot_timestamp_ms(pb)
            if pa_ts is None or pb_ts is None:
                continue
            if pb_ts - pa_ts > BURST_COLLAPSE_WINDOW_MS:
                break
            if (hashes[pa.stem] - hashes[pb.stem]) <= PHASH_THRESHOLD:
                union(pa.stem, pb.stem)


    groups: dict[str, list[Path]] = {}
    for p in valid:
        root = find(p.stem)
        groups.setdefault(root, []).append(p)


    for g in groups.values():
        g.sort(key=lambda p: _screenshot_timestamp_ms(p) or 0)

    return list(groups.values())






def _process_group(group: list[Path]) -> bool:
    """
    Run vision once on the most recent screenshot in the group (the representative),
    then copy the OCR/activity verdict to all other screenshots in the group.
    Each screenshot still looks up its own nearest event independently so
    window_context and process_name remain accurate per event.

    Returns True if successful (all members marked processed).
    Returns False if the representative failed (no members marked processed).
    """
    representative = group[-1]
    rep_ts = (_screenshot_timestamp_ms(representative) or 0) / 1000.0

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
        ocr_text, image_embedding, image_embedding_model = enrich_screenshot(representative)
    except Exception as e:
        print(f"  [screenshot_processor] Screenshot enrichment failed for {representative.name}: {e}")
        ocr_text, image_embedding, image_embedding_model = "", None, None

    try:
        verdict = classify_with_vision(rep_event, [representative])
    except Exception as e:
        print(f"  [screenshot_processor] Vision classifier unavailable: {e}")
        verdict = dict(OCR_ONLY_VERDICT)
    verdict["ocr_text"] = merge_ocr_text(ocr_text, verdict.get("ocr_text"))
    applied = apply_vision_verdict(
        rep_event["event_id"],
        verdict,
        image_embedding,
        image_embedding_model,
        representative.name,
    )
    if not applied:
        rep_event = _create_screenshot_event(rep_ts)
        applied = apply_vision_verdict(
            rep_event["event_id"],
            verdict,
            image_embedding,
            image_embedding_model,
            representative.name,
        )
    if not applied:
        return False
    activity = verdict.get("user_activity", "")[:80]
    print(f"  [screenshot_processor] {verdict['verdict']} | {activity}")


    for path in group[:-1]:
        ts = (_screenshot_timestamp_ms(path) or 0) / 1000.0
        other_event = _get_nearest_event(ts)
        if other_event is None:
            other_event = _create_screenshot_event(ts)
        applied = apply_vision_verdict(
            other_event["event_id"],
            verdict,
            image_embedding,
            image_embedding_model,
            path.name,
        )
        if not applied:
            continue
        _mark_as_processed(path)
        print(f"  [screenshot_processor] Copied verdict to duplicate {path.name[:20]}... [{other_event['event_id'][:8]}]")

    return _mark_as_processed(representative)






def screenshot_processor_loop():
    print("[screenshot_processor] Started")

    while True:
        time.sleep(POLL_SECS)

        all_unprocessed = _get_unprocessed_screenshots()
        if not all_unprocessed:
            continue

        hashes = _compute_all_hashes(all_unprocessed)
        groups = _group_by_similarity(all_unprocessed, hashes)


        groups.sort(key=lambda g: _screenshot_timestamp_ms(g[-1]) or 0, reverse=True)

        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - RECENT_THRESHOLD_SECS * 1000

        recent_groups = [g for g in groups if (_screenshot_timestamp_ms(g[-1]) or 0) >= cutoff_ms]
        old_groups    = [g for g in groups if (_screenshot_timestamp_ms(g[-1]) or 0) <  cutoff_ms]

        for group in recent_groups:
            try:
                _process_group(group)
            except Exception as exc:
                print(f"  [screenshot_processor] Group failed: {exc}")

        if not recent_groups and old_groups:

            try:
                _process_group(old_groups[-1])
            except Exception as exc:
                print(f"  [screenshot_processor] Group failed: {exc}")


def start_screenshot_processor() -> threading.Thread:
    t = threading.Thread(target=screenshot_processor_loop, daemon=True)
    t.start()
    return t
