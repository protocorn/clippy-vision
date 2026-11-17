# macOS install and run (unsigned CI builds)

macOS capture, permissions, and setup paths are implemented in this repository. **Unsigned** Apple Silicon (`arm64`) and Intel (`x64`) disk images and zips are built on GitHub-hosted `macos-latest` runners in [`.github/workflows/release.yml`](../.github/workflows/release.yml). They are **not notarized** and use **no Apple signing secrets**.

Published GitHub Release macOS installers may still be an older version until a maintainer attaches a current draft. CI artifacts from the Build & Publish Release workflow are the current unsigned 2.x packages.

Day-to-day capture and privacy: [usage.md](usage.md). Connecting an agent: [mcp.md](mcp.md). Building from source: [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Supported / tested scope

| | What the repo says |
|--|--------------------|
| Stated OS | macOS 12+ (`electron-ui/electron/lib/hardware.js`, setup copy: `macOS 12+`) |
| Architecture | **arm64** (Apple Silicon) and **x64** (Intel). `electron-ui/package.json` `build.mac.target` produces `dmg` and `zip` for both. Artifact name: `ClippyVision-macOS-${arch}-${version}.{dmg,zip}`. |
| Signing | Unsigned. `CSC_IDENTITY_AUTO_DISCOVERY=false`. `forceCodeSigning`, `gatekeeperAssess`, and `notarize` are false. `--publish never`. |
| **CI (packaging)** | Job `build-macos` on `macos-latest` (typically Apple Silicon). Builds both archs. Smoke-checks that each arch has a dmg or zip larger than 40MB. Launch-tests the **runner-native** zip: setup pre-seeded, Ollama stubbed, `CLIPPY_PYTHON` pointed at the runner’s Python 3.12, Screen Recording/Accessibility prompts skipped (`CLIPPY_SKIP_MAC_PERMISSIONS`). Proves the packaged app can spawn the backend and `GET /health` returns 200. **Does not prove** the setup wizard, Ollama, a real model, Screen Recording, Accessibility, or a real Mac desktop. |
| **CI (unit)** | Electron path tests (`mcp.test.js`, `setup.test.js`, `macos-permissions.test.js`, `macos-build.test.js`) run on `macos-latest`. Pytest still skips `@pytest.mark.macos` AX/AppleScript tests on GitHub-hosted macOS (those need a real session). |
| **Real desktop** | Not claimed by this CI job. Screen Recording, Accessibility, capture of real apps, and private-window coverage still need a person on Apple Silicon and Intel ([issue #55](https://github.com/protocorn/clippy-vision/issues/55)). |

---

## Download an unsigned build

1. Open the [Actions tab](https://github.com/protocorn/clippy-vision/actions) → **Build & Publish Release**.
2. Open a successful run. Download the **macos-installer** artifact.
3. Inside it, pick:
   - Apple Silicon: `ClippyVision-macOS-arm64-{version}.dmg` (or `.zip`)
   - Intel: `ClippyVision-macOS-x64-{version}.dmg` (or `.zip`)

If a maintainer has attached those files to a GitHub Release, you can use the release page instead.

Python is **not** bundled. Install Python 3.11+ and Ollama before or during first-run setup (the wizard uses Homebrew when Homebrew is already installed; otherwise it shows python.org and ollama.com links).

Packaged macOS `userData` is `~/Library/Application Support/Clippy Vision`. First-run setup writes `setup_complete.json` there. Until that file exists, the backend does not start.

---

## Open an unsigned app (Gatekeeper)

These builds are unsigned, so macOS Gatekeeper blocks a normal double-click the first time.

**From a `.dmg`:**

1. Open the disk image and drag **Clippy Vision** to **Applications**.
2. In Applications (or the disk image), **right-click** (or Control-click) **Clippy Vision** → **Open**.
3. Confirm **Open** in the dialog. You only need to do this once per install.

**From a `.zip`:**

1. Unzip, then move **Clippy Vision.app** to **Applications**.
2. Same right-click → **Open** the first time.

If macOS still refuses:

```bash
xattr -cr "/Applications/Clippy Vision.app"
```

Then right-click → **Open** again. Do not disable Gatekeeper globally.

**Windows-style “More info → Run anyway” does not exist on Mac.** Right-click → Open is the unsigned-app path.

---

## Screen Recording and Accessibility

On first launch the app asks for:

- **Screen Recording** — screenshots (`mss` / CoreGraphics)
- **Accessibility** — front-window title (AppleScript / System Events), password-field painting (`core/mac_ui.py`), and keyboard hooks (`pynput`)

Grant both when asked. If a sheet is dismissed, turn them on in **System Settings → Privacy & Security**.

After you grant **Screen Recording**, macOS often requires **fully quitting and reopening** Clippy Vision before frames are captured. Accessibility changes usually apply after the next prompt or a relaunch.

The unsigned package still shows those sheets because `Info.plist` includes `NSScreenCaptureUsageDescription` and `NSAccessibilityUsageDescription`.

---

## Python and Ollama on macOS

The setup wizard (`electron-ui/electron/lib/setup.js`):

- Parses `python --version` and requires **Python 3.11+**.
- Uses Homebrew `brew install python` / `brew install ollama` when `brew` is on PATH.
- If Homebrew is missing, shows [python.org/downloads](https://www.python.org/downloads/) and [ollama.com/download](https://ollama.com/download). It does not install Homebrew for you.

Known locations the wizard probes after a manual install:

- Ollama: `/opt/homebrew/bin/ollama`, `/usr/local/bin/ollama`, `/Applications/Ollama.app/Contents/Resources/ollama`
- Python: Homebrew prefixes, `/usr/bin/python3`, and python.org framework builds under `/Library/Frameworks/Python.framework/Versions/3.1x/bin/python3`

Packaged launches also prepend those directories onto PATH (`electron-ui/electron/lib/paths.js`) because the GUI app does not inherit your shell profile. Override with `CLIPPY_PYTHON` if needed:

```bash
export CLIPPY_PYTHON=/path/to/venv/bin/python3
```

---

## What works in code vs what is not claimed yet

Implemented (unit-tested and/or used on the macOS capture path):

- Front-window metadata via AppleScript (`core/platform_support.py`)
- Screenshots via `mss` (same scheduler as Windows)
- Accessibility redaction via AX / System Events (`core/mac_ui.py`)
- Capture opt-out for `NSWindowSharingNone` (`core/capture_optout.py`)
- Private-window detection for Chrome, Edge, and Brave (Firefox and Opera attempted, untested; Safari and Vivaldi not covered — [usage.md](usage.md#private-browsing))
- MCP config paths under `~/Library/Application Support` (`electron-ui/electron/lib/mcp.js`)

Not supported / not proven by CI:

- Apple notarization or Developer ID signing (unsigned only)
- A real-machine validation pass for 2.x ([issue #55](https://github.com/protocorn/clippy-vision/issues/55))
- Screen Recording or Accessibility on the GitHub-hosted runner (prompts are skipped in CI)
- Safari private browsing
- Shipping a published GitHub Release asset — that still needs a maintainer to attach the draft

---

## File locations (packaged macOS)

| Item | Path |
|------|------|
| App | `/Applications/Clippy Vision.app` |
| App data (DB, screenshots) | `~/Library/Application Support/Clippy Vision/data/` |
| Setup flag | `~/Library/Application Support/Clippy Vision/setup_complete.json` |
| API port | `~/Library/Application Support/Clippy Vision/api_process.json` |
| Ollama models | `~/.ollama/models/` |

A source run (`npm start`) stores the database in `core/data` inside the checkout. See [CONTRIBUTING.md](../CONTRIBUTING.md#dev-app-vs-installed-app).
