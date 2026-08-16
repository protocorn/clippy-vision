# Clippy Vision

> **A fully local AI assistant that watches your work to build context automatically without needing to explain much to an LLM. 100% private - no cloud, no data leakage.**

![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Models](https://img.shields.io/badge/models-Ollama%20local-orange)
[![All Contributors](https://img.shields.io/github/all-contributors/protocorn/clippy-vision?color=ee8449&style=flat-square)](#contributors)
[![Open Source Helpers](https://www.codetriage.com/protocorn/clippy-vision/badges/users.svg)](https://www.codetriage.com/protocorn/clippy-vision)

<p align="center">
  <img src="assets/clippy-vision-demo.gif" alt="Clippy Vision demo" width="720" />
</p>

---

## What is Clippy Vision?

Clippy Vision is a desktop AI companion that passively observes your work - active windows, clipboard, typing patterns, and screenshots - and builds a continuously updating memory of everything you do. When you open the chat, it already knows your context. No copy-pasting. No re-explaining.

Everything runs entirely on your machine. No API keys, no cloud, no data leaving your device.

---

## One memory across every app you work in

Your work is not stored in one place. It is spread across the browser, your IDE, local PDFs, terminal output, chat apps, notes files, spreadsheets, and design tools. Each of those keeps its own partial record, or none at all, and none of them know about each other.

Clippy watches all of them and keeps one timeline. Two things follow from that, and neither is possible from any single app's own history:

1. **You can search what was on the screen, not just what things were called.** Titles and filenames are usually useless later. A paper saved as `2103.00020v1.pdf`, a Jira ticket referred to only by its ID, a config you edited in a nameless scratch buffer. Clippy read the content, so the words that were actually in front of you are what you search.
2. **You can reconstruct a whole stretch of work, not look up one artifact.** "What was I doing Tuesday afternoon" spans the paper you read, the file you edited, the snippet you copied, and the conversation you had about it. Clippy answers that as a summary of the work. Every per-app history hands you a list and leaves the reconstruction to you.

## How Clippy Vision fits with Claude / ChatGPT

Claude and ChatGPT are built for **reasoning, writing, and general knowledge**. They are excellent when you bring them context. They are not built to know what was on your screen yesterday without you telling them.

Clippy Vision is built for the **context problem**. It watches your work, remembers it, and answers from that memory. It does not replace Claude or ChatGPT. It fills the gap they cannot: your personal activity history.

| | Per-app history (browser, recent files) | Claude / ChatGPT | Clippy Vision |
|--|--|--|--|
| Sees | Names and timestamps, one app at a time | Whatever you paste or upload | Screen content across every app |
| Answers with | A list to scan | Its general knowledge | What you were actually doing |
| Needs you to reconstruct context | Yes | Yes | No - already saw it |
| Runs where | Local | Cloud | 100% on your machine |
| Best for | "Which tab or file did I open?" | "Help me solve / write / explain this" | "What was I doing / reading / debugging?" |

Use Clippy when you need your own work history back. Use Claude or ChatGPT when you need a strong reasoning partner. Many people use both: Clippy to reconstruct context, then paste that into Claude to go deeper.

<p align="center">
  <img src="assets/demo-product.png" alt="Clippy Vision reconstructing research across apps and files" width="720" />
</p>

<p align="center"><em>One question. Answer pulled from papers, chat tools, and a local notes file from the same research stretch.</em></p>

<p align="center">
  <img src="assets/demo-vs-claude-urls.png" alt="Clippy Vision vs Claude on a personal activity question" width="720" />
</p>

<p align="center"><em>Same kind of personal question. Clippy answers from activity it saw on your machine. Claude has no record of that work, because it never saw it.</em></p>

---

## Download

Click your platform to download **v1.2.0** directly:

<p align="center">
  <a href="https://github.com/protocorn/clippy-vision/releases/download/v1.2.0/ClippyVision-Windows-Setup-1.2.0.exe"><img src="https://img.shields.io/badge/Download-Windows%20v1.2.0-0078D6?style=for-the-badge&logo=windows&logoColor=white" alt="Download for Windows v1.2.0" /></a>
  &nbsp;
  <a href="https://github.com/protocorn/clippy-vision/releases/download/v1.2.0/ClippyVision-macOS-arm64-1.2.0.dmg"><img src="https://img.shields.io/badge/Download-macOS%20Apple%20Silicon%20v1.2.0-000000?style=for-the-badge&logo=apple&logoColor=white" alt="Download for macOS Apple Silicon v1.2.0" /></a>
  &nbsp;
  <a href="https://github.com/protocorn/clippy-vision/releases/download/v1.2.0/ClippyVision-macOS-x64-1.2.0.dmg"><img src="https://img.shields.io/badge/Download-macOS%20Intel%20v1.2.0-555555?style=for-the-badge&logo=apple&logoColor=white" alt="Download for macOS Intel v1.2.0" /></a>
</p>

<p align="center">
  <a href="https://github.com/protocorn/clippy-vision/releases/latest">All releases &amp; older versions</a>
  &nbsp;·&nbsp;
  <a href="https://github.com/protocorn/clippy-vision/commits/main"><img src="https://img.shields.io/github/last-commit/protocorn/clippy-vision?style=flat-square&label=last%20commit" alt="Last commit" /></a>
  &nbsp;
  <a href="https://github.com/protocorn/clippy-vision/releases/latest"><img src="https://img.shields.io/github/release-date/protocorn/clippy-vision?style=flat-square&label=latest%20release" alt="Latest release date" /></a>
</p>

The installer includes a setup wizard that handles Python, Ollama, and all required models automatically. No terminal required.

Clippy Vision is under active development. Three releases shipped in the first two weeks, including full macOS support, and bug reports usually get a reply the same day.

### System requirements

Clippy Vision runs a local text model for chat and uses accessibility APIs plus OCR for screen capture (no vision model in the capture path).

| | Minimum | Recommended |
|--|---------|-------------|
| OS | Windows 10 / 11 (64-bit) | Windows 11 |
| System RAM | 8 GB | 16 GB |
| GPU VRAM | Not required (integrated OK) | 4 GB+ dedicated |
| Free disk | 8 GB | 10 GB+ |

- **First run** needs internet once for the text model (`qwen3:8b`, ~4.7 GB).
- The setup wizard **checks your PC** against these numbers before installing. Below minimum → setup is blocked. Between minimum and recommended → you can continue with a warning that chat may feel slower.
- Integrated / shared GPUs are allowed at minimum; a dedicated GPU still helps chat speed.
- **Lower-spec / contributor machines:** capture uses accessibility + OCR (no vision model in setup). Only the chat model downloads by default; pick a smaller chat model in setup if needed. Details in [CONTRIBUTING.md](CONTRIBUTING.md#lower-spec-machines).

---


## Quick Start

### Option A - Installer (recommended)

1. Use the [Download](#download) buttons above (Windows, macOS Apple Silicon, or macOS Intel)
2. Follow the setup wizard (installs Python, Ollama, and AI models)
3. Launch from Start Menu → Clippy Vision

### Option B - Run from source

```powershell
git clone https://github.com/protocorn/clippy-vision.git
cd clippy-vision\electron-ui
npm install
npm start
```

The app will open the setup wizard on first launch and walk you through dependencies.

---

## Features

- **Passive screen awareness** - captures foreground windows, clipboard, typing bursts, and screenshots in the background
- **Privacy-first redaction** - Clippy Vision's own window is blacked out in every screenshot before the AI ever sees it
- **Three-tier event classification** - rule-based → feature-based → LLM fallback, so only meaningful events are stored
- **Low-cost screen text** - accessibility/UI text first with RapidOCR fallback; no vision model in capture
- **Hierarchical memory** - events → session summaries → distilled long-term facts; memory never resets
- **Smart query router** - a fine-tuned MiniLM classifier routes every question to the right retrieval strategy before the LLM is even called
- **ReAct agent** - structured reasoning with tools: SQL generation, memory recall, fact saving
- **Conversation memory** - rolling summaries + semantic search over past conversations
- **Toggle capture** - start/stop data capture from the tray icon or the in-app button, with a desktop notification on change
- **Per-app redaction (in progress)** - backend rules exist for WhatsApp, Telegram, incognito windows, and similar targets; reliable matching outside Clippy's own window is still being improved, so capture on/off is the dependable privacy switch today

---

## Where this is going

Clippy is reactive today: you ask, it answers. The next bet is making it proactive, so it can act on what it sees instead of waiting to be asked. Capture now reads window text through accessibility APIs and falls back to local OCR without loading a vision model. A timeline view remains another priority so you can see and delete exactly what was captured.

No dates attached to any of it. [PROJECT_VISION.md](PROJECT_VISION.md) has the current thinking, the priority order, and an honest list of what does not work yet. If you want to shape any of it, the [open issues](https://github.com/protocorn/clippy-vision/issues) are the place to start.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Desktop UI | Electron |
| Backend | Python / FastAPI / Uvicorn |
| Local LLM runtime | [Ollama](https://ollama.com) |
| Main reasoning model | `qwen3:8b` |
| Screenshot text | Accessibility APIs + RapidOCR fallback |
| Embedding model | Bundled `all-MiniLM-L6-v2` (event RAG is opt-in) |
| Query classifier | Fine-tuned MiniLM-L3 |
| Database | SQLite (WAL mode) |
| Screen capture | `mss`, `pywin32`, `pynput` |

---

## Architecture

### Segment 1 - Data Capture

`core/screen_capture.py` runs as a background process and captures:

- Active foreground window (title, process name, active URL)
- Clipboard contents (copy and paste events)
- Context switches (window focus changes)
- Keystroke dynamics with per-app adaptive baseline
- Screenshots (taken proactively on activity bursts)

Every captured event passes through a three-tier classification pipeline before being stored:

**Tier 0 - Rule-based** (deterministic, instant)
Fast rules that immediately flag obvious signals: too few keystrokes → not interesting; known background system process → not interesting; typing deviation from personal baseline → interesting (score 9).

**Tier 1 - Feature-based** (scoring)
Scoring starts at 5. Multiple features add or subtract: typing deviation, context novelty (how many times this app was seen in 7 days), typing intensity z-score, clipboard content length. Events below 4 are dropped; above 7 are kept; 4-7 go to Tier 2.

**Tier 2 - LLM fallback**
The last 3 events + current event are sent to `qwen3:8b` for context-aware classification. Output is `INTERESTING` or `NOT_INTERESTING`; classification never queues a vision model.

**Screen text enrichment**
Each captured frame records bounded text from the foreground accessibility/UI API. RapidOCR runs only when that text is empty or too sparse. A background processor (`core/screenshot_processor.py`) groups visually identical screenshots using perceptual hashing and stores the resulting text with the nearest event (±10 s); if none exists, it creates a `screenshot_analysis` event. Image embeddings and event-level RAG are disabled by default.

---

### Segment 2 - Summarization

A background summarizer runs every 5 minutes and groups recent interesting events into session summaries using `qwen3:8b`. It runs in two passes per tick:

- **Pass 1:** Summarizes pending events immediately
- **Pass 2:** Refreshes sessions when delayed screenshot text becomes available

---

### Segment 3 - Distiller

Runs every 5 sessions and extracts high-level behavioral facts from summaries. Each fact is:
1. Vector-embedded
2. Compared against existing cluster centroids (threshold: 0.75 cosine similarity)
3. Routed to the closest cluster or a new one
4. Processed with a second LLM call: **ADD / UPDATE / NOOP / CONFLICT**

Conflicting facts are preserved in `memory_conflicts` and surfaced to the agent for user resolution. User-provided corrections via `save_identity` automatically close related conflicts.

---

### Segment 4 - Query Router

A fine-tuned **MiniLM-L3** classifier (`agent/router.py`) maps every incoming query to one of:

| Category | What it covers |
|----------|---------------|
| `time_anchored` | "What was I doing yesterday at 3 PM?" |
| `topic_search` | "What did I work on related to Clippy?" |
| `specific_recall` | "What URL was I reading this morning?" |
| `memory_query` | Questions about facts Clippy has memorized |
| `casual` | General chat, no retrieval needed |

Each category has a dedicated prefetch module. Context is retrieved in parallel before the LLM is called, so the agent already has relevant data in its prompt without needing to make tool calls reactively.

---

### Segment 5 - The Agent

A **ReAct agent** (`agent/react_agent.py`) with function calling. Tools available:

| Tool | Description |
|------|-------------|
| `search_sessions` | SQL queries against the sessions/summaries table |
| `search_events` | SQL queries against the raw events table |
| `recall_memory` | Lists all memory cluster labels |
| `fetch_cluster` | Fetches facts from a specific cluster |
| `save_identity` | Saves autobiographical details |
| `save_note` | Saves explicit things the user wants remembered |

Prompt components: conversation history (last 8 turns + rolling summaries), user profile, top-8 memory facts by semantic similarity, and prefetched context from the router.

---

### Segment 6 - Database

All data lives in a local SQLite database (`core/data/events.db`):

| Table | Contents | Retention |
|-------|----------|-----------|
| `events` | Raw captured events | 7 days |
| `sessions` | Summaries of events | 90 days |
| `memory_clusters` | Cluster metadata | Permanent |
| `memory_facts` | Individual long-term facts | Permanent |
| `memory_conflicts` | Unresolved fact contradictions | Permanent |
| `memory_meta` | Settings and distiller state | Permanent |
| `conversations` | Full conversation history | Permanent |
| `user_profile` | User name | Permanent |

FTS5 virtual tables on `events` and `sessions` enable full-text search across all stored content.

---

## Privacy

- All processing is local. Nothing leaves your machine.
- Clippy Vision's own window is blacked out in screenshots before any AI model sees them.
- You can toggle data capture on/off at any time from the tray icon.
- Per-app redaction is in progress for WhatsApp, Telegram, Signal, incognito windows, and similar targets. Matching is not reliable enough yet outside Clippy's own window, so capture on/off is the dependable privacy switch today.
- Captured data has TTLs: raw events expire after 7 days, session summaries after 90 days.
- The local API binds to `127.0.0.1` on a port chosen at launch, so it is never reachable from your network.

**The one outbound request:** Clippy Vision checks the public GitHub releases page for a newer version, at most once every 12 hours. It sends no chat, screen, profile, or account data — only the request itself, like opening the releases page in a browser. Turn it off any time under **Settings → Updates**.

---

## Building from Source

```powershell
# Python dependencies
pip install -r requirements.txt

# Run the desktop app
cd electron-ui
npm install
npm start

# Build the Windows installer
npm run dist
```

The built installer appears at `electron-ui/dist/ClippyVision-Windows-Setup-{version}.exe` (or `ClippyVision-macOS-{arch}-{version}.dmg` when building on macOS).

---

## License

MIT - see [LICENSE](LICENSE) for details.

---

## Contributors

Every feature in Clippy Vision has a person behind it. This wall is how we say thank you - by name, with what they actually built, backed by real numbers from git history.

[![All Contributors](https://img.shields.io/github/all-contributors/protocorn/clippy-vision?color=ee8449&style=flat-square)](#contributors)
[![Contributors](https://img.shields.io/github/contributors/protocorn/clippy-vision?style=flat-square)](https://github.com/protocorn/clippy-vision/graphs/contributors)

### Hall of fame

<!-- CONTRIBUTORS-STATS:START -->

| | Contributor | What they built | Commits | Lines |
| :---: | :--- | :--- | ---: | :---: |
| <a href="https://github.com/protocorn"><img src="https://avatars.githubusercontent.com/u/53559317?v=4" width="64" height="64" alt="protocorn"/></a> | <a href="https://github.com/protocorn"><b>@protocorn</b></a><br/><sub>💻 📖 🎨 🤔 🚧</sub> | Designed the core app: agent, vision pipeline, memory system, and the Electron desktop shell. | 84 | +91,430&nbsp;/&nbsp;−2,271 |
| <a href="https://github.com/rusetiq"><img src="https://avatars.githubusercontent.com/u/234747645?v=4" width="64" height="64" alt="rusetiq"/></a> | <a href="https://github.com/rusetiq"><b>@rusetiq</b></a><br/><sub>💻 📦</sub> | Brought Clippy Vision to macOS: native screen capture, permissions, and Apple Silicon + Intel packaging. | 8 | +39,032&nbsp;/&nbsp;−3,226 |
| <a href="https://github.com/ABarpanda"><img src="https://avatars.githubusercontent.com/u/145291762?v=4" width="64" height="64" alt="ABarpanda"/></a> | <a href="https://github.com/ABarpanda"><b>@ABarpanda</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=ABarpanda">See their commits →</a> | 4 | +278&nbsp;/&nbsp;−217 |
| <a href="https://github.com/vaishn4vi"><img src="https://avatars.githubusercontent.com/u/150888364?v=4" width="64" height="64" alt="vaishn4vi"/></a> | <a href="https://github.com/vaishn4vi"><b>@vaishn4vi</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=vaishn4vi">See their commits →</a> | 2 | +120&nbsp;/&nbsp;−41 |
| <a href="https://github.com/shaurya703"><img src="https://avatars.githubusercontent.com/u/153742516?v=4" width="64" height="64" alt="shaurya703"/></a> | <a href="https://github.com/shaurya703"><b>@shaurya703</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=shaurya703">See their commits →</a> | 1 | +49&nbsp;/&nbsp;−0 |
| <a href="https://github.com/cyforkk"><img src="https://avatars.githubusercontent.com/u/165913369?v=4" width="64" height="64" alt="cyforkk"/></a> | <a href="https://github.com/cyforkk"><b>@cyforkk</b></a><br/><sub>💻</sub> | Made errors readable: replaced bare HTTP status codes with real API error messages in chat. | 1 | +32&nbsp;/&nbsp;−11 |
| <a href="https://github.com/icn5381"><img src="https://github.com/icn5381.png" width="64" height="64" alt="icn5381"/></a> | <a href="https://github.com/icn5381"><b>@icn5381</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=icn5381">See their commits →</a> | 1 | +15&nbsp;/&nbsp;−4 |

<sub>Numbers come straight from git history and refresh automatically on every push to <code>main</code>.</sub>
<!-- CONTRIBUTORS-STATS:END -->

### How to get on this wall

Code is one way in, but not the only one - we follow the [All Contributors](https://allcontributors.org/) spec, so a sharp bug report, a design suggestion that sticks, or a doc fix all count: 💻 `code` · 📦 `platform` · 📖 `doc` · 🐛 `bug` · 🤔 `ideas` · 🎨 `design` · ⚠️ `test` · 👀 `review` · 🚧 `maintenance`

When your contribution lands, comment this on the PR or issue and the bot handles the rest:

```text
@all-contributors please add @your-username for code, doc
```

New here? [CONTRIBUTING.md](CONTRIBUTING.md) has setup steps and a list of good first issues.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup steps and good first issues, and [PROJECT_VISION.md](PROJECT_VISION.md) for what the project is optimizing for and where it is headed.
