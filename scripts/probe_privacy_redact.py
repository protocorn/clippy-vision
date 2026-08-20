"""
Probe the privacy / Access control redaction matcher against the real
foreground window.

Every ~2 second:
  - reads the current privacy target enabled flags (same store as
    Settings -> Access control)
  - reads the foreground window's process name + title
  - prints, for every known PRIVACY_TARGETS entry, whether it's enabled and
    whether it individually matches the current window
  - prints the overall should_redact_window() verdict (what capture would
    actually do right now)
  - optionally (--rules) prints the merged active title patterns / process
    list so mismatches (e.g. Incognito windows that don't say "incognito"
    anywhere) are obvious at a glance

This does NOT change core/privacy_settings.py or capture behavior. It's a
read-only observation tool for phase 1 of the Access control redaction work
(see issue: redaction matcher is brittle for Incognito/InPrivate windows).

Usage (PowerShell):
  cd c:\\Users\\proto\\Clippy_Vision
  $env:PYTHONPATH = (Get-Location).Path
  python .\\scripts\\probe_privacy_redact.py

  # also print the merged active title-pattern / process rule set each tick
  python .\\scripts\\probe_privacy_redact.py --rules

  # poll slower/faster (default 2s)
  python .\\scripts\\probe_privacy_redact.py --interval 3

Try this while:
  - a normal (non-private) browser window is focused
  - a Chrome / Edge / Firefox Incognito / InPrivate window is focused, with
    "Incognito / Private windows" enabled in Settings -> Access control
  - WhatsApp / Discord / Slack / etc. windows, with their toggle on and off

Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.platform_support import get_window_metadata
from core.privacy_settings import (
    PRIVACY_TARGETS,
    get_active_redact_rules,
    get_privacy_enabled,
    is_clippy_window,
    should_redact_window,
)

POLL_SECS = 3


def _target_matches(target: dict, process_name: str, window_title: str) -> bool:
    """Standalone match check for a single target (mirrors should_redact_window's
    matching logic, but scoped to one target instead of the merged rule set,
    so we can show per-target hits regardless of whether it's enabled)."""
    name = (process_name or "").lower()
    title = (window_title or "").lower()
    for p in target.get("processes") or []:
        if name == p.lower():
            return True
    for pat in target.get("title_patterns") or []:
        pat = pat.lower()
        if pat and pat in title:
            return True
    return False


def _print_snapshot(show_rules: bool) -> None:
    stamp = time.strftime("%H:%M:%S")
    meta = get_window_metadata() or {}
    process_name = str(meta.get("process_name") or "")
    window_title = str(meta.get("current_window_title") or "")

    enabled = get_privacy_enabled()
    clippy = is_clippy_window(process_name, window_title)
    verdict = should_redact_window(process_name, window_title)

    print("=" * 72)
    print(f"[{stamp}] process={process_name or '(unknown)'!r}  title={window_title or '(no title)'!r}")
    if clippy:
        print("  (this is the Clippy Vision window itself -> always redacted)")
    print(f"OVERALL should_redact_window() = {verdict}")
    print("-" * 72)
    print(f"{'target':<28} {'enabled':<9} {'matches now':<12}")
    for target in PRIVACY_TARGETS:
        tid = target["id"]
        is_enabled = bool(enabled.get(tid, False))
        matches = _target_matches(target, process_name, window_title)
        flag = ""
        if matches and not is_enabled:
            flag = "  <- would match if enabled"
        elif is_enabled and not matches and verdict:
            flag = "  <- redacted via another rule"
        print(f"{tid:<28} {str(is_enabled):<9} {str(matches):<12}{flag}")

    if show_rules:
        rules = get_active_redact_rules()
        print("-" * 72)
        print(f"active processes ({len(rules['processes'])}):")
        for p in sorted(rules["processes"]):
            print(f"  - {p}")
        print(f"active title patterns ({len(rules['title_patterns'])}):")
        for pat in rules["title_patterns"]:
            print(f"  - {pat!r}")

    print("=" * 72)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval",
        type=float,
        default=POLL_SECS,
        help=f"seconds between polls (default {POLL_SECS})",
    )
    parser.add_argument(
        "--rules",
        action="store_true",
        help="also print the merged active process/title rule set each tick",
    )
    args = parser.parse_args()

    print("Privacy redact probe — Ctrl+C to stop")
    print(f"Polling every {args.interval}s")
    print(f"Known targets: {', '.join(t['id'] for t in PRIVACY_TARGETS)}")
    print()

    while True:
        _print_snapshot(show_rules=args.rules)
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
