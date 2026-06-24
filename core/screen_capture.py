import win32api
import win32gui
from typing import TypedDict, Optional, List

import uiautomation as auto # UI Automation - used to get the active url from the browser window

import win32process # Windows API - used to get the process name associated with a given window handlesss
import psutil 

from pynput import keyboard # used to listen for keyboard events

import time
import threading

from baseline import update_baseline, compute_deviation

from events import Event, WindowMetadata, get_session_id, generate_summary
import uuid
from datetime import datetime
from pathlib import Path

from storage import store_event, purge_expired

from classifier.worker import start_worker

from vision import start_vision_daemon, on_activity_event
import win32clipboard

from summarizer import start_summarizer
from distil import should_distil, distil
from screenshot_processor import start_screenshot_processor

#-----------------------------------------------------#
# Currently launched only once at the start of -------#
# the program. To be launched periodically in the ----#
# future. --------------------------------------------#
#-----------------------------------------------------#

purge_expired()
start_worker()
start_vision_daemon()
start_summarizer()
start_screenshot_processor()
if should_distil():
    print("[startup] Distillation threshold reached — running distil...")
    distil()

## Constants
BURST_PAUSE_THRESHOLD_MS = 2000
MIN_KEYS_FOR_BURST = 3

class TypingEvent(TypedDict):
    timestamp: float
    event_type: str
    key: Optional[str]

class TypingBurstMetrics(TypedDict):
    # Temporal Metrics
    start_time_ms: float
    end_time_ms: float
    avg_iki_ms: float # Inter-keystroke interval (flight time)
    min_iki_ms: float
    max_iki_ms: float
    avg_dwell_time_ms: float # Dwell time (time spent on a key)

    # Contextual Metrics
    window_context: WindowMetadata
   
    # Vloume
    word_count: int
    character_count: int
    key_down_count: int
    backspace_count: int
    delete_count: int
    

    # derived metrics
    typing_speed_wpm: float # words per minute
    typing_speed_cpm: float # characters per minute
    revision_ratio: float # ratio of revision to total keystrokes
    max_pause_duration_ms: float
    total_duration_ms: float


class PasteEvent(TypedDict):
    timestamp: float
    window_context: WindowMetadata

##############################################################
################## Burst Detection ###########################
##############################################################

class BurstDetection:
    def __init__(self, on_burst_completed, on_paste_event):
        self._events: List[TypingEvent] = []
        self._lock = threading.Lock() # used to synchronize access to the events list
        self._timer: Optional[threading.Timer] = None
        self._on_burst_completed = on_burst_completed
        self._on_paste_event = on_paste_event
        self.window_metadata: Optional[WindowMetadata] = None

    def on_key_press(self, key):
        with self._lock:
            key_str = key.char if hasattr(key, 'char') and key.char else str(key)
            if key_str == '\x16':
                self.flush_events()
                self._on_paste_event(PasteEvent(timestamp=time.time(), window_context=self.window_metadata))
                return
            self._events.append(TypingEvent(timestamp=time.time(), event_type="key_press", key=key_str))
            self._reset_timer()

    def on_key_release(self, key):
        with self._lock:
            key_str = key.char if hasattr(key, 'char') and key.char else str(key)
            self._events.append(TypingEvent(
                timestamp=time.time(),
                event_type="key_release",
                key= key_str
            ))
            self._reset_timer()

    def _reset_timer(self):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(BURST_PAUSE_THRESHOLD_MS/1000, self._flush) # Wait for BURST_PAUSE_THRESHOLD_S seconds before flushing the events
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
        events = self._events[:] # create a copy of the events list to avoid modifying the original list while iterating
        self._events.clear()

        press_count = sum(1 for e in events if e['event_type'] == 'key_press')
        if press_count < MIN_KEYS_FOR_BURST:
            return
        
        metrics = compute_burst_metrics(events, self.window_metadata)
        if metrics:
            self._on_burst_completed(metrics)


##############################################################
############ Window Polling Functions ########################
##############################################################

def get_process_name(hwnd: int) -> str:
    """
    This function retrieves the process name associated with a given window handle (hwnd).
    Process name is different from window title as "Unititled - Notepad" and "main.py Notepad" are different window titles but same process name "notepad.exe"
    """
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name()
    except Exception:
        return "unknown"
    
def is_browser_window(class_name: str) -> bool:
    """"
    This function checks if the given window class name corresponds to a known browser window.
    """
    if class_name in ("Chrome_WidgetWin_1", "MozillaWindowClass"):
        return True
    return False

