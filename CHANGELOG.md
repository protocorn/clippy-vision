# Changelog

All notable changes to this project will be documented in this file.

This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- Linux install and run guide (`docs/linux.md`) ([#44](https://github.com/protocorn/clippy-vision/pull/44), part of [#5](https://github.com/protocorn/clippy-vision/issues/5)) — documentation. No real-machine testing.
- Chat retrieval-time privacy (password-manager process opt-out, private/redacted/Clippy windows, selected-apps mode, private sessions; drop the event if a privacy check raises), NULL-safe prompt fields, session lookback from local midnight with 60-minute events, and untrusted/no-guess prompt instructions ([#34](https://github.com/protocorn/clippy-vision/pull/34), part of [#6](https://github.com/protocorn/clippy-vision/issues/6)) — **CI-only** (pytest chat context and endpoints on ubuntu-latest and windows-latest). Does not start Ollama. (In-app chat removed in 2.1.1; privacy rules applied while chat existed.)
- Copy diagnostics button in Settings → About ([#42](https://github.com/protocorn/clippy-vision/pull/42), closes [#17](https://github.com/protocorn/clippy-vision/issues/17)) — **CI-only**. Copied JSON is counts, sizes, and machine/settings metadata only (no captured content). Not tested on a signed-in packaged Electron install. **Not real machine.**

### Changed
- Contributor guide now documents the CI pytest and Electron `node --test` commands ([#36](https://github.com/protocorn/clippy-vision/pull/36), closes [#18](https://github.com/protocorn/clippy-vision/issues/18)) — documentation. Author started the Windows-filtered command locally but did not finish a green run; no Linux/macOS verification on that PR.
- VS Code MCP guide now states that one-click Connect writes a `clippy-vision` entry under `servers` to the per-OS user `mcp.json`, then opens `vscode:mcp/install` ([#37](https://github.com/protocorn/clippy-vision/pull/37), follow-up to [#32](https://github.com/protocorn/clippy-vision/pull/32)) — documentation. Path logic is **CI-only** (`mcp.test.js`); Connect was not clicked in VS Code.
- Reuse already-computed window context and foreground hwnd lookups; pin Node 20 for the Electron MCP CI job ([#39](https://github.com/protocorn/clippy-vision/pull/39)) — **CI-only**. That PR claimed no behavior change and did not re-run the Xvfb screenshot test.
- Published v2.1.0 notes and changelog after the tag ([#35](https://github.com/protocorn/clippy-vision/pull/35)) — documentation. Dropped leftover draft/checklist text; Python 3.11+ as the stated requirement. Did not rebuild or retag.

### Fixed
- Background Ollama calls (Tier-2 classifier, summarizer, and other non-chat generate/chat) skip or reuse the resident chat model instead of loading a second model when another tag is loaded or free RAM is below the cold-load floor ([#111](https://github.com/protocorn/clippy-vision/issues/111)) — **CI-only**. Model-name match is exact after treating a missing tag as `:latest` (`qwen3` == `qwen3:latest`; `qwen3:4b` is not `qwen3:4b-instruct`). Intended skips log once per 120s and do not set backlog `last_error`. User-initiated chat is unchanged. Overlaps with [#107](https://github.com/protocorn/clippy-vision/pull/107)'s `/api/ps` helper (`other_model_loaded`); merge order is #106, #107, #108, then this.
- Chat prompt crash on SQL NULL `active_task` / `summary` / `ocr_text`; `/chat` and `/chat/stream` return a user-facing message when the model is missing instead of a 500 ([#34](https://github.com/protocorn/clippy-vision/pull/34)) — **CI-only**.
- Electron preflight parses `python --version` and requires Python 3.11+ ([#38](https://github.com/protocorn/clippy-vision/pull/38)) — **CI-only** (`node --test` of `setup.test.js` and `mcp.test.js`). Not in the published v2.1.0 installer or AppImage.
- Private-browsing docs aligned with a coverage table ([#41](https://github.com/protocorn/clippy-vision/pull/41), closes [#16](https://github.com/protocorn/clippy-vision/issues/16)) — documentation / **CI-only** wording honesty, not real-machine browser tests.
- Windows node spawn flake in `test_os_check` ([#40](https://github.com/protocorn/clippy-vision/pull/40)) — **CI-only** test deflake (`node --version` warm-up + 60s timeout).
- Release workflow attaches `docs/release-notes/<tag>.md` as the GitHub draft body; Linux AppImage `artifactName` pinned to `x86_64` ([#33](https://github.com/protocorn/clippy-vision/pull/33)) — CI/docs only. Published v2.1.0 assets were not rebuilt.

## [2.1.1] - 2026-10-15

Patch over v2.1.0. Verification is **CI-only** unless a line says otherwise. No **real machine** testing. Unsigned Windows NSIS + Linux AppImage (macOS skipped).

### Removed
- In-app chat UI and API ([#65](https://github.com/protocorn/clippy-vision/pull/65)): header/More-menu Chat controls, `#chat-view` panel, `electron-ui/src/js/chat.js` / `conversations.js` / `chat.test.js`, `/chat` and `/chat/stream`, and `core/chat_context.py` (with their pytest coverage). Ask via MCP under **Settings → Connect apps**. Day Cards, Connect apps, `agent/conversation.py` storage helpers, `/conversations*` for data-clear/restore, and `archive/in_app_chat/` are kept.

### Fixed
- Setup wizard uses an app-managed Python virtualenv and surfaces Linux Ollama/`zstd` installer errors ([#62](https://github.com/protocorn/clippy-vision/pull/62)) — **CI-only**.
- Linux setup installs `pynput` without `evdev`; pip error summary ignores harmless marker lines ([#63](https://github.com/protocorn/clippy-vision/pull/63)) — **CI-only**.
- Electron loading screen hang from missing `chatBtn` imports ([#64](https://github.com/protocorn/clippy-vision/pull/64)) — **CI-only**.
- Contributor wall de-dupes GitHub logins case-insensitively ([#61](https://github.com/protocorn/clippy-vision/pull/61)) — **CI-only**.
- Tag-push release builds pass `--publish never` so electron-builder does not require `GH_TOKEN` ([#60](https://github.com/protocorn/clippy-vision/pull/60)) — CI only.

## [2.1.0] - 2026-10-12

Adds experimental Linux support, in-app chat, expands browser privacy detection, and establishes CI plus unsigned installer dry-runs.

**Note:** macOS installers are not included in this release (opt-in only, requires manual validation).

Verification labels used below:
- **CI-only** — unit/integration tests in GitHub Actions
- **Xvfb launch** — packaged Linux AppImage launched under Xvfb in the release dry-run
- **Windows silent install** — packaged NSIS installer silent-installed on `windows-latest` in the release dry-run
- **real machine** — a physical desktop; nobody has done that for v2.1.0 work

### Added
- Linux platform support with X11 screenshot capture ([#10](https://github.com/protocorn/clippy-vision/pull/10), [#14](https://github.com/protocorn/clippy-vision/pull/14)) — **CI-only** (pytest, including Xvfb screenshot tests)
  - Window metadata collection via `xdotool`
  - Process name resolution via `psutil`
  - Clipboard reading (`wl-paste`, `xclip`, `xsel`)
  - Idle detection (`xprintidle`, `xdotool`)
  - X11 screenshots via `mss` with focused-window crop
  - Wayland session detection with graceful degradation
  - Blank/all-black frame guard for screenshot quality
- Linux AT-SPI accessibility text extraction ([#20](https://github.com/protocorn/clippy-vision/pull/20)) — **CI-only**
- Firefox, Opera, and Vivaldi private window detection on Windows ([#8](https://github.com/protocorn/clippy-vision/pull/8)) — **CI-only**
  - Firefox: pattern-based detection of "Private Browsing" markers
  - Opera and Vivaldi: Chromium-based BrowserRootView detection
  - macOS support via AppleScript mode detection (unit tests; Opera marked hypothesis)
- In-app chat backend (`/chat`, `/chat/stream`) with activity context retrieval ([#23](https://github.com/protocorn/clippy-vision/pull/23)) — **CI-only**
- In-app chat UI with streaming ([#25](https://github.com/protocorn/clippy-vision/pull/25)) — **CI-only**; not exercised against a real Ollama model
- Live progress while the setup wizard downloads Ollama models ([#19](https://github.com/protocorn/clippy-vision/pull/19)) — **CI-only** (parser tests; wizard UI itself is not driven in CI)
- Captured screen text marked untrusted in MCP replies and in-app chat ([#27](https://github.com/protocorn/clippy-vision/pull/27)) — **CI-only**
- Honor OS-level window capture opt-outs ([#28](https://github.com/protocorn/clippy-vision/pull/28)) — **CI-only** (mocked Win32 `GetWindowDisplayAffinity`, macOS window-sharing state, Linux no-op plus process-name fallback). **Not real machine.** Linux has no per-window OS capture API.
- VS Code MCP setup guide ([#29](https://github.com/protocorn/clippy-vision/pull/29), [@Afrin-banu-A](https://github.com/Afrin-banu-A)) — documentation; the author marked the setup steps as not verified on a real VS Code install
- CI testing on Ubuntu and Windows ([#7](https://github.com/protocorn/clippy-vision/pull/7)) — **CI-only**
- Unsigned Windows NSIS installer and Linux AppImage release workflow with PR dry-run ([#15](https://github.com/protocorn/clippy-vision/pull/15), this PR) — **Xvfb launch** and **Windows silent install**: packaged app spawned the backend and `GET /health` returned 200 `{"status":"ok"}` ([run 38286025883](https://github.com/protocorn/clippy-vision/actions/runs/38286025883) on `d068c49`, which includes #28/#29/#32). Setup was pre-seeded and Ollama stubbed; not a real-machine test.
- MCP setup guides ([#9](https://github.com/protocorn/clippy-vision/pull/9), [#13](https://github.com/protocorn/clippy-vision/pull/13), [#29](https://github.com/protocorn/clippy-vision/pull/29))
- Issue and pull request templates ([#7](https://github.com/protocorn/clippy-vision/pull/7))

### Changed
- Updated project roadmap to focus on free, high-impact features ([#7](https://github.com/protocorn/clippy-vision/pull/7))
- Deferred signed installers (requires ~$400-600/year budget) ([#7](https://github.com/protocorn/clippy-vision/pull/7))
- Lazy-loaded Windows/macOS imports for cross-platform compatibility ([#10](https://github.com/protocorn/clippy-vision/pull/10))

### Fixed
- Linux metadata collection now resolves PIDs to process names ([#10](https://github.com/protocorn/clippy-vision/pull/10)) — **CI-only**
- CI workflow properly excludes platform-specific tests ([#10](https://github.com/protocorn/clippy-vision/pull/10))
- Setup wizard now accepts Linux and allows users to proceed ([#26](https://github.com/protocorn/clippy-vision/pull/26)) — **CI-only**
  - Previously showed "Blocked" / "Cannot continue" for all Linux users
  - Now passes OS check and displays "Ubuntu 22.04+ (experimental)"
  - Linux with integrated GPU uses system RAM for VRAM grading
- Flaky X11 screenshot test under Xvfb waits for the window to map and polls for a non-blank frame ([#24](https://github.com/protocorn/clippy-vision/pull/24)) — **CI-only**
- VS Code one-click Connect wrote `mcp.json` to a Windows-style path on macOS and Linux ([#30](https://github.com/protocorn/clippy-vision/issues/30), [#32](https://github.com/protocorn/clippy-vision/pull/32)) — **CI-only** (`node --test` path tests on ubuntu-latest and windows-latest). Paths: Windows `%APPDATA%\Code\User\mcp.json`, macOS `~/Library/Application Support/Code/User/mcp.json`, Linux `$XDG_CONFIG_HOME/Code/User/mcp.json` or `~/.config/Code/User/mcp.json`. **Not real machine** (Connect was not clicked in VS Code).

### Known Issues
- Private-browsing docs overstated coverage at ship time ([#16](https://github.com/protocorn/clippy-vision/issues/16)). **Closed on main** by [#41](https://github.com/protocorn/clippy-vision/pull/41) (documentation / **CI-only** wording plus a coverage table; not real-machine browser tests). The v2.1.0 tag notes still describe the discrepancy as shipped.
- Python **3.11+** is required and is **not bundled**. The packaged app spawns `CLIPPY_PYTHON` if set, otherwise `python3` (Linux/macOS) or `python` (Windows) from PATH. `requirements.txt` does not pin a Python version. CI uses Python 3.12. See README.md for wizard behavior when Python is missing.
- Backend spawn is gated on `setup_complete.json` (first-run wizard). The release dry-run pre-seeds that file and stubs Ollama. With that bypass, `GET /health` returned 200 `{"status":"ok"}` on Linux (Xvfb AppImage, port 39367) and Windows (silent NSIS install, port 61677) in [run 38286025883](https://github.com/protocorn/clippy-vision/actions/runs/38286025883). That does not cover the wizard, Ollama, a real model, or a real desktop.
- Unsigned installers trigger SmartScreen/Gatekeeper warnings on first install.
- No **real machine** testing of capture, chat, or setup for v2.1.0.

## [2.0.0] - 2024-10-09

Baseline v2.0.0 (commit [8271941](https://github.com/protocorn/clippy-vision/commit/8271941)). Changes above landed after that commit.

[Unreleased]: https://github.com/protocorn/clippy-vision/compare/v2.1.1...HEAD
[2.1.1]: https://github.com/protocorn/clippy-vision/compare/v2.1.0...v2.1.1
[2.1.0]: https://github.com/protocorn/clippy-vision/compare/8271941...v2.1.0
[2.0.0]: https://github.com/protocorn/clippy-vision/releases/tag/v2.0.0
