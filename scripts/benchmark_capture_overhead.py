r"""Measure Clippy Vision with capture off and on.

PowerShell:
    python .\scripts\benchmark_capture_overhead.py --seconds 60

Restart Clippy Vision after instrumentation changes before running this script.
The script never toggles capture itself; it asks the user to use the app so the
measurement follows the same path as normal use.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psutil

from core.capture_state import get_capture_status
from core.paths import get_data_dir
from core.performance_metrics import get_performance_snapshot


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 2) if values else 0.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2)


def _wait_for_capture(expected: bool, timeout_seconds: float = 60.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if bool(get_capture_status().get("active")) is expected:
            return
        time.sleep(0.5)
    state = "on" if expected else "off"
    raise RuntimeError(f"Capture did not become {state} within {timeout_seconds:.0f}s")


def _measure_phase(name: str, seconds: int) -> dict:
    expected_capture = name == "capture_on"
    _wait_for_capture(expected_capture)
    print(f"\nMeasuring {name.replace('_', ' ')} for {seconds}s. Use the computer normally.")

    psutil.cpu_percent(None)
    disk_start = psutil.disk_io_counters()
    cpu_samples: list[float] = []
    ram_samples: list[float] = []

    for elapsed in range(seconds):
        cpu_samples.append(psutil.cpu_percent(interval=1.0))
        ram_samples.append(psutil.virtual_memory().percent)
        if (elapsed + 1) % 10 == 0 or elapsed + 1 == seconds:
            print(f"  {elapsed + 1}/{seconds}s")

    disk_end = psutil.disk_io_counters()
    disk_delta = {}
    if disk_start and disk_end:
        disk_delta = {
            "read_mb": round((disk_end.read_bytes - disk_start.read_bytes) / (1024 * 1024), 2),
            "write_mb": round((disk_end.write_bytes - disk_start.write_bytes) / (1024 * 1024), 2),
        }

    return {
        "phase": name,
        "duration_seconds": seconds,
        "capture_status": get_capture_status(),
        "system": {
            "cpu_avg_percent": _mean(cpu_samples),
            "cpu_p95_percent": _p95(cpu_samples),
            "ram_avg_percent": _mean(ram_samples),
            **disk_delta,
        },
        "clippy_processes": get_performance_snapshot(),
    }


def _stage_total(phase: dict, name: str) -> float:
    total = 0.0
    for process in phase["clippy_processes"].get("processes", []):
        total += float(process.get("timings", {}).get(name, {}).get("total_ms_1m", 0))
    return round(total, 2)


def _counter_total(phase: dict, name: str) -> int:
    total = 0
    for process in phase["clippy_processes"].get("processes", []):
        total += int(process.get("counters", {}).get(name, {}).get("count_1m", 0))
    return total


def _summary(off: dict, on: dict) -> dict:
    stages = [
        "screenshot.grab",
        "screenshot.redaction",
        "screenshot.hash",
        "screenshot.jpeg_write",
        "screenshot.uia_bounds",
        "screenshot.uia_text",
        "ocr.crop_inference",
        "ocr.full_inference",
        "processor.hash_backlog",
        "processor.group_backlog",
        "database.event_insert",
        "database.event_commit",
        "database.vision_update",
        "database.vision_commit",
    ]
    return {
        "system_cpu_delta_avg_percent": round(
            on["system"]["cpu_avg_percent"] - off["system"]["cpu_avg_percent"], 2
        ),
        "system_ram_delta_avg_percent": round(
            on["system"]["ram_avg_percent"] - off["system"]["ram_avg_percent"], 2
        ),
        "screenshots_captured_per_minute": _counter_total(on, "screenshots.captured"),
        "screenshots_deduplicated_per_minute": _counter_total(on, "screenshots.deduplicated"),
        "stage_total_ms_last_minute": {
            stage: {"capture_off": _stage_total(off, stage), "capture_on": _stage_total(on, stage)}
            for stage in stages
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=60, help="Seconds per phase (default: 60)")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    seconds = max(15, int(args.seconds))

    print("Clippy Vision capture overhead benchmark")
    print("Restart the app first so both Python processes load the instrumentation.")
    input("\nTurn capture OFF in Clippy Vision, then press Enter here...")
    off = _measure_phase("capture_off", seconds)

    input("\nTurn capture ON, then press Enter here...")
    on = _measure_phase("capture_on", seconds)

    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "seconds_per_phase": seconds,
        "capture_off": off,
        "capture_on": on,
        "comparison": _summary(off, on),
    }
    output = args.output or (
        get_data_dir() / "performance" / f"capture-baseline-{datetime.now():%Y%m%d-%H%M%S}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    comparison = report["comparison"]
    print("\nBaseline complete")
    print(f"  Average system CPU delta: {comparison['system_cpu_delta_avg_percent']:+.2f}%")
    print(f"  Average system RAM delta: {comparison['system_ram_delta_avg_percent']:+.2f}%")
    print(f"  Screenshots captured/min: {comparison['screenshots_captured_per_minute']}")
    print(f"  Screenshots deduplicated/min: {comparison['screenshots_deduplicated_per_minute']}")
    print(f"  Full report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
