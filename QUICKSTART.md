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

---

## Troubleshooting

### Setup wizard fails at a step
Click **Retry** on the failed step. If it keeps failing, check the log panel in the wizard for the specific error.

### App stuck on loading screen
The API server takes 30–60s on first launch while models load into RAM. Wait for the spinner to clear. If it stays stuck, close the app, reopen it — it will re-run preflight checks and redirect to setup if anything is broken.

### "Windows protected your PC" on install
The installer is unsigned. Click **More info → Run anyway**.

### macOS says the app cannot be opened
The disk image is unsigned. Right-click Clippy Vision and choose **Open**, then confirm. Grant Screen Recording and Accessibility when asked.

### Ollama not found after install
Open a new terminal and run `ollama --version`. If not found, re-run setup or install it from [ollama.com](https://ollama.com/download). On macOS, `brew install ollama` is the same step the wizard tries.

### Reset setup / reinstall
Delete `setup_complete.json` from the app data folder below. The setup wizard runs again on the next launch.

### Check if everything is working
The API listens on `127.0.0.1` at the port recorded in `api_process.json` next to the app data. It is not fixed at port 8000.

```powershell
# Windows
$state = Get-Content "$env:APPDATA\Clippy Vision\api_process.json" | ConvertFrom-Json
Invoke-RestMethod "http://127.0.0.1:$($state.port)/health"
python -c "import sqlite3, os; db=os.path.join(os.environ['APPDATA'],'Clippy Vision','data','events.db'); print(sqlite3.connect(db).execute('SELECT COUNT(*) FROM events').fetchone()[0], 'events')"
```

On macOS, in Terminal:

```bash
python3 -c "import json,urllib.request,pathlib; p=pathlib.Path.home()/'Library/Application Support/Clippy Vision/api_process.json'; port=json.loads(p.read_text())['port']; print(urllib.request.urlopen(f'http://127.0.0.1:{port}/health').read().decode())"
```

---

## File Locations (installed)

| Item | Windows | macOS |
|------|---------|-------|
| App data (DB, screenshots) | `%APPDATA%\Clippy Vision\data\` | `~/Library/Application Support/Clippy Vision/data/` |
| Setup flag | `%APPDATA%\Clippy Vision\setup_complete.json` | `~/Library/Application Support/Clippy Vision/setup_complete.json` |
| API port | `%APPDATA%\Clippy Vision\api_process.json` | `~/Library/Application Support/Clippy Vision/api_process.json` |
| Ollama models | `%USERPROFILE%\.ollama\models\` | `~/.ollama/models/` |
| App install | `%LOCALAPPDATA%\Programs\Clippy Vision\` | `/Applications/Clippy Vision.app` |

A source run (`npm start`) stores the database in `core/data` inside the checkout, not in the installed-app folder.

---

## Uninstall

Windows: **Settings → Apps → Clippy Vision → Uninstall**, or run the uninstaller in `%LOCALAPPDATA%\Programs\Clippy Vision\`.

macOS: drag **Clippy Vision** out of Applications.

To remove captured data, delete the app data folder in the table above. To remove Ollama models, delete the Ollama models folder in that table.
