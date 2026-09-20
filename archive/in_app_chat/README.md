# Archived: in-app chat UI

The default Clippy Vision shell no longer shows chat. Capture + MCP (Cursor /
Claude / etc.) remain the way to ask questions about activity. Insight cards
(Day Card + Threads) are the home surface instead.

## Why archived (not deleted)

We may want the composer / conversation drawer again later. Keep this folder
intact; do not rely on it from the live app.

## Contents

| File | Was |
|------|-----|
| `chat.js` | `electron-ui/src/js/chat.js` — composer, streaming, welcome screen |
| `conversations.js` | `electron-ui/src/js/conversations.js` — chat history drawer |
| `chat-view.fragment.html` | Markup that lived inside `#chat-view` in `index.html` |

## Backend notes

- `POST /chat` and `POST /chat/stream` still exist but only return MCP guidance
  (`chat_disabled: true`). See comments in `api_server.py`.
- Conversation list/search/delete APIs remain for Settings data-clear and a
  possible restore; the UI no longer calls them.
- Preload still exposes `clippy.chat` / `clippy.chatStream` / conversation
  helpers for restore; marked archived in comments.

## Restore checklist

1. Copy JS back under `electron-ui/src/js/`.
2. Re-insert `chat-view.fragment.html` as `#chat-view` and wire header Chats /
   New Chat buttons.
3. Point `app.js` / `utils.js` / `dom.js` at chat as the default panel again.
4. Re-enable real agent wiring in `/chat` if desired (currently stubbed).
