# Clippy Vision's Vision
The aim of this project is to build a complete screen watcher tool, one that sees what's on your screen (windows, clipboard, typing activity, screenshots), stores it in memory, remembers it, and retrieves or acts on it when needed.
To build trust with users, capture and memory stay on the machine and the source stays open. A cloud agent you connect can still receive the memory it asks for.

This doc exists so contributors know what we're optimizing for, where we stand today, and where we're headed, use it as the reference point when deciding what to build or how to build it.

# Principles it's built on
- **Privacy First:** Capture and the memory database stay on the user's machine. A cloud MCP client still receives the tool replies it requests; Clippy scrubs secrets and private windows before that reply is returned. Password and card capture is reduced, not solved. See Current Limitations below.
- **Performance & Efficiency:** It should not be a performance blocker and should be efficient enough. This comes before accuracy.
- **Transparency:** Openly share the current limitations of the system rather than hiding them.

# Current Limitations
- A privacy layer now tries to redact passwords and other sensitive data before they are stored: password fields are painted out of screenshots, and secret-shaped text (keys, tokens, and similar) is stripped. It is an effort, not a guarantee. Pause capture when you need to be sure nothing is saved.
- Per-app redaction is best-effort for the messaging apps in Settings. Private browsing is separate and on by default: Google Chrome, Microsoft Edge, and Brave private windows are blacked out on Windows and macOS. Firefox, Opera, and Safari are not covered. Pause capture when a window must not be stored.
- Capture no longer needs a vision model or a dedicated GPU. The remaining hardware cost is the chat model (`qwen3:8b` by default): minimum is 8 GB RAM, recommended 16 GB. A smaller chat model can be picked in setup. Do not put a vision-language model back on the default capture path.

# Roadmap
No fixed timeline, ordered by priority rather than by date.

**Version 1.0.1 (Shipped)**
- [x] Screen capture for Windows
- [x] Context building using `qwen3:8b` (early releases also used `qwen3-vl:4b` on screenshots; that path is gone)
- [x] Hierarchical memory handling
- [x] Intent detection and query routing (removed from the live app; the classifier and prefetch strategies are in `archive/`)
- [x] ReAct agent for data retrieval and answering (in-app agent removed; ask through MCP)

