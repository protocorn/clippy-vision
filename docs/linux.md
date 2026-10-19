# Linux install and run (experimental)

Linux support is **experimental and X11 only. Wayland is not supported.** Log in to an X11 session. This page is the install and run guide. It only states behavior that is in the source or existing docs. Anything not confirmed in this repository is marked **not verified yet**.

Day-to-day capture and privacy: [usage.md](usage.md). Connecting an agent: [mcp.md](mcp.md). Building from source and CI test commands: [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Supported / tested scope

| | What the repo says |
|--|--------------------|
| Stated OS | Ubuntu 22.04+ (experimental), **X11 only**. Wayland is not supported. The setup wizard OS check labels Linux as `Linux (supported, experimental)` and shows the requirement `Ubuntu 22.04+ (experimental)` (`electron-ui/electron/lib/hardware.js`, `electron-ui/src/setup.html`). |
| Architecture | Linux AppImage is **x86_64 only** (`electron-ui/package.json` `build.linux.artifactName`: `ClippyVision-Linux-x86_64-${version}.AppImage`; #33 pinned that name). |
| Recommended hardware | Wizard continue-or-block floor is still 8 GB RAM and 8 GB free disk (`hardware.js`); 16 GB RAM and 10 GB disk are the wizard “recommended” values. Below the floor blocks setup. Between the floor and recommended, you can continue with a warning. **In practice, 16 GB system RAM is required for the default `qwen3:8b`** — 8 GB is not enough over a long session ([README requirements](../README.md#requirements)). Linux with no discrete NVIDIA GPU (nvidia-smi reports 0) grades VRAM from system RAM (`hardware.js`). |
| **CI (pytest)** | Ubuntu runner, Python 3.12. Platform-filtered unit tests, plus an Xvfb screenshot test: `xvfb-run -a pytest tests/test_linux_screenshot_capture.py -v -m "linux" -k "xvfb"` (`.github/workflows/tests.yml`). That Xvfb job installs `xvfb`, `x11-xserver-utils`, and `xterm` only. |
| **CI (AppImage)** | Unsigned AppImage built with `npm run dist:linux`. Smoke check: `chmod +x`, then `./…AppImage --appimage-extract`, then looks for `squashfs-root/clippy-vision` or `squashfs-root/AppRun`. Launch test: Xvfb `:99`, `libfuse2` installed, setup pre-seeded, Ollama stubbed, `CLIPPY_PYTHON` pointed at the runner’s Python 3.12. Proves the packaged app can spawn the backend and `GET /health` returns 200. **Does not prove** the setup wizard, Ollama, a real model, or a real desktop (`.github/workflows/release.yml`). |
| **Real desktop** | Nobody has run Linux capture, wizard, or AppImage on a physical desktop. Treat every capture path below as **not verified yet** on GNOME, KDE, Sway, or any other compositor. |

Other distros and ARM are **not verified yet**. Wayland is **not supported** (log in to X11).

---

## Prerequisites

Install these **before** first launch. The Linux setup wizard does not silently install them.

1. **Install Ollama first.** Official command:

   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ```

   Download page: [https://ollama.com/download/linux](https://ollama.com/download/linux). If Ollama is missing, the wizard shows that command and a link. It can optionally run the official installer only after you click **Install** and confirm it needs sudo/admin rights. It never tries Homebrew on Linux.
2. **Python 3.11+** on PATH (next section). Python is not inside the AppImage.

---

## Get the AppImage

1. Download the published AppImage from the [GitHub releases page](https://github.com/protocorn/clippy-vision/releases) when a Linux build is published (artifact name `ClippyVision-Linux-x86_64-{version}.AppImage`). Until then, build from source with `npm run dist:linux` in `electron-ui/`.
2. The release workflow and `electron-builder` name the artifact:

   `ClippyVision-Linux-x86_64-${version}.AppImage`

   Example for v2.1.0: `ClippyVision-Linux-x86_64-2.1.0.AppImage`. The smoke check also matches `ClippyVision-Linux-*.AppImage`.
3. Make it executable and run it (from the directory that contains the file):

```bash
chmod +x ClippyVision-Linux-x86_64-2.1.0.AppImage
./ClippyVision-Linux-x86_64-2.1.0.AppImage
```

Replace the filename with the version you downloaded.

The AppImage is **unsigned** (release job name: “Build Linux AppImage (unsigned)”). There is no signed Linux installer.

**FUSE (general AppImage guidance, not a Clippy-specific requirement in application code):** AppImages produced by electron-builder commonly need FUSE to run in place. This repo’s launch test installs `libfuse2` on Ubuntu before executing the AppImage (`sudo apt-get install -y … libfuse2 …` in `.github/workflows/release.yml`). If the AppImage refuses to start with a FUSE-related error, installing `libfuse2` is the usual Debian/Ubuntu fix. That package install is **CI-only** here; it is **not verified yet** on a real desktop. The smoke check extracts with `--appimage-extract` instead of running under FUSE.

If `libfuse2` is missing, you can still run the AppImage without FUSE:

```bash
./ClippyVision-Linux-x86_64-2.1.0.AppImage --appimage-extract-and-run
```

Python is **not** inside the AppImage. Install Ollama and Python 3.11+ first (see Prerequisites), then run the AppImage so the first-run wizard can find them.

Packaged Linux `userData` is `~/.config/Clippy Vision` (Electron `productName`; confirmed in the release workflow). First-run setup writes `setup_complete.json` there. Until that file exists, the backend does not start.

---

## Python 3.11+ (not bundled)

`requirements.txt` does not pin a Python version. Current source (`electron-ui/electron/lib/setup.js`) parses `python --version` and requires **Python 3.11+**. Preflight copy: “Clippy needs Python 3.11+ on PATH.”

The packaged app (`electron-ui/electron/lib/paths.js`) runs, in order:

1. `CLIPPY_PYTHON` if that environment variable is set
2. otherwise `python3` from `PATH` on Linux

There are no extra PATH hints on Linux (those exist only on macOS).

To point Clippy at a specific interpreter **before** launch:

```bash
export CLIPPY_PYTHON=/path/to/venv/bin/python3
```

**The wizard does not install Python on Linux.** If `python3` is missing, it does **not** try Homebrew. It shows a clear manual-install message with a link to [https://www.python.org/downloads/](https://www.python.org/downloads/). Retry after you install Python 3.11+ and put it on PATH. Comment in `setup.js`: “Linux stays a manual install.”

CI unit tests and the AppImage dry-run use **Python 3.12**.

**Python 3.11+ is required.** The setup wizard parses `python --version` and rejects anything below 3.11. Python is not bundled. Do not treat 3.9 as enough ([CHANGELOG.md](../CHANGELOG.md) / [release notes](release-notes/v2.1.0.md)).

---

## System tools Clippy uses on Linux

None of these are bundled. Clippy looks them up with `shutil.which` and degrades if they are missing. Package names below are Debian/Ubuntu names that match those binaries, or names this repo already uses in comments/CI.

| Tool | What Clippy uses it for | Without it | Debian/Ubuntu package |
|------|-------------------------|------------|------------------------|
| `xdotool` | Window title (`getactivewindow getwindowname`), PID → process name via `psutil`, focused-window geometry for screenshot crop | Title empty, process name `"unknown"`, no focused-window crop. One-time warning: `[platform_support] xdotool not found; window metadata will be limited` | `xdotool` (installed in the AppImage launch job) |
| `xprintidle` (then `xdotool get_idle_time`) | Idle seconds | Idle detection returns `None`. One-time warning: `[platform_support] Linux idle detection unavailable (xprintidle or xdotool not found)` | `xprintidle` is the usual package for that binary — **not installed in this repo’s CI** |
| `wl-paste` | Clipboard (tried first) | Falls through to `xclip`, then `xsel` | `wl-clipboard` (provides `wl-paste`; listed in [v2.1.0 notes](release-notes/v2.1.0.md)) |
| `xclip` | Clipboard if `wl-paste` is absent | Falls through to `xsel` | `xclip` |
| `xsel` | Clipboard if the others are absent | Clipboard capture returns nothing (no dedicated warning) | `xsel` |
| `grim` | Code may call `grim -` if the session is detected as Wayland | **Wayland is not supported.** Do not treat `grim` as a supported workaround. Log in to X11. | `grim` — present in code; **unsupported** as a user path |
| `python3-pyatspi` | AT-SPI accessible text | Accessibility text is empty. One-time warning tells you to install `python3-pyatspi` | `python3-pyatspi` (`requirements.txt` comment: `apt install python3-pyatspi`). **CI-only** (mocked unit tests). Not tried on a real desktop. |

`xclip`, `xsel`, `wl-clipboard`, `grim`, and `xprintidle` are **not** installed by the pytest or AppImage jobs except where the table says otherwise. Exact `apt-get` lines for a personal desktop are **not verified yet**.

AT-SPI extra: the warning also says that if you use a venv, recreate it with `--system-site-packages` or ensure `python3-gi` is available. `pyatspi` is **not** a pip package in `requirements.txt`.

---

## X11 vs Wayland

**Supported session: X11.** Wayland is not supported. If `echo "$XDG_SESSION_TYPE"` prints `wayland`, log out and choose an X11 session (often labeled “Ubuntu on Xorg” or similar — the exact login-menu name is **not verified yet**).

Session kind is read in `core/linux_screenshot.py` (`linux_session_type()`), in this order:

1. `XDG_SESSION_TYPE` — `wayland` or `x11` (case-insensitive, trimmed)
2. else `WAYLAND_DISPLAY` set → `wayland`
3. else `DISPLAY` set → `x11`
4. else `unknown`

Check the first of those:

```bash
echo "$XDG_SESSION_TYPE"
```

Optional fallbacks the code also reads:

```bash
echo "$WAYLAND_DISPLAY"
echo "$DISPLAY"
```

| Session | Status | What the code does | What you should do |
|---------|--------|--------------------|--------------------|
| **X11** | **Supported (experimental)** | `mss` (needs `DISPLAY`). Crop to the focused window when `xdotool` can return geometry. Blank/all-black frames are not stored. | Use this session. This is the path the Xvfb pytest exercises. |
| **Wayland** | **Unsupported** | If `grim` is on `PATH`, the code may try `grim -`. If `DISPLAY` is set, it may also try `mss`. Blank grabs (typical under XWayland) are discarded. xdg-desktop-portal is **not implemented**. | Log in to an **X11** session. `grim` is not a supported workaround. |
| **unknown** / no `DISPLAY` | **Unsupported** | Same skip/warning path as a Wayland miss when there is no usable grab | Log in to X11 so `XDG_SESSION_TYPE=x11` and `DISPLAY` are set. |

One-time stderr warnings (you should not stay on Wayland to “work around” them):

- `[capture] Linux Wayland screenshots are not supported yet (xdg-desktop-portal not implemented); skipping blank captures`
- `[capture] screenshot grab returned a blank frame; skipping`

`xdotool` is an X11 tool. Clipboard code tries `wl-paste` first, then `xclip` / `xsel`; on an X11 session, `xclip` or `xsel` is what you want.

---

## First-run wizard and Ollama

On first launch the setup wizard runs (`electron-ui/electron/lib/setup.js`, `electron-ui/src/setup.html`). This page does not duplicate that UI.

When Ollama is the managed local runtime (default base URL `http://127.0.0.1:11434`):

1. Python 3.11+ on PATH (see above)
2. Ollama on PATH — `ollama --version`. If missing, Linux does **not** try Homebrew. The wizard shows the official command (`curl -fsSL https://ollama.com/install.sh | sh`) and https://ollama.com/download/linux. An **Install** button can run that script only after you confirm it needs sudo/admin rights (`pkexec`, or a visible terminal if `pkexec` is missing). After it runs, the wizard re-checks that Ollama is reachable and fails with a clear message if it is not. The wizard does **not** apt-install Ollama and never runs the installer silently.
3. Start/configure Ollama (`ollama serve` / wait until reachable)
4. `pip install -r requirements.txt` into the interpreter from `CLIPPY_PYTHON` / `python3`
5. Download MiniLM embeddings (`python -m core.model_download`) and pull **only** the chat model you selected (`python -m core.ollama_pull <model>`)
6. Warmup and write `setup_complete.json`

Consent copy in the wizard: Ollama is installed only when missing, via “winget on Windows, Homebrew on macOS, official Linux script with consent, or ollama.com”. Suggested chat models in the wizard include `qwen3:8b` (hint: balanced; README says ~4.7 GB). Capture uses accessibility + OCR; no vision model is downloaded.

You can point at a specific `ollama` binary with `CLIPPY_OLLAMA` (`paths.js`). That override is **not verified yet** on a Linux desktop.

Troubleshooting already in [usage.md](usage.md#installation-troubleshooting): Retry on a failed wizard step, stuck opening screen, `ollama --version` after install, deleting `setup_complete.json` to re-run setup. Lower-spec / smaller chat model: [CONTRIBUTING.md](../CONTRIBUTING.md#lower-spec-machines).

The AppImage dry-run **does not** run this wizard.

---

## Privacy limits on Linux

Full tables and controls: [usage.md](usage.md#privacy-settings) (private browsing: [usage.md](usage.md#private-browsing)).

- **Private browsing:** not implemented on Linux (`core/private_windows.py` always returns false). A private browser window is captured like any other. Pause capture when that window must not be stored.
- **OS capture opt-out:** Linux has no per-window “do not capture” API. Clippy uses an **exact** process-name list of password managers (not substring matching): `1password`, `bitwarden`, `dashlane`, `keepass`, `keepassxc`, `lastpass`, `keeper`, `enpass`, `nordpass`, `roboform`, plus a few aliases (`core/capture_optout.py`). When the foreground process matches, screenshot, OCR, accessibility text, and clipboard are skipped for that moment.
- Redaction of password fields and secret-shaped text is still attempted where AT-SPI / OCR run. It is best effort.

---

## Running from source (contributors)

Do not copy a second setup here. Use:

- [CONTRIBUTING.md](../CONTRIBUTING.md#building-from-source) — `cd electron-ui`, `npm install`, `npm start`; the wizard runs on first launch
- [CONTRIBUTING.md](../CONTRIBUTING.md#run-the-tests-like-ci-does) — Linux pytest markers and the Xvfb screenshot command

A source run (`npm start`) stores activity in `core/data` inside the checkout, not under `~/.config/Clippy Vision`. Dev vs installed app: [CONTRIBUTING.md](../CONTRIBUTING.md#dev-app-vs-installed-app).

Linux AppImage from a checkout: `cd electron-ui` then `npm run dist:linux` (same script CI uses). Output: `electron-ui/dist/ClippyVision-Linux-x86_64-${version}.AppImage`.

---

## File locations (packaged Linux)

| Item | Path |
|------|------|
| App data (DB, screenshots, `llm_config.json`) | `~/.config/Clippy Vision/data/` |
| Setup flag | `~/.config/Clippy Vision/setup_complete.json` |
| API port | `~/.config/Clippy Vision/api_process.json` |

Those paths follow Electron `userData` for `productName` “Clippy Vision”. Confirmed in `.github/workflows/release.yml` (CI seeded `~/.config/Clippy Vision` and logged `USER_DATA=/home/runner/.config/Clippy Vision`).

The local API binds to `127.0.0.1` on the port in `api_process.json` (not a fixed 8000). Same health-check idea as [usage.md](usage.md#check-if-everything-is-working), using the Linux path:

```bash
python3 -c "import json,urllib.request,pathlib; p=pathlib.Path.home()/'.config'/'Clippy Vision'/'api_process.json'; port=json.loads(p.read_text())['port']; print(urllib.request.urlopen(f'http://127.0.0.1:{port}/health').read().decode())"
```

That snippet is the macOS check from `docs/usage.md` with the Linux `userData` path substituted. It is **not verified yet** on a real desktop.

---

## Troubleshooting (known warnings)

| You see | Meaning | What to do (from the code / existing docs) |
|---------|---------|--------------------------------------------|
| Wizard: “Python is missing” / python.org link | No usable 3.11+ interpreter | Install Python 3.11+ from [python.org/downloads](https://www.python.org/downloads/). Set `CLIPPY_PYTHON` if it is not `python3` on PATH. The wizard will not `apt-get install` Python. |
| Wizard: “Ollama is not installed” / install from ollama.com | `ollama` not on PATH | Install with `curl -fsSL https://ollama.com/install.sh \| sh`, or click **Install** in the wizard (needs sudo). See [https://ollama.com/download/linux](https://ollama.com/download/linux) and [usage.md](usage.md#ollama-not-found-after-install). |
| `[platform_support] xdotool not found; window metadata will be limited` | No `xdotool` | Install `xdotool`. Titles and focused-window crop stay limited until it is on PATH. |
| `[platform_support] Linux idle detection unavailable (xprintidle or xdotool not found)` | Neither idle helper found | Install `xprintidle` and/or `xdotool`. |
| `[capture] Linux Wayland screenshots are not supported yet…` | Session is Wayland (or no `DISPLAY`) | **Wayland is not supported.** Log in to an X11 session. Do not rely on `grim`. |
| `[capture] screenshot grab returned a blank frame; skipping` | Grab was empty/all-black | Typical for XWayland-only grabs. Blank frames are not stored. |
| `[accessibility_text] pyatspi not available; install python3-pyatspi…` | `import pyatspi` failed | `apt install python3-pyatspi`. Venv: `--system-site-packages` or `python3-gi`. |
| `[accessibility_text] AT-SPI desktop not available (accessibility may be disabled)` | `Registry.getDesktop(0)` was `None` | Desktop accessibility may be off. **Not verified yet** how to enable it on a given DE. |
| `[accessibility_text] AT-SPI bus not available` / `AT-SPI query timed out` | D-Bus / timeout while walking the tree | Capture continues without that text. |
| AppImage will not execute | Not executable, or FUSE missing | `chmod +x` the file. If `libfuse2` is missing, run with `--appimage-extract-and-run`. For FUSE, see the AppImage section. |
| Unsigned binary warning from the desktop | Expected | The AppImage is unsigned. |

App stuck on the opening screen, retrying a wizard step, and resetting setup: [usage.md](usage.md#installation-troubleshooting).
