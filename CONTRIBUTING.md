# Contributing to Clippy Vision

Thanks for your interest in contributing! Clippy Vision is an open-source project that welcomes contributions from the community.

## Ways to Contribute

- **Bug Reports**: Found a bug? Open an issue with details on how to reproduce it
- **Feature Requests**: Have an idea? Open an issue describing the feature and use case
- **Code Contributions**: Want to implement a feature or fix a bug? Submit a pull request
- **Documentation**: Help improve the docs, guides, or code comments
- **Testing**: Test the software on different systems and report issues
- **Design / ideas / reviews**: UI polish, architecture notes, thoughtful PR reviews — all count

Every merged contribution is credited on the README [Contributors](README.md#contributors) wall (avatar, profile, contribution types, and lines of code). We follow the [All Contributors](https://allcontributors.org/) spec so non-code work is celebrated too.

### Getting credited

After your PR merges (or you’ve helped in another way), ask for credit on the PR or any issue:

```text
@all-contributors please add @your-username for code
```

Valid types include `code`, `doc`, `bug`, `ideas`, `design`, `test`, `review`, `maintenance`, and more — see the [emoji key](https://allcontributors.org/docs/en/emoji-key).

Maintainers: install the [All Contributors GitHub App](https://github.com/apps/allcontributors) on this repo so those comments open a credit PR automatically. Line-of-code stats refresh via `.github/workflows/update-contributors.yml`.

## Issue Format

When opening an issue (especially bug reports or scoped tasks), please use this structure so contributors can quickly tell what's involved:

**Title:** Short, specific, describes the outcome not the symptom

**Labels:** e.g. `good first issue`, `help wanted`, `bug`, `electron-ui`, `documentation`

**Context:** What exists today, what's broken or missing, and why it matters. Link relevant files/functions.

**Task:** What needs to happen, in plain terms.

**Hints:** (optional) Pointers on approach, gotchas, relevant API shapes, or patterns to follow elsewhere in the codebase.

**Acceptance criteria:** A checklist. Someone should be able to look at this and know exactly when the issue is done.

**How to test:** (optional, recommended for anything touching runtime behavior) Steps to verify the fix actually works.

**Difficulty:** Rough skill/time estimate, e.g. "Easy, docs only" or "Easy-medium, ~1-2 hrs, no ML knowledge needed"

This isn't mandatory for every issue, quick bug reports don't need the full template, but for anything tagged `good first issue`, please fill out Context, Task, and Acceptance criteria at minimum.

## Development Setup

Clippy Vision supports Windows and macOS. The app lives in `electron-ui/` and starts the Python API for you.

1. Fork the repository
2. Clone your fork:
   ```powershell
   git clone https://github.com/yourusername/clippy-vision.git
   cd clippy-vision
   ```
3. Install and run the Electron app:
   ```powershell
   cd electron-ui
   npm install
   npm start
   ```
   The setup wizard runs on first launch (Python, Ollama, models, `requirements.txt`).
4. Create a branch: `git checkout -b feature/your-feature-name`

### Dev app vs installed app

`npm start` launches **Clippy Vision (dev)** — a separate app from the installer build.

| | Installed (Start Menu) | `npm start` |
|--|--|--|
| Window / tray label | Clippy Vision | Clippy Vision (dev) |
| Activity data | `%APPDATA%\Clippy Vision\data` | `<repo>\core\data` |
| Single-instance lock | Own lock | Own lock |

Both can run at the same time. If `npm start` exits immediately, a previous **dev** instance is still in the system tray — right-click it → Quit, then start again. The terminal prints which instance is running (`[clippy] starting Clippy Vision (dev)`).

For Python-only work outside the app, you can also install deps with `pip install -r requirements.txt` from the repo root.

See [QUICKSTART.md](QUICKSTART.md) for installer and troubleshooting details.

### Lower-spec machines

Current `main` uses accessibility text + OCR for capture and does not download a vision model in setup. Only the chat text model is required by default (`qwen3:8b`). If that is still heavy on your machine, set a smaller chat model in the setup wizard.

## Code Style

- Follow PEP 8 for Python code
- Use meaningful variable and function names
- Add docstrings to functions and classes
- Keep functions focused and single-purpose
- Comment complex logic, but prefer self-documenting code

## Testing

Before submitting a PR:

1. Run the installation test: `python test_installation.py`
2. Test your changes with real usage
3. Ensure no new linter errors are introduced
4. Test on a clean database if modifying storage/schema

## Pull Request Process

1. Update the README.md or QUICKSTART.md if needed
2. Update requirements.txt if you added dependencies
3. Write a clear PR description explaining:
   - What problem does this solve?
   - What changes were made?
   - How was it tested?
4. Link any related issues
5. Be responsive to code review feedback

## Areas That Need Help

### High Priority
- [ ] Cross-OS support
  - [x] macOS support
  - [ ] Linux support
- [ ] Wire Settings → Access control UI to the existing privacy API (see open `good first issue`s)
- [ ] Automated tests for classification pipeline
- [ ] Performance optimization for vision processing
- [ ] Better error handling and logging (e.g. surface API `detail` in the UI)

### Medium Priority
- [ ] Docker/containerization support
- [ ] Alternative model support (LLaMA, Mistral, etc.)
- [ ] Web UI for database exploration
- [ ] Export/import conversation history
- [ ] Configurable retention policies

### Low Priority
- [ ] Plugin system for custom tools
- [ ] Integration with other productivity apps
- [ ] Alternative database backends
- [ ] Cloud sync option (with E2E encryption)

## Questions?

Open an issue or discussion on GitHub. I try to respond within 24-48 hours.

## Code of Conduct

- Be respectful and constructive
- Focus on the technical merit of ideas
- Welcome newcomers and help them learn
- Assume good intent

Happy coding! 🚀
