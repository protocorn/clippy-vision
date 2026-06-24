from pathlib import Path
import io
import mss
from PIL import Image
import threading
import time
from typing import Optional

_SCREENSHORT_DIR = Path(__file__).parent / "data" / "screenshots"
_SCREENSHORT_DIR.mkdir(parents=True, exist_ok=True)

MIN_GAP_SECONDS = 8

BACKGROUND_INTERVALS_SECS = 60
SCREENSHOT_TTL_MS = 24 * 60 * 60 * 1000 # 24 hours
JPEG_QUALITY = 75
ACTIVITY_DELAYS_SECS = (0, 4, 8)



_lock = threading.Lock()
global _last_capture_ms
_last_capture_ms = 0

def capture_screenshot(timestamp_ms: int) -> Optional[Path]:
    try:
        with mss.mss() as sct:
            screenshot = sct.grab(sct.monitors[1])
            img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
        
        path = _SCREENSHORT_DIR / f"{timestamp_ms}.jpg"
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        path.write_bytes(buf.getvalue())
        return path
    except Exception as e:
        print(f"Error capturing screenshot: {e}")
        return None
    

def _capture_if_not_recent() -> None:
    global _last_capture_ms
    with _lock:
        now_ms = int(time.time() * 1000)
        if now_ms - _last_capture_ms < MIN_GAP_SECONDS * 1000:
            return
        _last_capture_ms = now_ms
        capture_screenshot(now_ms)

def purge_expired_screenshots() -> None:
    cutoff_ms = int(time.time() * 1000) - SCREENSHOT_TTL_MS
    for path in _SCREENSHORT_DIR.glob("*.jpg"):
        try:
            ts_part = path.stem.split("_")[0]
            if int(ts_part) < cutoff_ms:
                path.unlink()
        except ValueError:
            continue
        except Exception as e:
            print(f"Error purging expired screenshots: {e}")

def on_activity_event()-> None:

    for delay in ACTIVITY_DELAYS_SECS:
        t = threading.Timer(delay, _capture_if_not_recent)
        t.daemon = True
        t.start()
    
def get_screenshots_near(
    event_timestamp: float,
    max_count: int = 4,
    window_secs: float = 45,
) -> list[Path]:
    target_ms = int(event_timestamp * 1000)
    window_ms = int(window_secs * 1000)
    candidates: list[tuple[int, Path]] = []
    for path in _SCREENSHORT_DIR.glob("*.jpg"):
        try:
            ts_ms = int(path.stem)
        except ValueError:
            continue
        # Only consider screenshots taken up to window_secs before the event
        # or up to 10 s after (camera lag), never far-future shots.
        offset = ts_ms - target_ms
        if -window_ms <= offset <= 10_000:
            candidates.append((abs(offset), path))
    candidates.sort(key=lambda x: x[0])
    return [path for _, path in candidates[:max_count]]

    
def start_background_capture() -> None:
    while True:
        _capture_if_not_recent()
        purge_expired_screenshots()
        time.sleep(BACKGROUND_INTERVALS_SECS)

def start_vision_daemon() -> threading.Thread:
    t = threading.Thread(target=start_background_capture, daemon=True)
    t.start()
    print("Vision daemon started")
    return t