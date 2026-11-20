# Quick Start — Clippy Vision

## Recommended: Use the Installer

Download `ClippyVision-Windows-Setup-{version}.exe` on Windows, or `ClippyVision-macOS-arm64-{version}.dmg` (Apple Silicon) / `ClippyVision-macOS-x64-{version}.dmg` (Intel) on macOS, from the [latest release](https://github.com/protocorn/clippy-vision/releases/latest).

The installer's built-in setup wizard will:
- Check for Python 3.11+. If none is on PATH, it downloads a relocatable CPython 3.12 into app data (no system Python install). `CLIPPY_PYTHON` still overrides.
- Check for Ollama the same way on Windows and macOS. On Linux it shows the official install command and can run it only with your consent.
- Start the Ollama service
- Install all Python packages from `requirements.txt` into the app-managed venv
- Download `qwen3:8b` automatically for session summaries (about 5 GB), with progress. `llama3.2:3b` is a labeled low-memory option that is less accurate. Change the model later in Settings.
- Warm that text model into memory

After setup completes, click **Launch**. Closing the window leaves the tray icon running. Launch at login is not turned on for you.

## Using the app

The home screen is the timeline. Open a session to see the events Clippy stored. There is no chat box in the app.

- **Capture** starts with the app. Pause for a set time, or stop until you turn it back on, from the button in the window or the tray icon.
- **Settings → Connect apps** is how an agent reads that memory. Cursor, Claude Desktop, and VS Code have a Connect button. **Copy JSON** builds the config from this computer. **Also connect with other apps** opens docs for Devin Desktop, Claude Code, Cline, Roo Code, Continue, JetBrains, Kiro, and LM Studio. Paste the same JSON there.
- Connecting lets that app call Clippy's tools. A cloud model receives those replies. Disconnect the app to stop.
- **Settings** is also where you black out apps, turn private-browsing capture off, and change how long events, screenshots, and summaries are kept.

Ask your questions in the connected app. Clippy does not answer them inside its own window.

**Requirements:** Windows 10/11 (64-bit) or macOS 12+. Internet is needed on first run for Python packages and the default text model (`qwen3:8b`, about 5 GB). You do not need to install Python yourself. Screen capture uses accessibility APIs and local OCR. It does not load a vision model.

On macOS, allow Screen Recording and Accessibility when Clippy asks. Both are required for screenshots, window titles, and password-field painting. If a prompt is dismissed, turn them on in System Settings → Privacy & Security.

The installers are unsigned. On Windows, SmartScreen shows "Windows protected your PC" — choose More info → Run anyway. On macOS, Gatekeeper may block the first open — right-click the app and choose Open.

---

## Running from Source

```powershell
git clone https://github.com/protocorn/clippy-vision.git
cd clippy-vision\electron-ui
npm install
npm start
```

The setup wizard runs automatically on first launch.

---

## Models

| Model | Size | Purpose |
|-------|------|---------|
| Default Ollama model `qwen3:8b` (changeable in Settings) | about 5 GB | Classification, session summaries, and long-term memory |
| `llama3.2:3b` (low-memory option) | about 2 GB | Same jobs; less accurate than `qwen3:8b`. Pick it only on low-memory machines |
| `all-MiniLM-L6-v2` | about 90 MB, downloaded from Hugging Face on first run | Optional local semantic retrieval. It is not bundled in the installer |

Pipeline details: [docs/architecture.md](docs/architecture.md).

---

## Troubleshooting, file locations, and uninstall

Full install troubleshooting (Homebrew, permissions, health checks), file locations, and uninstall steps: [docs/usage.md](docs/usage.md#installation-troubleshooting).

MCP connect and tool reference: [docs/mcp.md](docs/mcp.md). Build from source: [CONTRIBUTING.md](CONTRIBUTING.md#building-from-source).
