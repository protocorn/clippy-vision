# Usage Guide

How to run Clippy Vision day to day: capture controls, privacy settings, accessibility behavior, and install troubleshooting.

For connecting an agent, see [mcp.md](mcp.md). For how capture and memory work internally, see [architecture.md](architecture.md).

---

## Capture controls

Capture starts when the app launches. Closing the window leaves the tray icon running; capture continues from the tray.

**Pause vs stop**

- **Pause** turns capture off for a timed period, then turns it back on.
- **Stop** turns capture off until you start it again.

Use the button in the app or the tray icon. Clippy shows a desktop notification when capture state changes.

Pause capture whenever a window must not be stored. Privacy redaction is best effort; pause is the reliable off switch.

---

## Privacy settings

Open **Settings** to manage what gets stored.

### Private apps

Apps you list under privacy are blacked out whenever they are visible, not only when they are in front. Common messaging apps can be toggled there, including Instagram, WhatsApp, Telegram, Signal, Slack, and Discord.

### Private browsing

Private browsing protection is on by default for **Google Chrome**, **Microsoft Edge**, and **Brave** on Windows and macOS:

- The window is painted black.
- The address is not stored.
- The title is saved as "Private window".

Firefox, Opera, and Safari are not covered. Pause capture when those private windows must not be stored.

### What the privacy layer does

Before text or a screenshot is stored, Clippy tries to:

- Paint password fields and other single-line sensitive fields.
- Strip API keys, tokens, private keys, and other secret-shaped text.
- Black out Clippy's own window in every screenshot.

The same redactor runs again on every MCP tool reply. Screenshot file paths are left out. A window you marked private is returned as "This moment was private" with the app and time only. If that filter fails, the tool returns nothing. The copy on this computer is unchanged.

This is a best effort, not a guarantee.

<p align="center">
  <img src="../assets/instagram-redaction.jpg" alt="Instagram login with the password field blacked out in a Clippy capture" width="720" />
</p>

### Retention defaults

| Data | Default retention | Configurable range |
|------|-------------------|--------------------|
| Raw events | 7 days | 1–30 days in Settings |
| Session summaries | 90 days | 1–180 days in Settings |
| Screenshots | 1 day (high-signal frames up to 7) | See Settings (`screenshot_retention_max_days`) |
| Long-term memory facts | Until you delete them | — |

OCR text on events remains after the JPEG is purged. You can delete one moment, one session, or one day from the timeline.

### Local network binding

The local API binds to `127.0.0.1` on a port chosen at launch. It is never reachable from your network.

### Update check

Clippy checks the public GitHub releases page for a newer version at most once every 12 hours. That request carries no screen, profile, or account data. Turn it off under **Settings → Updates**. Tool replies sent to an MCP client are separate: they leave the machine only if that client sends them on.

---

## Editor accessibility

For Cursor, VS Code, and other VS Code-based editors, Clippy turns on that editor's screen-reader accessibility setting when the app is in front and the setting is off. That is how the open file can be read. It does not turn on a Windows or macOS screen reader.

On macOS, Screen Recording and Accessibility permissions are required for screenshots, window titles, and password-field painting. Grant them when Clippy asks. If a prompt is dismissed, turn them on in **System Settings → Privacy & Security**.

---

## Asking about your activity

There is no in-app chat. Connect an MCP client under **Settings → Connect apps**, then ask in that app. The agent chooses which Clippy tool to call.

See [mcp.md](mcp.md) for supported clients, Copy JSON instructions, and the tool list.

---

## Installation troubleshooting

### Setup wizard fails at a step

Click **Retry** on the failed step. If it keeps failing, check the log panel in the wizard for the specific error.

### App stuck on the opening screen

The window opens once the local server answers a health check. The text model can still be loading after that. If the opening screen never leaves, the server did not become healthy. Close the app and open it again. It re-runs the preflight checks and returns to setup when something is missing.

### "Windows protected your PC" on install

The installers are unsigned. Click **More info → Run anyway**.

### macOS says the app cannot be opened

The disk image is unsigned. Right-click Clippy Vision and choose **Open**, then confirm. Grant Screen Recording and Accessibility when asked.

### Homebrew / dependencies on macOS

The setup wizard uses Homebrew when Homebrew is already installed to install Python and Ollama. If Homebrew is missing, install Python 3.11+ and Ollama yourself, then retry setup. `brew install ollama` is the same step the wizard tries when Homebrew is present.

### Ollama not found after install

Open a new terminal and run `ollama --version`. If not found, re-run setup or install it from [ollama.com](https://ollama.com/download).

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

## File locations (installed)

| Item | Windows | macOS |
|------|---------|-------|
| App data (DB, screenshots) | `%APPDATA%\Clippy Vision\data\` | `~/Library/Application Support/Clippy Vision/data/` |
| Setup flag | `%APPDATA%\Clippy Vision\setup_complete.json` | `~/Library/Application Support/Clippy Vision/setup_complete.json` |
| API port | `%APPDATA%\Clippy Vision\api_process.json` | `~/Library/Application Support/Clippy Vision/api_process.json` |
| Ollama models | `%USERPROFILE%\.ollama\models\` | `~/.ollama/models/` |
| App install | `%LOCALAPPDATA%\Programs\Clippy Vision\` | `/Applications/Clippy Vision.app` |

A source run (`npm start`) stores the database in `core/data` inside the checkout, not in the installed-app folder. See [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Uninstall

Windows: **Settings → Apps → Clippy Vision → Uninstall**, or run the uninstaller in `%LOCALAPPDATA%\Programs\Clippy Vision\`.

macOS: drag **Clippy Vision** out of Applications.

To remove captured data, delete the app data folder in the table above. To remove Ollama models, delete the Ollama models folder in that table.
