from __future__ import annotations

import atexit
import os
import sys
from pathlib import Path
from typing import TypedDict

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The capture process starts the event workers once, then keeps the keyboard,
# clipboard, and foreground-window loop alive for the lifetime of the app.
import threading
import time

from pynput import keyboard

try:
    from core.baseline import compute_deviation, update_baseline
    from core.events import Event, WindowMetadata, generate_summary, get_session_id
    from core.storage import purge_expired, store_event
    from core.vision import on_activity_event, start_vision_daemon
except ImportError:
    from baseline import compute_deviation, update_baseline
    from events import Event, WindowMetadata, generate_summary, get_session_id
    from storage import purge_expired, store_event
    from vision import on_activity_event, start_vision_daemon
import uuid
from datetime import datetime

from classifier.worker import start_worker

try:
    from core.platform_support import (
        get_clipboard_text as read_clipboard_text,
    )
    from core.platform_support import (
        get_window_metadata as read_window_metadata,
    )
    from core.platform_support import (
        window_key,
    )
except ImportError:
    from platform_support import (
        get_clipboard_text as read_clipboard_text,
    )
    from platform_support import (
        get_window_metadata as read_window_metadata,
    )
    from platform_support import (
        window_key,
    )
try:
    from core.app_settings import get_capture_settings
except ImportError:
    from app_settings import get_capture_settings
try:
    from core.capture_state import set_capture_status
except ImportError:
    from capture_state import set_capture_status







def _capture_shutdown() -> None:
    set_capture_status(False, None)


def _capture_heartbeat() -> None:
    while True:
        set_capture_status(True, os.getpid())
        time.sleep(60)


set_capture_status(True, os.getpid())
atexit.register(_capture_shutdown)
threading.Thread(target=_capture_heartbeat, daemon=True, name="capture-heartbeat").start()
purge_expired()
# Capture only intakes live activity and runs cheap Tier-0/1 classification.
# Deferred Tier-2 catch-up, summarizer, screenshot OCR, and distil run in the
# API process so backlog drains even when capture is paused.
start_worker()
start_vision_daemon()


# A burst ends after a short pause; grouping keystrokes keeps activity records
# useful without writing one event per key press.
BURST_PAUSE_THRESHOLD_MS = 2000
MIN_KEYS_FOR_BURST = 3
WINDOW_POLL_INTERVAL_SECONDS = 2.0

class TypingEvent(TypedDict):
    timestamp: float
    event_type: str
    key: str | None

class TypingBurstMetrics(TypedDict):
    # Temporal metrics describe the rhythm of the burst rather than its text.
    start_time_ms: float
    end_time_ms: float
    avg_iki_ms: float
    min_iki_ms: float
    max_iki_ms: float
    avg_dwell_time_ms: float


    # The foreground window is attached to every burst for later retrieval.
    window_context: WindowMetadata


    # Volume and derived metrics are computed from key events; raw key content
    # is not persisted here beyond the event-level payloads already supported.
    word_count: int
    character_count: int
    key_down_count: int
    backspace_count: int
    delete_count: int



    typing_speed_wpm: float
    typing_speed_cpm: float
    revision_ratio: float
    max_pause_duration_ms: float
    total_duration_ms: float


class PasteEvent(TypedDict):
    timestamp: float
    window_context: WindowMetadata