**Version 1.1.0 (Shipped)**
- [x] Delete option for conversations (chats with agent)
- [x] Private browsing redaction for Google Chrome, Microsoft Edge, and Brave on Windows and macOS (GitHub #41). On by default in Settings. WhatsApp and the other listed apps remain optional toggles. Gmail and Outlook are not a separate blackout.
- [x] Markdown rendering for agent responses in UI
- [x] Other bug fixes

**Version 1.2.0 (Shipped)**
- [x] Screen capture support for macOS along with a macOS release

**Version 1.2.1 (Shipped)**
- [x] Capture cascade: accessibility APIs first (UI Automation on Windows, AXUIElement on macOS, with a System Events fallback if the AX frameworks are missing), RapidOCR when that text is empty or too thin. Shared entry points live in `core/accessibility_text.py` and `core/screenshot_enrichment.py`. Classification of the frame uses the extracted text (`build_capture_text_verdict`); it does not call a vision model.

**Version 1.2.2 (Shipped)**
- [x] Setup window: resizable, sized to the work area, and scrollable so the hardware table and Continue/Launch stay usable on small screens (GitHub #44).

**Version 1.3.0 (Shipped)**
- [x] MCP server ships with the packaged app: `mcp_server.py` and the `scripts/clippy-mcp` launchers are bundled, paths resolve when Claude Desktop / Cursor / VS Code spawn it with no Clippy environment, and Settings → Connect apps generates the per-client config.
- [x] Capture efficiency: accessibility-tree walks moved off the capture hot path onto a background worker (`core/uia_worker.py`), backlog enrichment throttles under system load without starving old screenshots, and local performance metrics (`core/performance_metrics.py`) make the overhead measurable.
- [x] Model weights are no longer bundled: MiniLM embeddings download from Hugging Face during setup or on first use (`core/model_download.py`), keeping the repo and installer small.
- [x] Timeline view: browse captured sessions in the app and drill into what was recorded.
- [x] Electron shell refactor: monolithic `main.js` and `index.html` split into focused main-process modules and ES modules.

**Version 1.3.1 (Current)**
- [x] Runtime LLM calls respect the chat model chosen in setup (`CLIPPY_CHAT_MODEL` / `llm_config.json`) instead of always requesting hardcoded `qwen3:8b`, which caused Ollama to auto-pull qwen3 even when another model was already configured.
- [x] Questions go through MCP tools (`search_sessions`, `search_events`, `recall_memory`). The query router and the old prefetch strategies (specific recall, time anchor, topic search) are archived. Semantic memory search stays in `agent/prefetch/memory_query.py`.
- [x] Privacy layer: an effort to redact passwords and other sensitive data before they are stored, on Windows and macOS. Password fields are painted out of screenshots, visible privacy-listed windows are blacked out, and secret-shaped text is stripped. Not a guarantee.
- [x] Timeline deletion: one event, one session, or one day, including the screenshot files that nothing else still references.

**Planned next, ordered by priority**

*Skills layer, making the agent proactive instead of purely reactive*
Development happens on the `feat/skills-ui` branch, off main until it is solid. Planned skill 1: a reading/watching mode that quizzes you on material afterward. Planned skill 2: "when you see XYZ, do ABC" — the watcher and matcher already work on the branch; what is left is a stable worker lifecycle and polished settings/alerts UI.

*Opt-in cloud model providers*
For users who prefer API speed over full locality: Claude, GPT, Gemini, OpenRouter as an explicit opt-in with clear warnings about what leaves the machine (groundwork in PR #32). Capture and memory stay local regardless.

*Audio capture and speaker attribution for meetings*
Local transcription with faster-whisper or whisper.cpp, pinned to CPU so it does not compete with the reasoning model for RAM or VRAM. First version attributes speech by audio source rather than by voice: microphone is the user, system output loopback is everyone else, which needs no enrollment and no extra model. Voiceprint matching (a one-time voice sample, then embedding similarity per segment) is a later addition for in-person conversations where every voice arrives through the mic. No meeting-platform APIs or bots, loopback capture works the same across Zoom, Meet and Teams. macOS system audio is the hard part and will need ScreenCaptureKit audio or a virtual device.

*Memory → action agent (Plan B — product north star for capabilities)*
Clippy already perceives and remembers. The next capability bet is general actions on top of that memory: retrieve what the user saw/did, then open paths/URLs, focus apps, and later use a11y-first GUI primitives — always **tools/OS first, pixel-click last**, and **not** a pile of per-app integrations. Observe → retrieve → reason → act → verify.

*Mouse streams*
Idle already uses the OS last-input clock, on Windows and macOS, and stretches background screenshot gaps instead of hard-skipping them. Do **not** store raw click or scroll streams. Bounded mouse aggregates and proactive `need_for_help` scoring stay deferred.

# Licensing
The next release is AGPL-3.0 (`LICENSE`). Individuals can use, modify, and share the client. A company that ships this code inside a product, or offers it as a service, publishes their changes under AGPL or arranges a commercial license. That commercial license is not written yet. Releases already published through v1.3.1 stay MIT.

# Future Vision
The ultimate plan for monetizing Clippy Vision is an enterprise version, where an employee could hand off their work context to another employee, using what Clippy already captured, instead of calling and disturbing someone on vacation. There are other use cases beyond this one too. Individual versions stay completely free, regardless of what the enterprise version looks like.

# Contributing
Want to help build this? See [CONTRIBUTING.md](https://github.com/protocorn/clippy-vision?tab=contributing-ov-file) for setup, and join the Discord server for ongoing discussion, skills architecture, and what's currently being worked on.
