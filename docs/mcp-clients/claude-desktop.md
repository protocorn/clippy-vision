# Connect Clippy Vision to Claude Desktop

Clippy Vision has no chat box of its own. You ask about your activity from a connected MCP client — this page walks through Claude Desktop from start to finish.

Before you start:

- Clippy Vision is installed and has run its setup wizard.
- Claude Desktop is installed.

If the app is not connected yet, start with the overview in [mcp.md](../mcp.md).

---

## 1. Let Clippy write the config for you

Open Clippy Vision → **Settings → Connect apps**.

The MCP panel shows a preview of the config and a **Claude Desktop** row with a **Connect** button. Click it. Clippy writes the `clippy-vision` entry into Claude Desktop's config file on this machine, using this computer's real Python path and data folder — nothing to copy by hand.

If the row says the MCP launcher is not ready, restart Clippy Vision so it re-detects Python, then try again.

If you would rather edit the file yourself, click **Copy JSON** and use the manual steps below.

> Connecting is all or nothing: a connected client may call every Clippy tool, and whatever it sends to a cloud model leaves your machine. Disconnect the app to stop. There is no second switch. See [usage.md — Privacy settings](../usage.md#privacy-settings).

---

## 2. Or add the entry by hand

Claude Desktop reads its MCP servers from `claude_desktop_config.json`:

| Platform | Path |
|----------|------|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |

On Windows, paste `%APPDATA%\Claude` into the File Explorer address bar to jump straight to the folder. If the file does not exist yet, create it.

Paste the Clippy Vision entry under `mcpServers`. Keep any servers already listed there:

```json
{
  "mcpServers": {
    "clippy-vision": {
      "command": "C:\\Users\\YOU\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": [
        "C:\\Users\\YOU\\AppData\\Local\\Programs\\Clippy Vision\\resources\\clippy\\mcp_server.py"
      ],
      "env": {
        "CLIPPY_DATA_DIR": "C:\\Users\\YOU\\AppData\\Roaming\\Clippy Vision\\data",
        "CLIPPY_EMBEDDING_MODEL_DIR": "C:\\Users\\YOU\\AppData\\Roaming\\Clippy Vision\\models\\embeddings\\all-MiniLM-L6-v2",
        "CLIPPY_OLLAMA_URL": "http://127.0.0.1:11434",
        "PYTHONPATH": "C:\\Users\\YOU\\AppData\\Local\\Programs\\Clippy Vision\\resources\\clippy",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

The paths above are placeholders. Do not guess them — the ones on your machine differ, and the config only works with absolute paths. **Copy JSON** in Clippy's Connect apps panel builds this exact block from your installation, so prefer that, or copy the paths it prints.

In JSON, Windows backslashes are escaped as `\\`. On macOS the entry uses ordinary forward slashes:

```json
{
  "mcpServers": {
    "clippy-vision": {
      "command": "/usr/bin/python3",
      "args": ["/Applications/Clippy Vision.app/Contents/Resources/clippy/mcp_server.py"],
      "env": {
        "CLIPPY_DATA_DIR": "/Users/YOU/Library/Application Support/Clippy Vision/data",
        "CLIPPY_EMBEDDING_MODEL_DIR": "/Users/YOU/Library/Application Support/Clippy Vision/models/embeddings/all-MiniLM-L6-v2",
        "CLIPPY_OLLAMA_URL": "http://127.0.0.1:11434",
        "PYTHONPATH": "/Applications/Clippy Vision.app/Contents/Resources/clippy",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

The macOS paths are placeholders too — including `command`. Clippy resolves whatever `which python3` returns on your machine, which is often `/opt/homebrew/bin/python3` if you installed Python with Homebrew; `/usr/bin/python3` is just Apple's bundled stub. Use the real path from **Copy JSON**.

---

## 3. Restart Claude Desktop

Claude Desktop reads the config once at startup, so a running app will not see a new server.

Fully quit — on Windows, right-click the tray icon and choose **Quit**, not just closing the window; on macOS, **Cmd+Q**. A window closed to the tray keeps the old config loaded. Then reopen Claude Desktop.

---

## 4. Check the tools appear

In Claude Desktop, look for the tools or connectors icon (the plug / hammer symbol in the message box) and open it. `clippy-vision` should be listed with its tools — `search_sessions_tool`, `search_events_tool`, `recall_memory_tool`, and the rest.

If nothing is listed:

- Confirm the config file is valid JSON — a trailing comma or a missing brace makes Claude ignore it silently. Claude Desktop writes MCP diagnostics to `%APPDATA%\Claude\logs\mcp*.log` on Windows and `~/Library/Logs/Claude/mcp*.log` on macOS; check there for spawn errors.
- Confirm Clippy Vision is running and has finished setup; the server is spawned on demand but needs the data folder to exist.
- Confirm the Python path in `command` really exists on this machine.
- Claude Desktop also has a **Settings → Developer** screen listing configured MCP servers; check there if the tools icon is empty.

---

## 5. Try it

Ask in your own words — the agent picks which Clippy tool to call.

- "What did I work on yesterday afternoon?"
- "Which hotels did I look at yesterday?"
- "What was the error I ran into in my terminal this morning?"

A good answer names the apps and the time window, not just a topic. If it says there is nothing recorded, check that capture was actually running in Clippy Vision's timeline for that period — pause and stop states are visible there.

---

## When you are done

To disconnect, use **Disconnect** in Clippy's Connect apps panel (it removes the entry from Claude's config), or delete the `clippy-vision` block from `claude_desktop_config.json` yourself. Fully quit and reopen Claude Desktop either way.

More on which tools exist and what they return: [mcp.md — Tool reference](../mcp.md#tool-reference).