# Burst Detection
# ----------------
# The timer flushes after inactivity and the context-change path flushes before
# switching windows, so a burst never straddles two foreground applications.
class BurstDetection:
    def __init__(self, on_burst_completed, on_paste_event):
        self._events: list[TypingEvent] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._on_burst_completed = on_burst_completed
        self._on_paste_event = on_paste_event
        self.window_metadata: WindowMetadata | None = None
        self._modifiers: set[str] = set()

    @staticmethod
    def _key_string(key) -> str:
        return key.char if hasattr(key, "char") and key.char else str(key)

    @staticmethod
    def _modifier_name(key_str: str) -> str | None:
        if key_str in ("Key.cmd", "Key.cmd_l", "Key.cmd_r"):
            return "cmd"
        if key_str in ("Key.ctrl", "Key.ctrl_l", "Key.ctrl_r"):
            return "ctrl"
        return None

    def on_key_press(self, key):
        with self._lock:
            key_str = self._key_string(key)
            modifier = self._modifier_name(key_str)
            if modifier:
                self._modifiers.add(modifier)



            is_paste = key_str == "\x16" or (
                key_str.lower() == "v" and bool(self._modifiers & {"cmd", "ctrl"})
            )
            if is_paste:
                self.flush_events()
                self._on_paste_event(PasteEvent(timestamp=time.time(), window_context=self.window_metadata))
                return
            self._events.append(TypingEvent(timestamp=time.time(), event_type="key_press", key=key_str))
            self._reset_timer()

    def on_key_release(self, key):
        with self._lock:
            key_str = self._key_string(key)
            self._events.append(TypingEvent(
                timestamp=time.time(),
                event_type="key_release",
                key= key_str
            ))
            self._reset_timer()
            modifier = self._modifier_name(key_str)
            if modifier:
                self._modifiers.discard(modifier)

    def _reset_timer(self):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(BURST_PAUSE_THRESHOLD_MS/1000, self._flush)
        self._timer.daemon = True
        self._timer.start()

    def flush_on_context_change(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self.flush_events()

    def _flush(self):
        with self._lock:
            self.flush_events()

    def flush_events(self):
        # Copy before clearing so the callback can safely store metrics without
        # holding the detector's mutable event list.
        events = self._events[:]
        self._events.clear()

        press_count = sum(1 for e in events if e['event_type'] == 'key_press')
        if press_count < MIN_KEYS_FOR_BURST:
            return

        metrics = compute_burst_metrics(events, self.window_metadata)
        if metrics:
            self._on_burst_completed(metrics)









def get_window_metadata() -> WindowMetadata | None:
    # Keep platform-specific foreground-window discovery behind one adapter.
    return read_window_metadata()





_last_paste_time = 0.0

def _safe_window_metadata(candidate: WindowMetadata | None = None) -> WindowMetadata:
    if candidate:
        return candidate
    return get_window_metadata() or WindowMetadata(
        timestamp=time.time(),
        current_window_title="",
        active_url=None,
        process_name="unknown",
    )


# Paste events
# ------------
# Clipboard changes use the same activity signal as typing so the vision layer
# can capture the surrounding screen once the paste has settled.
def on_paste_event(paste_event: PasteEvent):
    global _last_paste_time
    if not get_capture_settings()["capture_clipboard"]:
        return
    _last_paste_time = time.time()
    window_context = _safe_window_metadata(paste_event.get("window_context"))
    content = get_clipboard_text()
    event = Event(
        event_id=str(uuid.uuid4()),
        session_id=get_session_id(),
        timestamp=time.time(),
        event_type="paste",
        window_context=window_context,
        previous_window_context=None,
        payload= {"pasted_content": content},
        summary=None,
        vector_embedding=None,
        interest_score=None,
        interest_reason=None,
        interesting=None
    )
    event["summary"] = generate_summary(event)
    store_event(event)
    print_event(event)
    on_activity_event()


def get_clipboard_text() -> str | None:
    return read_clipboard_text()

def clipboard_monitor():
    # Polling is intentionally conservative: only meaningful text changes are
    # stored, and the short suppression window avoids duplicating key-paste.
    global _last_paste_time
    last_content = None
    was_enabled = False
    while True:
        time.sleep(1)
        enabled = bool(get_capture_settings()["capture_clipboard"])
        if not enabled:
            last_content = None
            was_enabled = False
            continue
        current = get_clipboard_text()
        if not was_enabled:
            last_content = current
            was_enabled = True
            continue
        if current and current != last_content and len(current.strip()) > 10:
            last_content = current
            if time.time() - _last_paste_time < 2.0:
                continue
            metadata = _safe_window_metadata(get_window_metadata())
            event = Event(
                event_id=str(uuid.uuid4()),
                session_id=get_session_id(),
                timestamp=time.time(),
                event_type="clipboard_change",
                window_context=metadata,
                previous_window_context=None,
                payload={"content": current},
                summary=None,
                vector_embedding=None,
                interest_score=None,
                interest_reason=None,
                interesting=None
            )
            event["summary"] = generate_summary(event)
            store_event(event)
            print_event(event)
            on_activity_event()






# Burst metrics computation
# -------------------------
# These derived values support baseline/deviation scoring without sending the
# raw event stream to another process.
def compute_burst_metrics(events: list[TypingEvent], window_metadata: WindowMetadata) -> TypingBurstMetrics:
    press_events = [event for event in events if event['event_type'] == 'key_press']
    release_events = [event for event in events if event['event_type'] == 'key_release']


    if not press_events:
        return None

    # Inter-keystroke interval is measured from one release to the next press.
    ikis = []
    last_release_time = None


    for e in events:
        if e['event_type'] == 'key_release':
            last_release_time = e['timestamp']
        elif e['event_type'] == 'key_press' and last_release_time is not None:
            iki = (e['timestamp'] - last_release_time) * 1000
            if iki >=0:
                ikis.append(iki)

    press_times = {}
    dwells = []
    for e in events:
        key = e['key']
        if e['event_type'] == 'key_press':
            press_times[key] = e['timestamp']
        elif e['event_type'] == 'key_release' and key in press_times:
            dwell = (e['timestamp'] - press_times.pop(key)) * 1000
            dwells.append(dwell)



    # Volume metrics distinguish normal text entry from revision-heavy bursts.
    backspace_count = sum(1 for e in events if e['event_type'] == 'key_release' and e['key'] == 'Key.backspace')
    delete_count = sum(1 for e in events if e['event_type'] == 'key_release' and e['key'] == 'Key.delete')


    char_count = sum(1 for e in press_events if len(e['key']) == 1 and e['key'].isprintable())
    word_count = 0
    in_word = False
    WORD_DELIMITERS = ('Key.space', 'Key.enter', 'Key.tab', ' ')
    for e in events:
        if e['event_type'] != 'key_press':
            continue
        key = e['key']
        is_delimiter = key in WORD_DELIMITERS
        is_backspace  = key == 'Key.backspace'
        is_word_char  = len(key) == 1 and key.isprintable() and not is_delimiter
        if is_word_char:
            in_word = True
        elif is_delimiter and in_word:
            word_count += 1
            in_word = False
        elif is_backspace:
            in_word = False

    if in_word:
        word_count += 1


    # Speed is intentionally calculated from the burst duration, not wall-clock
    # time between unrelated window changes.
    start_time_ms = press_events[0]['timestamp'] * 1000
    end_time_ms = (release_events[-1]['timestamp'] if release_events else press_events[-1]['timestamp']) * 1000
    total_duration_ms = end_time_ms - start_time_ms

    minutes = total_duration_ms / 60000
    typing_speed_wpm = round(word_count / minutes, 2) if minutes > 0 else 0.0
    typing_speed_cpm = round(char_count / minutes, 2) if minutes > 0 else 0.0

    key_down_count = len(press_events)
    revision_ratio = round((backspace_count + delete_count) / max(key_down_count, 1), 2)

    return TypingBurstMetrics(
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        avg_iki_ms=round(sum(ikis) / len(ikis), 2) if ikis else 0,
        min_iki_ms=round(min(ikis), 2) if ikis else 0,
        max_iki_ms=round(max(ikis), 2) if ikis else 0,
        avg_dwell_time_ms=round(sum(dwells) / len(dwells), 2) if dwells else 0,
        window_context=window_metadata,
        word_count=word_count,
        character_count=char_count,
        key_down_count=key_down_count,
        backspace_count=backspace_count,
        delete_count=delete_count,
        typing_speed_wpm=typing_speed_wpm,
        typing_speed_cpm=typing_speed_cpm,
        revision_ratio=revision_ratio,
        max_pause_duration_ms=round(max(ikis), 2) if ikis else 0,
        total_duration_ms=total_duration_ms,
    )

def print_event(event: Event):
    ts = datetime.fromtimestamp(event["timestamp"]).strftime("%H:%M:%S")
    w = event["window_context"]
    print(f"  [{ts}] {event['event_type'].upper()}")
    print(f"  window  : {w['process_name']} — {w['current_window_title']}")
    if w.get("active_url"):
        print(f"  url     : {w['active_url']}")
    if event["previous_window_context"]:
        pw = event["previous_window_context"]
        print(f"  prev    : {pw['process_name']} — {pw['current_window_title']}")
    print(f"  summary : {event['summary']}")
    print(f"  id      : {event['event_id']}")
    print()

def is_meaningful_typing(metrics: TypingBurstMetrics) -> bool:
    if metrics["word_count"] < 2:
        return False
    if metrics["key_down_count"] == 0:
        return False
    meaningful_ratio = metrics["character_count"] / metrics["key_down_count"]
    return meaningful_ratio >= 0.30

def on_burst_completed(metrics: TypingBurstMetrics):
    window_context = _safe_window_metadata(metrics.get("window_context"))
    metrics["window_context"] = window_context
    context_key = window_context["process_name"]

    if is_meaningful_typing(metrics):
        update_baseline(metrics, context_key)
        deviation = compute_deviation(metrics, context_key)
    else:
        deviation = None

    event = Event(
        event_id=str(uuid.uuid4()),
        session_id=get_session_id(),
        timestamp=time.time(),
        event_type="typing_burst",
        window_context=metrics["window_context"],
        previous_window_context=None,
        payload=metrics,
        summary=None,
        vector_embedding=None,
        interest_score=None,
        interest_reason=None,
        interesting=None
    )
    event["summary"] = generate_summary(event)
    store_event(event)
    print_event(event)
    on_activity_event()

    if deviation:
        event_2 = Event(
            event_id=str(uuid.uuid4()),
            session_id=get_session_id(),
            timestamp=time.time(),
            event_type="deviation",
            window_context=metrics["window_context"],
            previous_window_context=None,
            payload=deviation,
            summary=None,
            vector_embedding=None,
            interest_score=None,
            interest_reason=None,
            interesting=None
        )
        event_2["summary"] = generate_summary(event_2)
        store_event(event_2)
        print_event(event_2)
        on_activity_event()


burst_detector = BurstDetection(on_burst_completed=on_burst_completed, on_paste_event=on_paste_event)

# Keyboard and clipboard listeners are optional at runtime. A permission
# failure should not prevent the foreground-window loop from reporting context.
try:
    listener = keyboard.Listener(on_press=burst_detector.on_key_press, on_release=burst_detector.on_key_release)
    listener.start()
except Exception as exc:


    listener = None
    print(f"[capture] keyboard listener unavailable: {exc}")

t = threading.Thread(target=clipboard_monitor, daemon=True)
t.start()


# Polling detects app/window changes that do not generate keyboard events and
# flushes the current burst before attaching the next context.
last_window_key = None
metadata: WindowMetadata | None = None
last_window_context: WindowMetadata | None = None
last_context_change_time: float = time.time()

while True:
    try:
        current_metadata = get_window_metadata()
        current_key = window_key(current_metadata)
        if current_key != last_window_key:
            if metadata is not None:
                last_window_context = metadata
                metadata = current_metadata
                if metadata is None:
                    last_window_key = current_key
                    continue
                burst_detector.flush_on_context_change()

                now = time.time()
                dwell_ms = round((now - last_context_change_time) * 1000)
                last_context_change_time = now

                event = Event(
                    event_id=str(uuid.uuid4()),
                    session_id=get_session_id(),
                    timestamp=time.time(),
                    event_type="context_change",
                    window_context=metadata,
                    previous_window_context=last_window_context,
                    payload={
                        "dwell_ms": dwell_ms,
                        "previous_url": last_window_context.get("active_url") if last_window_context else None,
                        "current_url": metadata.get("active_url")
                    },
                    summary=None,
                    vector_embedding=None,
                    interest_score=None,
                    interest_reason=None,
                    interesting=None
                )
                event["summary"] = generate_summary(event)
                store_event(event)
                print_event(event)
                on_activity_event()
            last_window_key = current_key

        metadata = current_metadata or get_window_metadata()
        if metadata is None:
            time.sleep(WINDOW_POLL_INTERVAL_SECONDS)
            continue
        burst_detector.window_metadata = metadata
        time.sleep(WINDOW_POLL_INTERVAL_SECONDS)
    except Exception as e:
        print(f"  [ERROR] Main loop exception (continuing): {e}")
        time.sleep(WINDOW_POLL_INTERVAL_SECONDS)
