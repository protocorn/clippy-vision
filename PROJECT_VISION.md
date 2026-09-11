# Clippy Vision's Vision
The aim of this project is to build a complete screen watcher tool, one that sees what's on your screen (windows, clipboard, typing activity, screenshots), stores it in memory, remembers it, and retrieves or acts on it when needed.
To build trust with users, this project is kept 100% local and open source.

This doc exists so contributors know what we're optimizing for, where we stand today, and where we're headed, use it as the reference point when deciding what to build or how to build it.

# Principles it's built on
- **Privacy First:** No data should leave the user's machine. We're actively working toward zero sensitive-data capture (passwords, card numbers, etc.); this isn't fully solved yet, see Current Limitations below.
- **Performance & Efficiency:** It should not be a performance blocker and should be efficient enough. This comes before accuracy.
- **Transparency:** Openly share the current limitations of the system rather than hiding them.

# Current Limitations
- Sensitive info like passwords or card numbers can still be captured today. Manual pause/resume of capture is the current workaround while automatic redaction for this is being built.
- Per-app redaction is best-effort. The rules live in `core/privacy_settings.py` and are exposed in Settings → Privacy & access, but matching is title-based and brittle outside Clippy Vision's own window (e.g. an Incognito window stops matching once you navigate, see GitHub #41). Stopping capture is the dependable privacy switch until this is fixed.
- Capture no longer needs a vision model or a dedicated GPU. The remaining hardware cost is the chat model (`qwen3:8b` by default): minimum is 8 GB RAM, recommended 16 GB. A smaller chat model can be picked in setup. Do not put a vision-language model back on the default capture path.

# Roadmap
No fixed timeline, ordered by priority rather than by date.

**Version 1.0.1 (Shipped)**
- [x] Screen capture for Windows
- [x] Context building using `qwen3:8b` (early releases also used `qwen3-vl:4b` on screenshots; that path is gone)
- [x] Hierarchical memory handling
- [x] Intent detection and query routing
- [x] ReAct agent for data retrieval and answering

**Version 1.1.0 (Shipped)**
- [x] Delete option for conversations (chats with agent)
- [ ] Screen redaction for WhatsApp, incognito tabs/private windows, Gmail, Outlook, etc. (still open, window matching is unreliable outside Clippy's own window, see Current Limitations)
- [x] Markdown rendering for agent responses in UI
- [x] Other bug fixes

**Version 1.2.0 (Shipped)**
- [x] Screen capture support for macOS along with a macOS release

**Version 1.2.1 (Shipped)**
- [x] Capture cascade: accessibility APIs first (UI Automation on Windows, AXUIElement on macOS), RapidOCR when that text is empty or too thin. Shared entry points live in `core/accessibility_text.py` and `core/screenshot_enrichment.py`. Classification of the frame uses the extracted text (`build_capture_text_verdict`); it does not call a vision model.

**Version 1.2.2 (Shipped)**
- [x] Setup window: resizable, sized to the work area, and scrollable so the hardware table and Continue/Launch stay usable on small screens (GitHub #44).

**Version 1.3.0 (Shipped)**
- [x] MCP server ships with the packaged app: `mcp_server.py` and the `scripts/clippy-mcp` launchers are bundled, paths resolve when Claude Desktop / Cursor / VS Code spawn it with no Clippy environment, and Settings → Connect apps generates the per-client config.
- [x] Capture efficiency: accessibility-tree walks moved off the capture hot path onto a background worker (`core/uia_worker.py`), backlog enrichment throttles under system load without starving old screenshots, and local performance metrics (`core/performance_metrics.py`) make the overhead measurable.
- [x] Model weights are no longer bundled: MiniLM embeddings and the fine-tuned query router download from Hugging Face during setup or on first use (`core/model_download.py`), keeping the repo and installer small.
- [x] Timeline view: browse captured sessions in the app and drill into what was recorded.
- [x] Electron shell refactor: monolithic `main.js` and `index.html` split into focused main-process modules and ES modules.

**Version 1.3.1 (Current)**
- [x] Runtime LLM calls respect the chat model chosen in setup (`CLIPPY_CHAT_MODEL` / `llm_config.json`) instead of always requesting hardcoded `qwen3:8b`, which caused Ollama to auto-pull qwen3 even when another model was already configured.

**Planned next, ordered by priority**

*Per-app privacy redaction that actually holds*
Reliable incognito/private-window detection (GitHub #41): title matching breaks after navigation, so the next attempt is inspecting the UIA layout tree of the foreground window for the Incognito/InPrivate badge instead of trusting the title.

*Skills layer, making the agent proactive instead of purely reactive*
Development happens on the `feat/skills-ui` branch, off main until it is solid. Planned skill 1: a reading/watching mode that quizzes you on material afterward. Planned skill 2: "when you see XYZ, do ABC" — the watcher and matcher already work on the branch; what is left is a stable worker lifecycle and polished settings/alerts UI.

*Opt-in cloud model providers*
For users who prefer API speed over full locality: Claude, GPT, Gemini, OpenRouter as an explicit opt-in with clear warnings about what leaves the machine (groundwork in PR #32). Capture and memory stay local regardless.

*Capture audit deletion*
The timeline shows what was captured; the missing half is deleting individual entries from memory directly in that view, so users can see and remove exactly what is stored about them.

*Audio capture and speaker attribution for meetings*
Local transcription with faster-whisper or whisper.cpp, pinned to CPU so it does not compete with the reasoning model for RAM or VRAM. First version attributes speech by audio source rather than by voice: microphone is the user, system output loopback is everyone else, which needs no enrollment and no extra model. Voiceprint matching (a one-time voice sample, then embedding similarity per segment) is a later addition for in-person conversations where every voice arrives through the mic. No meeting-platform APIs or bots, loopback capture works the same across Zoom, Meet and Teams. macOS system audio is the hard part and will need ScreenCaptureKit audio or a virtual device.

*Mouse and idle signals*
Mouse activity is intended as an idle detector that gates capture, not as stored events. Storing raw clicks and scrolls adds volume without meaning and works against the bounded-storage design.

# Licensing
Core stays free and open source for individuals, always, latest version, no delay, source is visible for every version we release. Leaning toward AGPL (or similar) for the core, with a separate commercial license for companies that want to use it without AGPL's obligations. Still being finalized.

# Future Vision
The ultimate plan for monetizing Clippy Vision is an enterprise version, where an employee could hand off their work context to another employee, using what Clippy already captured, instead of calling and disturbing someone on vacation. There are other use cases beyond this one too. Individual versions stay completely free, regardless of what the enterprise version looks like.

# Contributing
Want to help build this? See [CONTRIBUTING.md](https://github.com/protocorn/clippy-vision?tab=contributing-ov-file) for setup, and join the Discord server for ongoing discussion, skills architecture, and what's currently being worked on.