def get_browser_url(window: auto.WindowControl, class_name: str) -> Optional[str]:
    
    """
    This function attempts to retrieve the active URL from a browser window by searching for the address bar control.
    """
    
    #----------------------------------------------------------#
    # TESTED ON : Chrome, edge and brave-----------------------#
    # YET TO TEST : Firefox, Opera, Safari (winodws)-----------#
    #----------------------------------------------------------#
    
    try:
        addr = None
        if class_name == "Chrome_WidgetWin_1":
            # Edge exposes a stable AutomationId; try it first
            addr = window.EditControl(AutomationId="addressEditBox", searchDepth=15)
            if not addr.Exists(0):
                # Chrome / Brave / Opera fallback
                addr = window.EditControl(Name="Address and search bar", searchDepth=15)
        
        elif class_name == "MozillaWindowClass": 
            # Firefox — label varies by locale, use SubName (partial match)
            addr = window.EditControl(SubName="Search with Google or enter address", searchDepth=15)
            if not addr.Exists(0):
                addr = window.EditControl(SubName="Search or enter address", searchDepth=15)
        
        if addr and addr.Exists(0.5):
            return addr.GetValuePattern().Value
        return None
    except Exception:
        return None

def get_window_metadata() -> Optional[WindowMetadata]:
    try:
        hwnd = win32gui.GetForegroundWindow()
        class_name = win32gui.GetClassName(hwnd)
        active_url = get_browser_url(auto.WindowControl(Handle=hwnd), class_name) if is_browser_window(class_name) else None
        return WindowMetadata(
            timestamp=time.time(),
            current_window_title=win32gui.GetWindowText(hwnd),
            active_url=active_url,
            process_name=get_process_name(hwnd)
        )
    except Exception:
        return None

#############################################################
####################### PASTE EVENTS ########################
#############################################################

_last_paste_time = 0.0

def on_paste_event(paste_event: PasteEvent):
    global _last_paste_time
    _last_paste_time = time.time()
    content = get_clipboard_text() 
    event = Event(
        event_id=str(uuid.uuid4()),
        session_id=get_session_id(),
        timestamp=time.time(),
        event_type="paste",
        window_context=paste_event["window_context"],
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


def get_clipboard_text() -> Optional[str]:
    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                return data[:2000]
            return None
        finally:
            win32clipboard.CloseClipboard() 
    except:
        return None

def clipboard_monitor():
    global _last_paste_time
    last_content = get_clipboard_text()
    while True:
        time.sleep(1)
        current = get_clipboard_text()
        if current and current != last_content and len(current.strip()) > 10:
            last_content = current
            if time.time() - _last_paste_time < 2.0:
                continue
            metadata = get_window_metadata()
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


###########################################################
############# Burst Metrics Computation ###################
###########################################################

def compute_burst_metrics(events: List[TypingEvent], window_metadata: WindowMetadata) -> TypingBurstMetrics:
    press_events = [event for event in events if event['event_type'] == 'key_press']
    release_events = [event for event in events if event['event_type'] == 'key_release']


    if not press_events:
        return None

    ikis = []
    last_release_time = None

    # Iki (Flight time) computation
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
    
    # Volume metrics

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
            word_count += 1   # only counts ONCE per word, no matter how many spaces
            in_word = False
        elif is_backspace:
            in_word = False   # rough but reasonable
    # count the final word if burst ended without a trailing space
    if in_word:
        word_count += 1

    # Derived metrics
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
    context_key = metrics["window_context"]["process_name"]

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

listener = keyboard.Listener(on_press=burst_detector.on_key_press, on_release=burst_detector.on_key_release)
listener.start() 

t = threading.Thread(target=clipboard_monitor, daemon=True)
t.start()


last_hwnd = None
metadata: Optional[WindowMetadata] = None
last_window_context: Optional[WindowMetadata] = None
last_context_change_time: float = time.time()

while True:
    try:
        hwnd = win32gui.GetForegroundWindow()
        if hwnd != last_hwnd:
            if metadata is not None:
                last_window_context = metadata
                metadata = get_window_metadata() # to get the new window metadata
                if metadata is None:
                    last_hwnd = hwnd
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
            last_hwnd = hwnd

        metadata = get_window_metadata()
        if metadata is None:
            time.sleep(5)
            continue
        burst_detector.window_metadata = metadata
        time.sleep(5)
    except Exception as e:
        print(f"  [ERROR] Main loop exception (continuing): {e}")
        time.sleep(5)