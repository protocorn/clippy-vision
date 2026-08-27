"""Shared Ollama HTTP transport with readable failures.

Ollama returns real explanations in the body of a 500 response ("model requires
more system memory...", "error loading model..."). ``urllib`` only exposes the
status line, so every caller used to log a bare "HTTP Error 500: Internal Server
Error" with no cause. Every request goes through this module so the body is read
once and attached to the raised exception, and so an unreachable server is a
distinct, self-explanatory condition instead of a generic OSError.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:11434"


# OLLAMA_HOST commonly holds a *bind* address such as 0.0.0.0 or [::]. Those are
# not connectable destinations, so they are rewritten to loopback for clients.
_WILDCARD_HOSTS = ("0.0.0.0", "::", "[::]", "*")


def base_url() -> str:
    """Ollama endpoint, overridable for non-default installs."""
    raw = (os.environ.get("CLIPPY_OLLAMA_URL") or os.environ.get("OLLAMA_HOST") or "").strip()
    if not raw:
        return DEFAULT_BASE_URL
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    scheme, _, remainder = raw.partition("://")
    authority = remainder.split("/", 1)[0]
    host, sep, port = authority.rpartition(":")
    if not sep or not port.isdigit():  # no explicit port
        host, port = authority, ""
    if host.strip().lower() in _WILDCARD_HOSTS:
        host = "127.0.0.1"
        return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}:11434"
    return raw.rstrip("/")


class OllamaError(OSError):
    """Ollama answered, but rejected the request. Carries the server's message."""


class OllamaUnavailable(OllamaError):
    """No Ollama server is reachable (not started, or still starting up)."""


def _extract_message(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return text[:400]
    if isinstance(parsed, dict):
        message = parsed.get("error") or parsed.get("message") or ""
        if isinstance(message, dict):
            message = message.get("message", "")
        return str(message or text)[:400]
    return text[:400]


def describe_http_error(err: urllib.error.HTTPError) -> str:
    """Turn an HTTPError into 'HTTP 500: <what Ollama actually said>'."""
    try:
        detail = _extract_message(err.read())
    except Exception:
        detail = ""
    if not detail:
        detail = err.reason if isinstance(err.reason, str) else str(err.reason or "no detail")
    return f"HTTP {err.code}: {detail}"


def request(path: str, body: dict | None = None, *, timeout: float = 90, method: str | None = None):
    """Open an Ollama request, raising OllamaError/OllamaUnavailable with context."""
    url = f"{base_url()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method or ("POST" if data else "GET"),
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as err:
        raise OllamaError(f"ollama {path} failed — {describe_http_error(err)}") from err
    except urllib.error.URLError as err:
        raise OllamaUnavailable(
            f"ollama not reachable at {base_url()} ({err.reason}) — is the Ollama server running?"
        ) from err
    except (TimeoutError, ConnectionError) as err:
        raise OllamaUnavailable(f"ollama not reachable at {base_url()} ({err})") from err


def post_json(path: str, body: dict, *, timeout: float = 90) -> dict:
    with request(path, body, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}


def get_json(path: str, *, timeout: float = 5) -> dict:
    with request(path, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}


def server_reachable(timeout: float = 2.0) -> bool:
    try:
        get_json("/api/version", timeout=timeout)
        return True
    except OllamaError:
        return False


def wait_until_reachable(max_wait: float = 30.0, interval: float = 1.0) -> bool:
    """Give a server that is still booting a chance before declaring failure."""
    deadline = time.monotonic() + max(0.0, max_wait)
    while True:
        if server_reachable():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
