# MCP Guide

Clippy Vision is the capture and memory layer. You ask about your activity through a connected MCP client (Cursor, Claude Desktop, VS Code, or any other MCP app). There is no in-app chat. The agent chooses which tool to call; Clippy does not classify the question.

For day-to-day capture and privacy controls, see [usage.md](usage.md).

---

## Access permissions

Connecting is all or nothing. A connected app may call every Clippy tool listed below. A cloud model receives the replies those tools return. Disconnect the app to stop. There is no second switch.

The same privacy redactor that runs before storage runs again on every tool reply. Screenshot file paths are left out. A window you marked private is returned as "This moment was private" with the app and time only. If that filter fails, the tool returns nothing. The copy on this computer is unchanged.

---

## Connecting a client

Open **Settings → Connect apps**.

### One-click clients

**Cursor**, **Claude Desktop**, and **VS Code** have a Connect button. It writes the config on this machine.

### Copy JSON (any other MCP app)

**Copy JSON** copies a config built from this computer's Python path and data folder. Paste that into any other MCP app. It is not a config from the developer's machine.

### Other apps

**Also connect with other apps** lists clients that use the same copied JSON. Each row opens that app's docs for a local stdio server. Choose **Hide other apps** to collapse the list.

| App | Where to paste | Docs |
|-----|----------------|------|
| Devin Desktop | `mcp_config.json` (this app was Windsurf) | [Devin MCP](https://docs.devin.ai/cli/extensibility/mcp/configuration) |
| Claude Code | Local stdio server (follow the docs command) | [Claude Code MCP](https://code.claude.com/docs/en/mcp#option-3-add-a-local-stdio-server) |
| Cline | Cline's MCP settings | [Cline MCP](https://docs.cline.bot/mcp/mcp-overview#local-server-stdio) |
| Roo Code | Roo's MCP config | [Roo Code MCP](https://roocodeinc.github.io/Roo-Code/features/mcp/using-mcp-in-roo/#stdio-transport) |
| Continue | `.continue/mcpServers` | [Continue MCP](https://docs.continue.dev/customize/deep-dives/mcp#how-to-use-standard-inputoutput-stdio) |
| JetBrains | AI Assistant MCP dialog | [JetBrains MCP](https://www.jetbrains.com/help/ai-assistant/mcp.html#connect-to-an-mcp-server) |
| Kiro | Kiro's MCP config | [Kiro MCP](https://kiro.dev/docs/mcp/) |
| LM Studio | `mcp.json` | [LM Studio MCP](https://lmstudio.ai/docs/app/mcp) |

Timeline, privacy, and capture controls stay in the Clippy desktop app.

---

## Tool reference

| MCP tool | Description |
|------|-------------|
| `search_sessions_tool` / `search_events_tool` | NL search (sessions / raw events) |
| `search_bounded_tool` | Keyword search with explicit `start`/`end` + pagination |
| `list_sessions_tool` | Chronological deduped sessions for a time window |
| `activity_coverage_tool` | Hourly event counts (honest capture gaps) |
| `app_time_summary_tool` | Approximate per-app time in a window |
| `list_urls_tool` | Distinct URLs (optional pattern filter) |
| `list_screenshots_tool` / `get_screenshot_tool` | Frames when on disk; OCR backup after adaptive TTL |
| `recall_memory_tool` / `fetch_cluster_tool` | Long-term memory (freshness-ranked; hides low-freshness clusters by default) |
| `save_identity_tool` / `save_note_tool` / `delete_note_tool` | Explicit memory writes |
| `remember_turn_tool` | Local extractor stores facts from the user's own message |
| `find_files_tool` / `list_workspace_roots_tool` | Trusted filesystem path recall (host opens files/URLs) |

### Example questions

Ask in the connected app, in your own words:

- "What was I working on before lunch?"
- "Which hotels did I look at yesterday?"
- "What was the error I encountered in my terminal?"
