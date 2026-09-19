"""Manual test for Plan B open helpers.

Usage (PowerShell, from repo root):
  $env:PYTHONPATH = (Get-Location).Path
  python .\\scripts\\probe_os_actions.py path "$PWD\\README.md"
  python .\\scripts\\probe_os_actions.py url "https://example.com"
  python .\\scripts\\probe_os_actions.py path-verify "$PWD\\README.md"
  python .\\scripts\\probe_os_actions.py url-verify "https://example.com"

path-verify waits patiently if Windows shows an Open-with dialog — pick an
app when prompted; success when the filename appears in the foreground.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.os_actions import open_path, open_path_and_verify, open_url, open_url_and_verify


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    kind, target = sys.argv[1].lower(), sys.argv[2]
    if kind == "path":
        result = open_path(target)
    elif kind == "url":
        result = open_url(target)
    elif kind == "path-verify":
        result = open_path_and_verify(target)
    elif kind == "url-verify":
        result = open_url_and_verify(target)
    else:
        print("kind must be path | url | path-verify | url-verify")
        return 2
    print(json.dumps(result, indent=2))
    if kind.endswith("verify"):
        verify = result.get("verify") or {}
        if result.get("open", {}).get("ok") and verify.get("status") == "ok":
            return 0
        if result.get("open", {}).get("ok") and verify.get("status") == "pending":
            return 3
        return 1
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
