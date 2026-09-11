"""Low-overhead, local-only performance metrics for capture diagnostics.

Each Python process publishes one small rolling snapshot.  No raw captured
content is recorded, files are bounded, and stale process files are ignored.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from core.paths import get_data_dir

_WINDOW_SECONDS = 60.0
_STALE_SECONDS = 30.0
_DELETE_AFTER_SECONDS = 24 * 60 * 60
_lock = threading.Lock()
_role = "unknown"
_started = False
_timings: dict[str, deque[tuple[float, float]]] = defaultdict(deque)
_counter_events: dict[str, deque[float]] = defaultdict(deque)
_counter_totals: dict[str, int] = defaultdict(int)
_gauges: dict[str, float] = {}
_resource_samples: deque[dict[str, float]] = deque()


def _metrics_dir() -> Path:
    path = get_data_dir() / "performance"
    path.mkdir(parents=True, exist_ok=True)
    return path


def observe_timing(name: str, elapsed_ms: float) -> None:
    now = time.time()
    with _lock:
        _timings[name].append((now, max(0.0, float(elapsed_ms))))


@contextmanager
def timed(name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        observe_timing(name, (time.perf_counter() - started) * 1000.0)


def increment(name: str, amount: int = 1) -> None:
    now = time.time()
    amount = max(0, int(amount))
    with _lock:
        _counter_totals[name] += amount
        _counter_events[name].extend([now] * amount)


def set_gauge(name: str, value: float) -> None:
    with _lock:
        _gauges[name] = float(value)


def _rolling_snapshot(now: float) -> tuple[dict, dict, dict]:
    cutoff = now - _WINDOW_SECONDS
    timing_output: dict[str, dict] = {}
    counter_output: dict[str, dict] = {}
    with _lock:
        for name, values in _timings.items():
            while values and values[0][0] < cutoff:
                values.popleft()
            durations = [value for _, value in values]
            if durations:
                timing_output[name] = {
                    "count_1m": len(durations),
                    "avg_ms_1m": round(sum(durations) / len(durations), 3),
                    "max_ms_1m": round(max(durations), 3),
                    "total_ms_1m": round(sum(durations), 3),
                }
        for name, values in _counter_events.items():
            while values and values[0] < cutoff:
                values.popleft()
            counter_output[name] = {
                "count_1m": len(values),
                "total": _counter_totals[name],
            }
        gauges = {name: round(value, 3) for name, value in _gauges.items()}
    return timing_output, counter_output, gauges


def _publish(process, process_cpu_percent: float) -> None:
    import psutil

    now = time.time()
    timings, counters, gauges = _rolling_snapshot(now)
    memory = process.memory_info()
    try:
        io = process.io_counters()
        io_data = {"read_bytes": io.read_bytes, "write_bytes": io.write_bytes}
    except (AttributeError, OSError):
        io_data = {}
    current_resources = {
        "process_cpu_percent": float(process_cpu_percent),
        "rss_mb": memory.rss / (1024 * 1024),
        "system_cpu_percent": float(psutil.cpu_percent(None)),
        "system_ram_percent": float(psutil.virtual_memory().percent),
        **{name: float(value) for name, value in io_data.items()},
    }
    with _lock:
        _resource_samples.append({"timestamp": now, **current_resources})
        cutoff = now - _WINDOW_SECONDS
        while _resource_samples and _resource_samples[0]["timestamp"] < cutoff:
            _resource_samples.popleft()
        samples = list(_resource_samples)

    rolling: dict[str, dict] = {}
    for name in ("process_cpu_percent", "rss_mb", "system_cpu_percent", "system_ram_percent"):
        values = [sample[name] for sample in samples]
        ordered = sorted(values)
        rolling[name] = {
            "avg": round(sum(values) / len(values), 2),
            "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
            "max": round(max(values), 2),
        }
    if len(samples) >= 2 and "read_bytes" in samples[0] and "read_bytes" in samples[-1]:
        rolling["process_io_mb"] = {
            "read": round((samples[-1]["read_bytes"] - samples[0]["read_bytes"]) / (1024 * 1024), 3),
            "write": round((samples[-1]["write_bytes"] - samples[0]["write_bytes"]) / (1024 * 1024), 3),
        }
    payload = {
        "version": 1,
        "role": _role,
        "pid": os.getpid(),
        "updated_at": now,
        "window_seconds": _WINDOW_SECONDS,
        "resources": {
            "current": {
                name: round(value, 2) for name, value in current_resources.items()
            },
            "rolling_1m": rolling,
            **io_data,
        },
        "timings": timings,
        "counters": counters,
        "gauges": gauges,
    }
    target = _metrics_dir() / f"{_role}-{os.getpid()}.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(target)


def _sampler_loop(interval_seconds: float) -> None:
    import psutil

    process = psutil.Process()
    process.cpu_percent(None)
    psutil.cpu_percent(None)
    while True:
        time.sleep(interval_seconds)
        try:
            _publish(process, process.cpu_percent(None))
        except (OSError, psutil.Error) as exc:
            print(f"[performance] snapshot skipped: {exc}")


def _delete_old_snapshots() -> None:
    cutoff = time.time() - _DELETE_AFTER_SECONDS
    for path in _metrics_dir().glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def start_performance_monitor(role: str, interval_seconds: float = 5.0) -> None:
    global _role, _started
    with _lock:
        _role = str(role or "unknown")
        if _started:
            return
        _started = True
    _delete_old_snapshots()
    thread = threading.Thread(
        target=_sampler_loop,
        args=(max(1.0, float(interval_seconds)),),
        daemon=True,
        name=f"performance-{_role}",
    )
    thread.start()


def load_backoff_multiplier() -> float:
    """How much longer a background loop should sleep between iterations,
    based on the last minute of system CPU load already captured in every
    process's own performance snapshot. This module has always collected
    that data; until now nothing ever read it back to change behavior, so
    a sustained-high-load machine got exactly the same background capture
    and processor cadence as an idle one.

    Deliberately CPU-only, not RAM% — system_ram_percent is mostly harmless
    OS file-cache/standby pages and is not a reliable "the machine is
    struggling" signal (see the same reasoning in core/model_residency.py's
    _under_pressure(), which dropped a RAM%-based gate for this exact
    reason). A machine that idles at 90%+ RAM used would otherwise get its
    background interval stretched 3x for no real reason, every single time.

    1.0 = no change. Higher only kicks in once CPU load has been high for a
    sustained window (rolling 1-minute average), not on a single noisy
    sample, and only affects periodic/idle-triggered work — never the
    user-activity-triggered capture path, which must stay responsive.
    """
    try:
        snapshot = get_performance_snapshot()
    except Exception:
        return 1.0
    cpu_values: list[float] = []
    for process in snapshot.get("processes") or []:
        rolling = ((process.get("resources") or {}).get("rolling_1m")) or {}
        cpu = (rolling.get("system_cpu_percent") or {}).get("avg")
        if isinstance(cpu, (int, float)):
            cpu_values.append(float(cpu))
    # Every process samples the same system-wide counter independently; take
    # the max across them as the most conservative read of "how loaded has
    # the machine actually been," rather than diluting a real spike with a
    # process that happened to sample a moment later.
    pressure = max(cpu_values, default=0.0)
    if pressure >= 90.0:
        return 3.0
    if pressure >= 80.0:
        return 2.0
    if pressure >= 70.0:
        return 1.5
    return 1.0


def get_performance_snapshot() -> dict:
    """Return current per-process snapshots for diagnostics and benchmarks."""
    now = time.time()
    processes = []
    for path in _metrics_dir().glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            age = now - float(payload.get("updated_at") or 0)
            if age <= _STALE_SECONDS:
                payload["age_seconds"] = round(max(0.0, age), 2)
                processes.append(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    processes.sort(key=lambda item: (str(item.get("role")), int(item.get("pid") or 0)))
    return {"window_seconds": _WINDOW_SECONDS, "processes": processes}
