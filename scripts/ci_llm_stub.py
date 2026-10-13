#!/usr/bin/env python3
"""Minimal LLM stand-in for release dry-run CI.

The packaged app's preflight treats a non-11434 base_url as an external
Ollama-compatible API (see usesManagedOllama() in
electron-ui/electron/lib/llm-config.js). This process answers the two
probes that preflight makes so CI can spawn the real Python backend
without installing Ollama or pulling a chat model:

  GET /          — ollamaReachable()
  GET /api/tags  — ollamaHasModel() for the seeded chat_model

This does not emulate inference, embeddings, or the interactive setup wizard.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 11555
CHAT_MODEL = "test-model"


class StubHandler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path in ("/api/tags", "/api/tags/"):
            self._send_json({"models": [{"name": CHAT_MODEL, "model": CHAT_MODEL}]})
            return
        self._send_json({"status": "ok"})

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), StubHandler)
    print(f"ci-llm-stub listening on http://{HOST}:{PORT}/", flush=True)
    server.serve_forever()
