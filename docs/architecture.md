# Architecture

How Clippy Vision captures activity, summarizes it, distills long-term memory, and stores it locally.

Day-to-day usage is in [usage.md](usage.md). MCP tools and client setup are in [mcp.md](mcp.md).

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Desktop UI | Electron |
| Backend | Python / FastAPI / Uvicorn |
| Local LLM runtime | [Ollama](https://ollama.com) |
| Main reasoning model | User-chosen Ollama model (suggested default `qwen3:8b`) |
| Screenshot text | Accessibility APIs + RapidOCR fallback |
| Embedding model | `all-MiniLM-L6-v2`, downloaded from Hugging Face on first run (event RAG is opt-in) |
| Database | SQLite (WAL mode) |
| Screen capture | `mss`, `pywin32`, `pynput` |

There is no vision model on the capture path. Screen text comes from accessibility APIs plus OCR. Classification, session summaries, and distillation use the chat model chosen in setup.

---

## Segment 1 — Data capture

`core/screen_capture.py` runs as a background process and captures:

- Active foreground window (title, process name, active URL)
- Clipboard contents (copy and paste events)
- Context switches (window focus changes)
- Keystroke dynamics with per-app adaptive baseline
- Screenshots (taken proactively on activity bursts by `core/screenshot_scheduler.py`)

Audio and mouse streams are not captured.

Every captured event passes through a three-tier classification pipeline before being stored:

**Tier 0 — Rule-based** (deterministic, instant)
Fast rules that immediately flag obvious signals: too few keystrokes → not interesting; known background system process → not interesting; typing deviation from personal baseline → interesting (score 9).

**Tier 1 — Feature-based** (scoring)
Scoring starts at 5. Multiple features add or subtract: typing deviation, context novelty (how many times this app was seen in 7 days), typing intensity z-score, clipboard content length. A score of 4 or below is marked not interesting. A score of 7 or above is marked interesting. Scores in between go to Tier 2. The row is stored either way. The score marks what is interesting; the event is still stored.

**Tier 2 — LLM fallback**
The last 3 events plus the current event are sent to the chat model you chose in setup for context-aware classification. Output is `INTERESTING` or `NOT_INTERESTING`. Classification never queues a vision model.

### Screen text enrichment

Each captured frame records bounded text from the foreground accessibility API. Those walks run on a dedicated background worker (`core/uia_worker.py`), so a slow app does not delay the next capture. If the walk finishes after the capture timeout, the text is written beside the screenshot and copied onto the event that already points at that frame. A short first read is replaced when a longer walk of the same frame arrives.

RapidOCR runs only when that accessibility text is empty or too thin. A background processor (`core/screenshot_processor.py`) groups visually identical screenshots using perceptual hashing and stores the resulting text with the nearest event; if none exists, it creates a `screenshot_analysis` event. After the JPEG is renamed to its processed name, the event stores that name.

Image embeddings and event-level RAG are disabled by default. Screenshot expiry is checked about once an hour. The accessibility text file stays after the JPEG is deleted.

---

## Segment 2 — Summarization

A background summarizer wakes about every **60 seconds** and turns pending activity events into session summaries with the chat model you chose in setup. That interval is the check cadence, not the session length: each tick looks for unsummarized events, merges them into activity windows (events within a 10-minute gap, capped at 30 minutes), and summarizes a window that has at least one contentful event (up to 25 per summary). Typing bursts alone do not count. It runs in two passes per tick:

- **Pass 1:** Summarizes pending event windows
- **Pass 2:** Refreshes sessions when delayed screenshot text becomes available

---

## Segment 3 — Distiller

Runs every 5 sessions and extracts high-level behavioral facts from summaries. Each fact is:

1. Vector-embedded
2. Compared against existing cluster centroids (threshold: 0.75 cosine similarity)
3. Routed to the closest cluster or a new one
4. Processed with a second LLM call: **ADD / UPDATE / NOOP / CONFLICT**

Conflicting facts are preserved in `memory_conflicts` and surfaced to the agent for user resolution. User-provided corrections via `save_identity` automatically close related conflicts.

---

## Segment 4 — Database

All data lives in a local SQLite database. A source checkout uses `core/data/events.db`. An installed app uses its own data folder: `%APPDATA%\Clippy Vision\data` on Windows, and `~/Library/Application Support/Clippy Vision/data` on macOS.

| Table | Contents | Retention |
|-------|----------|-----------|
| `events` | Raw captured events | 7 days by default (1–30 in Settings) |
| `sessions` | Summaries of events | 90 days by default (1–180 in Settings) |
| `memory_clusters` | Cluster metadata | Until you delete it |
| `memory_facts` | Durable claims about the person. A pattern starts counting from the day it is confirmed | Until you delete it |
| `memory_candidates` | One sitting. Hidden from recall until a later day repeats it, or you state it as identity | Until it is saved, dropped, or you clear memory |
| `memory_conflicts` | Unresolved fact contradictions | Until you delete it |
| `memory_meta` | Settings and distiller state | Until you clear app data |
| `conversations` | Leftover rows from the removed in-app chat. The current app does not write a chat here | Until you clear app data |
| `user_profile` | User name | Until you clear app data |

FTS5 virtual tables on `events` and `sessions` enable full-text search across all stored content.

Screenshots default to 1 day; high-signal frames (interesting flag, interest score, URL present, clipboard/paste) can live up to `screenshot_retention_max_days` (default 7). OCR text on events remains after the JPEG is purged.

---

## Models

| Model | Size | Purpose |
|-------|------|---------|
| The Ollama model you pick (suggested `qwen3:8b`) | about 4.7 GB for `qwen3:8b` | Classification, session summaries, and long-term memory |
| `all-MiniLM-L6-v2` | about 90 MB, downloaded from Hugging Face on first run | Optional local semantic retrieval. It is not bundled in the installer |
