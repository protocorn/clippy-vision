# Quick Start — Clippy Vision

## Recommended: Use the Installer

Download `ClippyVision-Windows-Setup-{version}.exe` on Windows, or `ClippyVision-macOS-arm64-{version}.dmg` (Apple Silicon) / `ClippyVision-macOS-x64-{version}.dmg` (Intel) on macOS, from the [latest release](https://github.com/protocorn/clippy-vision/releases/latest).

The installer's built-in setup wizard will:
- Check for Python. Windows installs it with winget when it is missing. macOS installs it with Homebrew when Homebrew is already installed.
- Check for Ollama the same way (winget on Windows, Homebrew on macOS).
- Start the Ollama service
- Install all Python packages from `requirements.txt`
- Download the text model you picked in setup (the suggested default is `qwen3:8b`)
- Warm that text model into memory

After setup completes, click **Launch**. Closing the window leaves the tray icon running. Launch at login is not turned on for you.

## Using the app

The home screen is the timeline. Open a session to see the events Clippy stored. There is no chat box in the app.

- **Capture** starts with the app. Pause for a set time, or stop until you turn it back on, from the button in the window or the tray icon.
- **Settings → Connect apps** is how an agent reads that memory. Cursor, Claude Desktop, and VS Code have a Connect button. **Copy JSON** builds the config from this computer. **Also connect with other apps** opens docs for Devin Desktop, Claude Code, Cline, Roo Code, Continue, JetBrains, Kiro, and LM Studio. Paste the same JSON there.
- Connecting lets that app call Clippy's tools. A cloud model receives those replies. Disconnect the app to stop.
- **Settings** is also where you black out apps, turn private-browsing capture off, and change how long events, screenshots, and summaries are kept.

Ask your questions in the connected app. Clippy does not answer them inside its own window.

**Requirements:** Windows 10/11 (64-bit) or macOS 12+. Internet is needed on first run for the text model. Screen capture uses accessibility APIs and local OCR. It does not load a vision model.

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
| The Ollama model you pick (suggested `qwen3:8b`) | about 4.7 GB for `qwen3:8b` | Classification, session summaries, and long-term memory |
| `all-MiniLM-L6-v2` | about 90 MB, downloaded from Hugging Face on first run | Optional local semantic retrieval. It is not bundled in the installer |

Pipeline details: [docs/architecture.md](docs/architecture.md).

---

## Troubleshooting, file locations, and uninstall

Full install troubleshooting (Homebrew, permissions, health checks), file locations, and uninstall steps: [docs/usage.md](docs/usage.md#installation-troubleshooting).

MCP connect and tool reference: [docs/mcp.md](docs/mcp.md). Build from source: [CONTRIBUTING.md](CONTRIBUTING.md#building-from-source).
