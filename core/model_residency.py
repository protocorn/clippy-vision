"""Vision model residency tied to screen capture — not app launch.

Startup (API):     pin text only; vision idle / unloaded.
Capture start:     pin vision if RAM allows, else on-demand (short keep_alive).
Capture stop:      unload vision.
While pinned:      if free RAM drops below the floor, demote to on-demand.

Persists to <data>/model_residency.json. Gateway reads policy via keep_alive_for().
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal

import psutil

try:
    from core.paths import get_data_dir
except ImportError:
    from paths import get_data_dir
try:
    from core.llm_config import get_llm_config, is_external_provider, model_for
except ImportError:
    from llm_config import get_llm_config, is_external_provider, model_for

TEXT_MODEL = "qwen3:8b"
VL_MODEL = "qwen3-vl:4b"

VisionPolicy = Literal["idle", "pinned", "on_demand"]

_GB = 1024**3
_EST_VL = 3.5 * _GB
_FREE_FLOOR = 3.5 * _GB
_PRESSURE_INTERVAL_S = 30

KEEP_ALIVE_PINNED = "1h"
KEEP_ALIVE_VL_EPHEMERAL = "5m"
KEEP_ALIVE_UNLOAD = 0

_OLLAMA = "http://127.0.0.1:11434"
_STATE_NAME = "model_residency.json"

_policy: VisionPolicy = "idle"
_monitor_stop = threading.Event()
_monitor_thread: threading.Thread | None = None
_lock = threading.Lock()


def _state_path() -> Path:
    return get_data_dir() / _STATE_NAME


def _available() -> int:
    return int(psutil.virtual_memory().available)


def _mb(n: int) -> int:
    return round(n / (1024 * 1024))


def _can_pin_vision(available: int | None = None) -> bool:
    """True if VL plus a free-RAM floor still fits."""
    free = _available() if available is None else available
    return free >= _EST_VL + _FREE_FLOOR


def load_residency() -> dict:
    global _policy
    path = _state_path()
    if not path.exists():
        _policy = "idle"
        return {"vision": _policy}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[residency] read failed: {e}")
        _policy = "idle"
        return {"vision": _policy}

    vision = data.get("vision") or data.get("mode")
    if vision == "dual":
        vision = "pinned"
    elif vision == "single":
        vision = "on_demand"
    if vision not in ("idle", "pinned", "on_demand"):
        vision = "idle"
    _policy = vision
    data["vision"] = _policy
    return data


def _persist(vision: VisionPolicy, **extra: Any) -> dict:
    global _policy
    _policy = vision
    payload = {
        "vision": vision,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "available_ram_mb": _mb(_available()),
        **extra,
    }
    _state_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[residency] vision={vision}  free~{payload['available_ram_mb']}MB")
    return payload


def keep_alive_for(model: str) -> str | int:
    if is_external_provider():



        return KEEP_ALIVE_UNLOAD
    if "vl" not in (model or "").lower():
        return KEEP_ALIVE_PINNED
    if _policy == "pinned":
        return KEEP_ALIVE_PINNED
    if _policy == "on_demand":
        return KEEP_ALIVE_VL_EPHEMERAL
    return KEEP_ALIVE_UNLOAD


def _ollama_post(path: str, body: dict, timeout: float = 90) -> None:
    base = get_llm_config().get("base_url", _OLLAMA).rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]
    elif base.endswith("/v1"):
        base = base[:-3]
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def _warm(model: str, keep_alive: str | int = KEEP_ALIVE_PINNED, timeout: float = 90) -> None:
    config = get_llm_config()
    if config["provider"] != "ollama":
        print(f"[residency] skip warm for external provider ({config['provider']})")
        return
    if model == VL_MODEL:
        model = model_for("vision", VL_MODEL)
    elif model == TEXT_MODEL:
        model = model_for("chat", TEXT_MODEL)
    print(f"[residency] warm {model} (keep_alive={keep_alive!r})")
    _ollama_post(
        "/api/generate",
        {"model": model, "prompt": "ping", "stream": False, "keep_alive": keep_alive},
        timeout=timeout,
    )


def _unload_vision() -> None:
    if is_external_provider():
        return
    vision_model = model_for("vision", VL_MODEL)
    print(f"[residency] unload {vision_model}")
    try:
        _ollama_post(
            "/api/generate",
            {
                "model": vision_model,
                "prompt": "ping",
                "stream": False,
                "keep_alive": KEEP_ALIVE_UNLOAD,
            },
            timeout=30,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[residency] unload failed: {e}")


def _stop_monitor() -> None:
    global _monitor_thread
    _monitor_stop.set()
    _monitor_thread = None


def _pressure_loop() -> None:
    """While vision is pinned, demote to on-demand if free RAM collapses."""
    while not _monitor_stop.wait(_PRESSURE_INTERVAL_S):
        with _lock:
            if _policy != "pinned":
                return
            free = _available()
            if free >= _FREE_FLOOR:
                continue
            print(f"[residency] pressure free~{free / _GB:.1f}GB < floor "
                  f"{_FREE_FLOOR / _GB:.1f}GB - demoting vision to on_demand")
            _unload_vision()
            _persist("on_demand", reason="ram_pressure")
            return


def _start_monitor() -> None:
    global _monitor_thread
    _stop_monitor()
    _monitor_stop.clear()
    _monitor_thread = threading.Thread(
        target=_pressure_loop, daemon=True, name="residency-pressure",
    )
    _monitor_thread.start()


def warm_for_startup() -> dict:
    """App/API launch: pin text only. Do not load vision.

    Always ends in vision=idle so setup UI cannot hang on a partial warm.
    """
    with _lock:
        _stop_monitor()
        if is_external_provider():
            return _persist(
                "idle",
                reason="external_provider",
                provider=get_llm_config()["provider"],
                model_warm_skipped=True,
            )
        before = _available()
        print(f"[residency] startup warm (text only)  free~{before / _GB:.1f}GB")

        _state_path().write_text(
            json.dumps(
                {"status": "warming", "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                indent=2,
            ),
            encoding="utf-8",
        )

        reason = "startup_text_only"
        err = None
        try:
            try:
                _warm(TEXT_MODEL, timeout=90)
            except Exception as e:
                print(f"[residency] text warm failed: {e}")
                reason = "text_warmup_failed"
                err = str(e)

            try:
                _unload_vision()
            except Exception as e:
                print(f"[residency] vision unload skipped: {e}")

            payload = dict(
                reason=reason,
                available_before_mb=_mb(before),
            )
            if err:
                payload["error"] = err
            return _persist("idle", **payload)
        except Exception as e:
            print(f"[residency] startup warm crashed: {e}")
            return _persist(
                "idle",
                reason="startup_warm_error",
                error=str(e),
                available_before_mb=_mb(before),
            )


def on_capture_start() -> dict:
    """Screen capture turned on: pin vision if RAM allows, else on-demand."""
    with _lock:
        if is_external_provider():
            return _persist(
                "idle",
                reason="external_provider",
                provider=get_llm_config()["provider"],
                vision_warm_skipped=True,
            )
        free = _available()
        print(f"[residency] capture start  free~{free / _GB:.1f}GB")

        if _can_pin_vision(free):
            try:
                _warm(VL_MODEL, KEEP_ALIVE_PINNED)
            except Exception as e:
                print(f"[residency] vision pin failed - on_demand: {e}")
                _stop_monitor()
                return _persist("on_demand", reason="vl_warm_failed", error=str(e),
                                available_before_mb=_mb(free))

            after = _available()
            if after < _FREE_FLOOR:
                print(f"[residency] after VL pin free~{after / _GB:.1f}GB - on_demand")
                _unload_vision()
                _stop_monitor()
                return _persist("on_demand", reason="post_pin_below_floor",
                                available_before_mb=_mb(free), available_after_mb=_mb(after))

            result = _persist("pinned", reason="capture_pin",
                              available_before_mb=_mb(free), available_after_mb=_mb(after))
            _start_monitor()
            return result

        _stop_monitor()
        return _persist("on_demand", reason="insufficient_ram_to_pin",
                        available_before_mb=_mb(free))


def on_capture_stop() -> dict:
    """Screen capture turned off: unload vision and return to idle."""
    with _lock:
        print("[residency] capture stop - unloading vision")
        _stop_monitor()
        if not is_external_provider():
            _unload_vision()
        return _persist("idle", reason="capture_stop")



load_residency()


if __name__ == "__main__":
    warm_for_startup()
