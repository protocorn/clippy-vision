# Clippy Vision

Clippy Vision watches your work on this computer, stores a searchable activity history locally, and lets you ask about it through a connected MCP client such as Cursor, Claude Desktop, or VS Code. The desktop app captures, summarizes, and shows the timeline.

![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux%20(experimental)-blue)
![License](https://img.shields.io/badge/license-AGPL--3.0-blue)
![Models](https://img.shields.io/badge/models-Ollama%20local-orange)
![MCP](https://img.shields.io/badge/MCP-Cursor%20%7C%20Claude%20Desktop%20%7C%20VS%20Code-purple)
[![All Contributors](https://img.shields.io/github/all-contributors/protocorn/clippy-vision?color=ee8449&style=flat-square)](#contributors)
[![Open Source Helpers](https://www.codetriage.com/protocorn/clippy-vision/badges/users.svg)](https://www.codetriage.com/protocorn/clippy-vision)

<p align="center">
  <img src="assets/clippy_vision_demo.png" alt="Explain less. Ask more. — an agent answering a LeetCode question from remembered screen activity, with nothing pasted into the chat" width="720" />
</p>

Ask in a connected MCP client, for example:

- "What was I working on before lunch?"
- "Which hotels did I look at yesterday?"
- "What was the error I encountered in my terminal?"

---

## Download

**Windows is tested for 2.0.** macOS validation is pending, so macOS 2.0 installers are not included yet. macOS downloads below are the previous release (**v1.3.1**). Linux is **experimental and X11 only**; an unsigned x86_64 AppImage is built in CI (see [docs/linux.md](docs/linux.md)). The README describes current `main`; some installer features differ from 1.3.1.

<p align="center">
  <a href="https://github.com/protocorn/clippy-vision/releases/download/v2.0.0/ClippyVision-Windows-Setup-2.0.0.exe"><img src="https://img.shields.io/badge/Download-Windows%20v2.0.0-0078D6?style=for-the-badge&logo=windows&logoColor=white" alt="Download for Windows v2.0.0" /></a>
</p>

<p align="center">
  <a href="https://github.com/protocorn/clippy-vision/releases/download/v1.3.1/ClippyVision-macOS-arm64-1.3.1.dmg"><img src="https://img.shields.io/badge/Download-macOS%20Apple%20Silicon%20v1.3.1%20(previous)-555555?style=for-the-badge&logo=apple&logoColor=white" alt="Download for macOS Apple Silicon v1.3.1 (previous release)" /></a>
  &nbsp;
  <a href="https://github.com/protocorn/clippy-vision/releases/download/v1.3.1/ClippyVision-macOS-x64-1.3.1.dmg"><img src="https://img.shields.io/badge/Download-macOS%20Intel%20v1.3.1%20(previous)-555555?style=for-the-badge&logo=apple&logoColor=white" alt="Download for macOS Intel v1.3.1 (previous release)" /></a>
</p>

<p align="center">
  <a href="https://github.com/protocorn/clippy-vision/releases/latest">All releases &amp; older versions</a>
  &nbsp;·&nbsp;
  <a href="https://github.com/protocorn/clippy-vision/commits/main"><img src="https://img.shields.io/github/last-commit/protocorn/clippy-vision?style=flat-square&label=last%20commit" alt="Last commit" /></a>
  &nbsp;
  <a href="https://github.com/protocorn/clippy-vision/releases/latest"><img src="https://img.shields.io/github/release-date/protocorn/clippy-vision?style=flat-square&label=latest%20release" alt="Latest release date" /></a>
</p>

The installers are unsigned: on Windows choose **More info → Run anyway**, and on macOS right-click → **Open** the first time. First run needs internet for an app-managed Python runtime (if you do not already have Python 3.11+), Python packages, and the default summary model (`qwen3:8b`, about 5 GB). `llama3.2:3b` is a low-memory option that is less accurate. You can pick a different model later in Settings.

**Trying Clippy?** [Share feedback (5 min)](https://docs.google.com/forms/d/e/1FAIpQLScqsgeWGLdUS_Ba0Me0LPQH8QRrwnIgGiGGHwLElVuqIpfcxQ/viewform).

### Requirements

| | Minimum | Recommended |
|--|---------|-------------|
| OS | Windows 10 / 11 (64-bit), macOS 12+, or Linux (Ubuntu 22.04+, experimental, X11 only) | Windows 11 or a current macOS |
| System RAM | 8 GB | 16 GB |
| Graphics memory | Not required. Integrated graphics or Apple unified memory is enough | 4 GB+ dedicated on Windows, or 16 GB unified memory on Apple silicon |
| Free disk | 8 GB | 10 GB+ |
| **Python runtime** | No system Python required for the packaged app. Setup downloads a relocatable CPython 3.12 if needed. | **Python 3.12** (app-managed or `CLIPPY_PYTHON`) |

**Python is not inside the Windows installer, Linux AppImage, or macOS package** (keeps those artifacts around 100–130 MB). First-run setup downloads a relocatable [python-build-standalone](https://github.com/astral-sh/python-build-standalone) CPython 3.12 into app data and then creates the same app-managed venv as before. `requirements.txt` does not pin a Python version. Resolution order (`electron-ui/electron/lib/paths.js`): `CLIPPY_PYTHON` if set, else a healthy app venv, else the downloaded runtime, else `python3` / `python` from PATH. CI unit tests and the release dry-run still install Python 3.12 on the runner and set `CLIPPY_PYTHON`. A separate `managed-python-venv` job on Ubuntu, Windows, and macOS (arm64 + Intel x64) downloads the pinned standalone CPython, verifies SHA-256, and creates a venv. The setup wizard parses `python --version` and requires **Python 3.11+**.

**When Python is missing, the setup wizard (`electron-ui/electron/lib/setup.js`) does this:**
- It parses `--version` and requires Python 3.11+. A healthy system 3.11+ interpreter is used when present so the extra download is skipped.
- If nothing suitable is found (or PATH Python is older than 3.11), it **downloads a relocatable CPython 3.12** (~25–30 MB) with a progress bar, verifies the SHA-256, and extracts it under app data (`python-runtime/`). No winget, Homebrew, or apt install of system Python.
- Packages still go into the app-managed venv (`python-venv/`). Linux `python3-venv` / Ollama `zstd` error handling is unchanged.
- If the download fails, Retry and an optional [python.org/downloads](https://www.python.org/downloads/) link. Advanced: set `CLIPPY_PYTHON`.

**Advanced:** point Clippy at a specific interpreter before launch:

```powershell
$env:CLIPPY_PYTHON = "C:\path\to\python.exe"
```

```bash
export CLIPPY_PYTHON=/path/to/venv/bin/python3
```

Linux support is **experimental and X11 only. Wayland is not supported.** On X11, screenshots use `mss` (optional crop via `xdotool`). Window titles use `xdotool`, clipboard uses `wl-paste`/`xclip`/`xsel`. AT-SPI accessibility text is implemented; it is covered by CI unit tests, not a real desktop. An unsigned Linux AppImage is built in CI. Install and run steps: [docs/linux.md](docs/linux.md).

The setup wizard checks these numbers before installing. Below minimum → setup is blocked. Between minimum and recommended → you can continue with a warning that summaries may feel slower. Homebrew, dependency, and permission troubleshooting: [docs/usage.md](docs/usage.md#installation-troubleshooting). Lower-spec machines: [CONTRIBUTING.md](CONTRIBUTING.md#lower-spec-machines).

---

## Quick Start

1. Download and install Clippy. You do not need to install Python first.
2. Complete the setup wizard (Ollama if missing, then Python packages). Setup downloads `qwen3:8b` automatically for summaries. `llama3.2:3b` is a low-memory option that is less accurate; change the model later in Settings.
3. Leave Clippy running to build your activity history.
4. Open **Settings → Connect apps**, connect your agent, and ask about your activity.

More detail: [docs/usage.md](docs/usage.md) · [docs/mcp.md](docs/mcp.md) · client guides: [Claude Desktop](docs/mcp-clients/claude-desktop.md) · [LM Studio](docs/mcp-clients/lm-studio.md)  
Build from source: [CONTRIBUTING.md](CONTRIBUTING.md#building-from-source)

---

## Features

- Searchable activity history across apps
- Local summaries and long-term memory
- Access through MCP-compatible agents
- A timeline for reviewing and deleting activity
- Capture and privacy controls

---

## Privacy

- Clippy captures foreground windows, clipboard, typing metrics, and screenshots.
- Capture, summarization, and storage run locally on this computer.
- Connected cloud agents can receive retrieved memory through MCP tool replies.
- Connected clients have access to all exposed Clippy tools.
- Redaction is best effort. Private-browsing detection is on by default: Chrome, Edge, and Brave on Windows and macOS; Firefox and Opera on Windows (and attempted but untested on macOS); Vivaldi on Windows. Vivaldi, Safari, and Linux are not covered. See the [coverage table](docs/usage.md#private-browsing).
- You can pause capture and delete stored activity from the tray or timeline.

Full guide (retention defaults and detailed behavior): [docs/usage.md](docs/usage.md#privacy-settings).

<p align="center">
  <img src="assets/instagram-redaction.jpg" alt="Instagram login with the password field blacked out in a Clippy capture" width="720" />
</p>

---

## Documentation & Roadmap

| Guide | Contents |
|-------|----------|
| [docs/usage.md](docs/usage.md) | Capture controls, privacy, accessibility, install troubleshooting |
| [docs/linux.md](docs/linux.md) | Experimental Linux install and run (X11 only; AppImage, first-run Python download, capture tools) |
| [docs/mcp.md](docs/mcp.md) | Supported clients, Copy JSON, tool reference, access permissions |
| [docs/mcp-clients/claude-desktop.md](docs/mcp-clients/claude-desktop.md) | Step-by-step Claude Desktop setup: config location, restart, verification |
| [docs/mcp-clients/lm-studio.md](docs/mcp-clients/lm-studio.md) | Step-by-step LM Studio setup: MCP configuration, local model, test query, privacy |
| [docs/architecture.md](docs/architecture.md) | Tech stack, capture pipeline, summarization, distillation, schema |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Build from source and contribution guide |
| [PROJECT_VISION.md](PROJECT_VISION.md) | Roadmap, principles, and known limitations |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup steps, building from source, and good first issues.

---

## Contributors

Thanks to everyone who has shipped code, docs, design, or ideas.

[![All Contributors](https://img.shields.io/github/all-contributors/protocorn/clippy-vision?color=ee8449&style=flat-square)](#contributors)
[![Contributors](https://img.shields.io/github/contributors/protocorn/clippy-vision?style=flat-square)](https://github.com/protocorn/clippy-vision/graphs/contributors)

<!-- CONTRIBUTORS-STATS:START -->

| | Contributor | What they built |
| :---: | :--- | :--- |
| <a href="https://github.com/protocorn"><img src="https://avatars.githubusercontent.com/u/53559317?v=4" width="64" height="64" alt="protocorn"/></a> | <a href="https://github.com/protocorn"><b>@protocorn</b></a><br/><sub>💻 📖 🎨 🤔 🚧</sub> | Designed the core app: agent, vision pipeline, memory system, and the Electron desktop shell. |
| <a href="https://github.com/rusetiq"><img src="https://avatars.githubusercontent.com/u/234747645?v=4" width="64" height="64" alt="rusetiq"/></a> | <a href="https://github.com/rusetiq"><b>@rusetiq</b></a><br/><sub>💻 📦</sub> | Brought Clippy Vision to macOS: native screen capture, permissions, and Apple Silicon + Intel packaging. |
| <a href="https://github.com/cursoragent"><img src="https://avatars.githubusercontent.com/u/199161495?v=4" width="64" height="64" alt="cursoragent"/></a> | <a href="https://github.com/cursoragent"><b>@cursoragent</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=cursoragent">See their commits →</a> |
| <a href="https://github.com/ABarpanda"><img src="https://avatars.githubusercontent.com/u/145291762?v=4" width="64" height="64" alt="ABarpanda"/></a> | <a href="https://github.com/ABarpanda"><b>@ABarpanda</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=ABarpanda">See their commits →</a> |
| <a href="https://github.com/Afrin-banu-A"><img src="https://avatars.githubusercontent.com/u/263189865?v=4" width="64" height="64" alt="Afrin-banu-A"/></a> | <a href="https://github.com/Afrin-banu-A"><b>@Afrin-banu-A</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=Afrin-banu-A">See their commits →</a> |
| <a href="https://github.com/vitorparras"><img src="https://avatars.githubusercontent.com/u/43687831?v=4" width="64" height="64" alt="vitorparras"/></a> | <a href="https://github.com/vitorparras"><b>@vitorparras</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=vitorparras">See their commits →</a> |
| <a href="https://github.com/Furox-Art"><img src="https://avatars.githubusercontent.com/u/177975472?v=4" width="64" height="64" alt="Furox-Art"/></a> | <a href="https://github.com/Furox-Art"><b>@Furox-Art</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=Furox-Art">See their commits →</a> |
| <a href="https://github.com/vaishn4vi"><img src="https://avatars.githubusercontent.com/u/150888364?v=4" width="64" height="64" alt="vaishn4vi"/></a> | <a href="https://github.com/vaishn4vi"><b>@vaishn4vi</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=vaishn4vi">See their commits →</a> |
| <a href="https://github.com/adity982"><img src="https://avatars.githubusercontent.com/u/59918965?v=4" width="64" height="64" alt="adity982"/></a> | <a href="https://github.com/adity982"><b>@adity982</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=adity982">See their commits →</a> |
| <a href="https://github.com/Draoui-Haroun"><img src="https://avatars.githubusercontent.com/u/266044395?v=4" width="64" height="64" alt="Draoui-Haroun"/></a> | <a href="https://github.com/Draoui-Haroun"><b>@Draoui-Haroun</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=Draoui-Haroun">See their commits →</a> |
| <a href="https://github.com/KeuqS"><img src="https://avatars.githubusercontent.com/u/50778230?v=4" width="64" height="64" alt="KeuqS"/></a> | <a href="https://github.com/KeuqS"><b>@KeuqS</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=KeuqS">See their commits →</a> |
| <a href="https://github.com/shaurya703"><img src="https://avatars.githubusercontent.com/u/153742516?v=4" width="64" height="64" alt="shaurya703"/></a> | <a href="https://github.com/shaurya703"><b>@shaurya703</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=shaurya703">See their commits →</a> |
| <a href="https://github.com/cyforkk"><img src="https://avatars.githubusercontent.com/u/165913369?v=4" width="64" height="64" alt="cyforkk"/></a> | <a href="https://github.com/cyforkk"><b>@cyforkk</b></a><br/><sub>💻</sub> | Made errors readable: replaced bare HTTP status codes with real API error messages in chat. |
| <a href="https://github.com/tadaokatzuki"><img src="https://avatars.githubusercontent.com/u/246784086?v=4" width="64" height="64" alt="tadaokatzuki"/></a> | <a href="https://github.com/tadaokatzuki"><b>@tadaokatzuki</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=tadaokatzuki">See their commits →</a> |
| <a href="https://github.com/icn5381"><img src="https://avatars.githubusercontent.com/u/255778606?v=4" width="64" height="64" alt="icn5381"/></a> | <a href="https://github.com/icn5381"><b>@icn5381</b></a><br/><sub>💻</sub> | <a href="https://github.com/protocorn/clippy-vision/commits?author=icn5381">See their commits →</a> |
<!-- CONTRIBUTORS-STATS:END -->

How to get on this wall: [CONTRIBUTING.md](CONTRIBUTING.md#contribution-types).

---

## License

AGPL-3.0. See [LICENSE](LICENSE). Releases already published through v1.3.1 stay under MIT. A commercial license for companies that cannot use AGPL is not written yet.
