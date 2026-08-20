"""
Queue / backlog snapshot for Clippy Vision.

Shows what is waiting for classification, Tier-2 catch-up, screenshot OCR,
summarization, and distillation — plus whether catch-up is currently allowed.

Usage (PowerShell):
  cd c:\\Users\\proto\\Clippy_Vision
  $env:PYTHONPATH = (Get-Location).Path
  python .\\scripts\\probe_queues.py
  python .\\scripts\\probe_queues.py --watch
  python .\\scripts\\probe_queues.py -w 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.app_settings import get_capture_settings
from core.backlog import catch_up_allowed, get_backlog_status
from core.capture_state import get_capture_status
from core.distil import (
    DISTIL_EVERY_N_SESSIONS,
    count_sessions_since_last_distil,
    should_distil,
)
from core.model_residency import can_load_text, can_run_ocr, text_model_loaded
from core.paths import get_screenshots_dir
from core.storage import conn


def _age_secs(ts: float | None) -> str:
    if ts is None:
        return "-"
    age = max(0, int(time.time() - float(ts)))
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    return f"{age / 3600:.1f}h"


def _classification_counts() -> list[tuple[str, int, float | None]]:
    rows = conn.execute(
        """SELECT classification_status,
                  COUNT(*),
                  MIN(timestamp)
           FROM events
           GROUP BY classification_status
           ORDER BY COUNT(*) DESC"""
    ).fetchall()
    return [(str(r[0] or "(null)"), int(r[1]), float(r[2]) if r[2] is not None else None) for r in rows]


def _unprocessed_screenshots() -> tuple[int, float | None]:
    shots = [
        p
        for p in get_screenshots_dir().glob("*.jpg")
        if "_processed" not in p.stem
    ]
    oldest = None
    for path in shots:
        try:
            ts = int(path.stem.split("_", 1)[0]) / 1000.0
        except ValueError:
            continue
        oldest = ts if oldest is None else min(oldest, ts)
    return len(shots), oldest


def _unsummarized_events() -> tuple[int, float | None]:
    # Events that have no overlapping session summary yet (same idea as summarizer lookback).
    row = conn.execute(
        """SELECT COUNT(*), MIN(e.timestamp)
           FROM events e
           WHERE e.timestamp >= ?
             AND NOT EXISTS (
               SELECT 1 FROM sessions s
               WHERE e.session_id = s.session_id
                 AND e.timestamp >= s.window_start
                 AND e.timestamp <= s.window_end
             )""",
        (time.time() - 7 * 86400,),
    ).fetchone()
    return int(row[0] or 0), (float(row[1]) if row[1] is not None else None)


def _rag_pending() -> int | None:
    settings = get_capture_settings()
    if not settings.get("rag_enabled"):
        return None
    row = conn.execute(
        """SELECT COUNT(*) FROM events
           WHERE vector_embedding IS NULL
             AND (interesting = 1 OR event_type IN (
               'context_change','paste','clipboard_change','screenshot_analysis',
               'deviation','typing_burst','mouse_burst'
             ))"""
    ).fetchone()
    return int(row[0] or 0)


def _meta(key: str, default=None):
    row = conn.execute("SELECT value FROM memory_meta WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def print_snapshot(*, watch_interval: float | None = None) -> None:
    print("Clippy Vision queue snapshot")
    print("=" * 64)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"time: {stamp}")
    if watch_interval is not None:
        print(f"watch: every {watch_interval:g}s  (Ctrl+C to stop)")
    print()

    capture = get_capture_status()
    print(f"capture active: {bool(capture.get('active'))}")
    print(f"text model loaded: {text_model_loaded()}")
    print(f"can_load_text: {can_load_text()}   can_run_ocr: {can_run_ocr()}")
    print(f"catch_up_allowed: {catch_up_allowed()}")
    print()

    print("--- Classification (events by status) ---")
    counts = _classification_counts()
    if not counts:
        print("  (no events)")
    total_events = 0
    pending = deferred = 0
    for status, n, oldest in counts:
        total_events += n
        if status == "pending":
            pending = n
        if status == "deferred":
            deferred = n
        print(f"  {status:18} {n:6}   oldest={_age_secs(oldest)}")
    print(f"  {'TOTAL':18} {total_events:6}")
    print()
    print("  pending  = waiting for Tier-0/1 (live while capturing; catch-up when idle)")
    print("  deferred = ambiguous; waiting for automatic Tier-2 catch-up")
    print(f"  Tier-2 catch-up queue (deferred): {deferred}")
    print(f"  Live/idle classify queue (pending): {pending}")
    print()

    backlog = get_backlog_status()
    print("--- Catch-up / backlog gate ---")
    print(f"  deferred: {backlog['deferred']}   pending: {backlog['pending']}")
    print(f"  recommend drain (while capturing): {backlog['recommend']}")
    print(f"  oldest deferred age: {_age_secs(backlog.get('oldest_deferred_ts'))}")
    print(f"  catch_up_running: {backlog.get('catch_up_running')}")
    print(f"  cooldown remaining: {backlog.get('cooldown_remaining_secs')}s")
    if backlog.get("last_error"):
        print(f"  last_error: {backlog['last_error']}")
    print()

    shot_n, shot_oldest = _unprocessed_screenshots()
    print("--- Screenshot OCR queue ---")
    print(f"  unprocessed JPGs: {shot_n}   oldest={_age_secs(shot_oldest)}")
    print()

    sum_n, sum_oldest = _unsummarized_events()
    print("--- Summarizer (approx, last 7 days) ---")
    print(f"  events without a covering session summary: {sum_n}   oldest={_age_secs(sum_oldest)}")
    print()

    since = count_sessions_since_last_distil()
    last_at = _meta("last_distilled_at", 0) or 0
    print("--- Distiller ---")
    print(f"  sessions since last distil: {since} / {DISTIL_EVERY_N_SESSIONS}")
    print(f"  should_distil now: {should_distil()}")
    print(f"  last_distilled_at: {_age_secs(float(last_at)) if last_at else 'never'}")
    print()

    rag_n = _rag_pending()
    print("--- Event RAG embeddings ---")
    if rag_n is None:
        print("  rag_enabled: False (indexer off — no queue)")
    else:
        print(f"  events missing vector_embedding: {rag_n}")
    print()
    if watch_interval is None:
        print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Clippy Vision queue / backlog snapshot")
    parser.add_argument(
        "-w",
        "--watch",
        nargs="?",
        const=5.0,
        type=float,
        metavar="SECS",
        help="Refresh periodically (default 5s if flag given with no value)",
    )
    args = parser.parse_args()
    interval = args.watch

    if interval is None:
        print_snapshot()
        return

    if interval <= 0:
        print("watch interval must be > 0", file=sys.stderr)
        sys.exit(2)

    try:
        while True:
            _clear_screen()
            print_snapshot(watch_interval=interval)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
