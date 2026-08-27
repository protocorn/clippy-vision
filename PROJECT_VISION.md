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
- Per-app redaction is not usable yet. The backend rules exist in `core/privacy_settings.py`, but reliable window matching only works for Clippy Vision's own window; other apps match inconsistently. That is why Settings shows "Access control" as coming soon. Stopping capture is the dependable privacy switch until this is fixed.
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

**Version 1.2.2 (Current)**
- [x] Setup window: resizable, sized to the work area, and scrollable so the hardware table and Continue/Launch stay usable on small screens (GitHub #44).
- [ ] Prove the cascade stays cheap: skip OCR when accessibility text already clears `is_useful_accessibility_text`, and add a `bench/` run that reports a11y-enough / OCR-used / empty-text rates. `bench/test_classification_cascade.py` today measures classification tiers (rules → features → LLM), not this text-source split. Without that number, OCR can quietly run on every frame and the efficiency win erodes.

**Version 1.3.0 (Next in pipeline)**
- Skills layer, making the agent proactive instead of purely reactive
- Planned skill 1: A reading/watching mode that quizzes you on material afterward, and a timed study mode that builds a quiz/test plus analytics once a session ends
- Planned skill 2: "when you see XYZ, do ABC". `skills/when_x_then_y.py` already watches a URL in the background (HTTP) and/or the focused tab (accessibility text), with literal gates and an optional intent LLM. What is left is shipping it as a first-class skill in the app: settings, alerts in the UI, and a stable worker lifecycle — not rewriting the matcher.
- MCP server integration. `mcp_server.py` already exposes retrieval and memory tools over stdio, the packaged app ships it, Settings can write Claude Desktop / Cursor configs, and `docs/MCP.md` covers manual setup. What is left is proving that spawn works on real Windows and macOS installs (packaged paths, no Clippy env), and broadening beyond those two clients. Smoke coverage is `tests/test_mcp_server.py`; it does not replace a live Claude/Cursor connect.

**Planned next, ordered by priority**

*Timeline and capture audit view*
A browsable view of what Clippy actually captured, session by session, with the matching screenshots, plus the ability to delete individual entries from memory. This serves recall (scroll back to find something) and trust equally: a user should be able to see exactly what is stored about them and remove it. Needs listing endpoints on the API first, since `api_server.py` has no events or sessions listing today.

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
