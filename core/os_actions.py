"""General OS actions for Plan B (memory → act).

No app-specific integrations. Paths and URLs only for Step 1.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Literal, TypedDict
from urllib.parse import urlparse

from core.platform_support import IS_MACOS, IS_WINDOWS, get_window_metadata

# How long to keep waiting after we last saw an OS "pick an app" dialog.
_USER_GATE_SLACK_SECS = 20.0
# Hard ceiling so we never wait forever if the dialog never resolves.
# This is a temporary fix, and in future we would want the agent to automatically choose the best option and proceed.
_USER_GATE_MAX_SECS = 90.0


# General chooser / open-with UI — not tied to a specific app.
_AWAITING_TITLE_SNIPPETS = (
    "open with",
    "how do you want to open",
    "choose an app",
    "select an app",
    "keep using this",
    "look for an app",
)
_AWAITING_PROCESS_SNIPPETS = (
    "openwith",
    "pickerhost",
    "openwith.exe",
    "pickerhost.exe",
)


class ActionResult(TypedDict):
    ok: bool
    action: str
    target: str
    error: str | None


class VerifyResult(TypedDict):
    ok: bool
    status: Literal["ok", "pending", "failed"]
    matched_on: str | None
    process_name: str
    title: str
    active_url: str
    error: str | None


def _fail(action: str, target: str, error: str) -> ActionResult:
    return {"ok": False, "action": action, "target": target, "error": error}


def _succeed(action: str, target: str) -> ActionResult:
    return {"ok": True, "action": action, "target": target, "error": None}


def open_path(path: str | Path) -> ActionResult:
    """Open a local file or folder with the OS default handler."""
    action = "open_path"
    raw = str(path).strip().strip('"')
    if not raw:
        return _fail(action, raw, "empty path")

    p = Path(raw).expanduser()
    try:
        p = p.resolve(strict=False)
    except OSError as exc:
        return _fail(action, raw, f"resolve failed: {exc}")

    if not p.exists():
        return _fail(action, str(p), "path does not exist")

    target = str(p)
    try:
        if IS_WINDOWS:
            os.startfile(target)  # type: ignore[attr-defined]
        elif IS_MACOS:
            subprocess.run(["open", target], check=False)
        else:
            subprocess.run(["xdg-open", target], check=False)
    except OSError as exc:
        return _fail(action, target, str(exc))

    return _succeed(action, target)


def open_url(url: str) -> ActionResult:
    """Open an http(s) URL in the default browser."""
    action = "open_url"
    raw = (url or "").strip().strip('"')
    if not raw:
        return _fail(action, raw, "empty url")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return _fail(action, raw, f"unsupported scheme: {parsed.scheme!r}")
    if not parsed.netloc:
        return _fail(action, raw, "missing host")
    try:
        if IS_WINDOWS:
            os.startfile(raw)  # type: ignore[attr-defined]
        elif IS_MACOS:
            subprocess.run(["open", raw], check=False)
        else:
            subprocess.run(["xdg-open", raw], check=False)
    except OSError as exc:
        return _fail(action, raw, str(exc))
    return _succeed(action, raw)


def _normalize(s: str) -> str:
    return " ".join((s or "").casefold().split())


def _awaiting_user_choice(process: str, title: str) -> bool:
    """True when foreground looks like an OS open-with / app-picker dialog."""
    p = _normalize(process)
    t = _normalize(title)
    if any(s in t for s in _AWAITING_TITLE_SNIPPETS):
        return True
    if any(s in p for s in _AWAITING_PROCESS_SNIPPETS):
        return True
    return False


def _verify_snap(
    *,
    ok: bool,
    status: Literal["ok", "pending", "failed"],
    matched_on: str | None,
    process: str,
    title: str,
    url: str,
    error: str | None,
) -> VerifyResult:
    return {
        "ok": ok,
        "status": status,
        "matched_on": matched_on,
        "process_name": process,
        "title": title,
        "active_url": url,
        "error": error,
    }


def verify_foreground(
    *,
    expect_title_contains: str | None = None,
    expect_url_contains: str | None = None,
    expect_process_contains: str | None = None,
    expect_path: str | Path | None = None,
    timeout_secs: float = 5.0,
    poll_secs: float = 0.4,
) -> VerifyResult:
    """Poll foreground until an expectation matches, with patience for OS choosers.

    If Windows shows "Open with" / "How do you want to open this file?", we
    treat that as *pending* (user must pick), extend the wait, and only fail
    after a hard ceiling — not after a short impatient timeout.
    """
    expects: list[tuple[str, str]] = []
    if expect_title_contains:
        expects.append(("title", _normalize(expect_title_contains)))
    if expect_url_contains:
        expects.append(("url", _normalize(expect_url_contains)))
    if expect_process_contains:
        expects.append(("process", _normalize(expect_process_contains)))
    if expect_path is not None:
        name = _normalize(Path(expect_path).name)
        if name:
            expects.append(("path_name", name))

    if not expects:
        return _verify_snap(
            ok=False,
            status="failed",
            matched_on=None,
            process="",
            title="",
            url="",
            error="no expectations provided",
        )

    started = time.time()
    deadline = started + max(0.5, timeout_secs)
    hard_deadline = started + max(timeout_secs, _USER_GATE_MAX_SECS)
    last = {"process_name": "", "title": "", "active_url": ""}
    saw_user_gate = False

    while True:
        now = time.time()
        meta = get_window_metadata() or {}
        process = str(meta.get("process_name") or "")
        title = str(meta.get("current_window_title") or "")
        url = str(meta.get("active_url") or "")
        last = {"process_name": process, "title": title, "active_url": url}

        blob = {
            "title": _normalize(title),
            "url": _normalize(url),
            "process": _normalize(process),
        }
        path_haystack = f"{blob['title']} {blob['url']}"

        for kind, needle in expects:
            hit = (
                needle in path_haystack
                if kind == "path_name"
                else bool(needle and needle in blob[kind])
            )
            if hit:
                return _verify_snap(
                    ok=True,
                    status="ok",
                    matched_on=kind,
                    process=process,
                    title=title,
                    url=url,
                    error=None,
                )

        if _awaiting_user_choice(process, title):
            saw_user_gate = True
            # Keep giving the user time after each sighting of the chooser.
            deadline = min(hard_deadline, now + _USER_GATE_SLACK_SECS)

        if now >= hard_deadline or now >= deadline:
            break
        time.sleep(poll_secs)

    if saw_user_gate or _awaiting_user_choice(last["process_name"], last["title"]):
        return _verify_snap(
            ok=False,
            status="pending",
            matched_on=None,
            process=last["process_name"],
            title=last["title"],
            url=last["active_url"],
            error="awaiting user to pick an app in the OS open dialog",
        )

    return _verify_snap(
        ok=False,
        status="failed",
        matched_on=None,
        process=last["process_name"],
        title=last["title"],
        url=last["active_url"],
        error="foreground did not match expectations before timeout",
    )


def open_path_and_verify(path: str | Path, timeout_secs: float = 45.0) -> dict:
    """Open a path, then wait for the filename (patient across Open-with dialogs)."""
    opened = open_path(path)
    if not opened["ok"]:
        return {"open": opened, "verify": None}
    verify = verify_foreground(expect_path=path, timeout_secs=timeout_secs)
    return {"open": opened, "verify": verify}


def open_url_and_verify(url: str, timeout_secs: float = 8.0) -> dict:
    """Open a URL, then check host/path appears in foreground URL or title."""
    opened = open_url(url)
    if not opened["ok"]:
        return {"open": opened, "verify": None}
    expect = urlparse(opened["target"]).netloc or opened["target"]
    verify = verify_foreground(
        expect_url_contains=expect,
        expect_title_contains=expect,
        timeout_secs=timeout_secs,
    )
    return {"open": opened, "verify": verify}
