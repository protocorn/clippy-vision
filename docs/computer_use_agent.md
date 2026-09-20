# Clippy Vision: Memory → Action (Plan B)

> **Product line:** Clippy remembers what happened on your computer and can perform **general** actions on your behalf — not app-specific bots.

Status: **Active design + early implementation.**  
- Phase A idle “enough” done (OS idle + stretched background screenshots).  
- Plan B Steps 1–2 done: `core/os_actions.py` + `scripts/probe_os_actions.py`.  
- Plan B open tools live in `agent/tools.py` / `core/os_actions.py` (available for future MCP exposure). In-app ReAct chat (`agent/react_agent.py`) has been **removed** — ask via MCP + a bigger model.  
- **Trusted folders + `find_files`:** Clippy can search the real filesystem under user-approved roots (`core/workspace_roots.py`, `core/fs_search.py`), remember roots from chat/Settings, and open absolute paths. Activity DB is no longer used as a fake filesystem search.  
- Next: `open_app` / `focus_window`; confirm-on-ambiguous opens.  
- Next: chat dogfood (search → open on request); then confirm UX / `open_app` / `focus_app`.  
Proactive stuck-detection (`docs/stuck_detector.md`) remains a later policy layer on top of these capabilities.

---

## 1. What we are building

Not: “Clippy for Chrome” / “Clippy for VS Code” / per-app plugins as the core.

Yes:

> A local agent that **retrieves** from personal computer memory, **plans** with an LLM, and **acts** through general OS + UI primitives, then **verifies** the result.

App names appear in *memory* (what you used). Actions stay *general* (open a path, focus a process, click an a11y-named control wherever it is).

Granulated tools compose into tasks (open app → open file → … → click → verify). The platform is the tool belt + loop; Photoshop/Chrome/etc. are just apps the tools drive.

---

## 2. Why this fits Clippy

You already have the hard perception half:

```text
Computer → a11y / screenshot / OCR / window+URL context → memory
```

Plan B adds:

```text
Memory → retrieve → reason → action planner → general tools → observe → verify
```

The interesting product is **memory + execution**, not a better click-model than cloud computer-use agents. Clippy’s edge is *your* history of what you actually saw and did.

---

## 3. Non‑negotiable: general actions, not app integrations

| Prefer | Avoid (as the architecture) |
|---|---|
| `open_path`, `open_url`, `open_app`, `focus_window` | Hardcoded Chrome/Gmail/Slack flows |
| `click_element(role, name)` via a11y wherever the control is | “Click Download in Chrome at (x,y)” as the primary API |
| `type_text`, `press_keys`, `scroll` as primitives | Per-app macros as first-class product |
| OS APIs for alarms, volume, files when they exist | Driving Clock/Settings UIs when an API works |

**App-specific knowledge may appear in prompts or heuristics** (“browsers often expose URL in a11y”) but must not become a forest of adapters. If a capability only works in one app, it is a special case — not the platform.

Hierarchy of how to accomplish a goal:

```text
1. Memory retrieval (what / where)
2. Deterministic OS / shell / filesystem API
3. Accessibility tree (role + name + bounds)
4. OCR / screenshot as fallback perception
5. LLM plans which general tool to call
6. Pixel-click only as last resort
```

Same spirit as capture: cheap structured signal first, vision last.

---

## 4. How the agent knows what to open (memory → target)

**v0 does not index the whole disk.** The primary index is **activity memory** (what appeared on screen): titles, URLs, paths in Explorer/terminal/OCR/a11y, clipboard, session summaries.

```text
User question
    ↓
Retrieve (search_sessions / search_events — already exist)
    ↓
Extract candidates: urls, paths, soft names (“agent.py”)
    ↓
1 clear target → open_* ; many/weak → ask user
    ↓
open_path / open_url
    ↓
verify_foreground → ok | pending | failed
```

| Layer | Role |
|---|---|
| **A — Memory (v0)** | Find path/URL from what the user actually saw/did |
| **B — Filesystem (later)** | Resolve soft names under known roots if memory lacks a full path |
| **`os_actions`** | Only executes an already-chosen path/URL — does not decide *which* file |

“Last photo I opened” → memory. “Color grade how I usually do” → needs stored recipe / taught skill / past UI sequence later; not v0.

Optional later: **opt-in** `web_search` / `fetch_url` for docs when the local model is stuck — never upload screen dumps by default; map docs onto general tools + verify. Prefer personal memory over the web for “how *I* usually…”.

---

## 5. Core loop (stable — do not redesign for clicks later)

```text
        ┌──────────────┐
        │   Observe    │  current foreground + optional fresh a11y/OCR
        └──────┬───────┘
               ↓
        ┌──────────────┐
        │   Retrieve   │  sessions / events / facts
        └──────┬───────┘
               ↓
        ┌──────────────┐
        │    Reason    │  intent, candidates, plan
        └──────┬───────┘
               ↓
        ┌──────────────┐
        │    Action    │  general tool call(s)
        └──────┬───────┘
               ↓
        ┌──────────────┐
        │   Verify     │  code-side check (not “model said it worked”)
        └──────┬───────┘
               │
               └── fail/pending → replan, wait on user, or ask → Observe
```

Not: `LLM → click`.  
Yes: `retrieve → act → verify`.

**This same spine scales to GUI:** finer tools (`click_element`, …), same loop, shorter per-step context.

---

## 6. Capability layers (all general)

### L0 — Memory (mostly exists)

- `search_sessions` / `search_events` / notes / identity  

### L1 — OS utilities (in progress)

| Tool | Status |
|---|---|
| `open_path` | **Dropped from MCP/agent** — host opens paths; `core/os_actions.py` remains for probes only |
| `open_url` | **Dropped from MCP/agent** — same |
| `verify_foreground` / `open_*_and_verify` | **Done** (patient across Open-with) |
| `find_files` / `remember_workspace_root` / trusted folders | **Done** (`core/fs_search.py`, `core/workspace_roots.py`, Settings → Privacy) |
| `open_app` / `focus_app` / `reveal_in_folder` | Later |
| reminders / volume | Later, where OS APIs exist |

### L2 — Generic GUI primitives (later)

`find_elements`, `click_element`, `type_text`, `press_keys`, `scroll` — a11y-first, pixel last.

### L3 — Safety / UX

- Confirm before irreversible opens/actions (chat confirm — Step 3+)  
- Action audit (args + verify status)  
- Deny lists; user can disable action tools  

---

## 7. Local model constraints (Qwen3:8B-class / Ollama)

Clippy’s chat model is a **local ~8B** tool-calling model. That is viable for Plan B **if we constrain the harness**. It is not a frontier computer-use model.

### What research / practice says

- Small local models can do tool calling; **Qwen3 8B** ranks relatively well among &lt;10B models in practical evals (e.g. Docker’s local tool-calling writeups), but many 8B models fail often.
- Common failures: wrong tool, malformed/missing args, inventing paths, calling tools on greetings, **describing** a tool call in prose instead of emitting one, stopping after the first tool, ignoring tool results.
- After tool-1 output fills context, 7B/8B often lose structured tool format on later steps (widely reported with Ollama agents).
- Guidance of thumb: **8B works with few sharp tools + validation**; comfort tier for rich agents is often cited nearer **14–22B+**.
- Multi-agent swarms often **increase** tokens/cost a lot vs single-agent and add handoff errors; benefits shrink as the base model improves (empirical MAS vs SAS studies).
- For local/small models, **ReAct remains the most reliable default**; heavy plan-execute / ToT is fragile. Prefer **staged tool menus** and **fresh short contexts per phase** over one god-context.

### How Clippy should go wrong less often

| Practice | Why |
|---|---|
| Few tools exposed per phase | Reduces wrong-tool rate |
| Validate args in **code** before execute | Hallucinated paths never run |
| Verify in **code** | Model cannot self-declare success |
| Truncate tool results / prefetch caps | Already partly in ReAct (`MAX_STEPS`, prefetch char caps) |
| Cap steps / identical retries | Stops loops |
| Confirm ambiguous targets | Wrong open worse than no open |

**Do not** put 20+ GUI tools in one prompt on day one.

---

## 8. Target architecture: staged single agent (not a rewrite later)

### Decision

**Staged single-agent** (one product agent, phased tool allowlists, compressed state).  
**Not** a default multi-agent debate swarm.  
**Not** one endless ReAct with every tool and full a11y history forever.

```text
User goal
    ↓
Router / intent          (existing)
    ↓
Phase RETRIEVE           tools = search_* only  → candidates
    ↓
Phase RESOLVE            code or small LLM      → one target (+ confirm)
    ↓
Phase ACT                tools = open_* only    → ActionResult
    ↓
Phase VERIFY             code verify_*          → ok | pending | failed
    ↓
(later) Phase GUI        tools = find/click/type
         each step: short context = goal + last observe + last result
```

Reuse existing ReAct (`agent/react_agent.py`) + `agent/tools.py`; gate which schemas are visible by phase/policy. Optional later: planner emits a step list, each step runs with an isolated mini-context (µagent-style) — same idea, still not a chatty multi-agent org chart.

### Context

- Keep router + prefetch (already reduces tool thrash).  
- Soft-cap user / prefetch / tool payloads (already partly present).  
- Pass **summaries** forward between phases, not raw dumps.  
- Long GUI tasks: one tool → verify → one-line result into a running summary.

---

## 9. Scalability to clicks and in-app navigation

**Yes — without changing the product architecture**, if we keep:

1. General tools (not per-app bots)  
2. `observe → act → verify` with verify outside the LLM  
3. Staged tool exposure  
4. Bounded per-step observation (a11y slice, not infinite tree)

| Layer | Now | Later (same spine) |
|---|---|---|
| L1 OS | open path/url + verify | open_app, focus_window |
| L2 UI | — | a11y click/type/scroll + per-step verify |
| Policy | — | confirm destructive; deny password/payment UIs |
| Skills | — | optional recorded recipes (“my grade preset”) replayed via general tools |

**Would force a painful rewrite (avoid):**

- App-specific RPA packs as the core API  
- Pixel coordinates as the primary action language  
- Accumulating every a11y dump for 50 steps in one context  
- Trusting the model’s word instead of verify  

Example long task (future): “Photoshop + last photo + usual grade” =

1. Memory → last photo path  
2. `open_app` / `open_path`  
3. Skill or planned a11y steps with verify each  
4. Docs fetch only if stuck and user allows  

Opening the file is v0/v1. Full creative replay is a later skill.

---

## 10. Verify semantics (lessons from implementation)

`open_*` only means the OS accepted the request. **Verify** means the foreground (or agreed signal) matches the target.

### Outcomes

| `status` | Meaning |
|---|---|
| `ok` | Expectation matched |
| `pending` | Open in progress — often OS “Open with” / app picker; user must choose |
| `failed` | Timeout with no match and no recognizable waiting UI |

### Patience for OS choosers

Windows may show “How do you want to open this file?” before the real app appears. A short hard timeout falsely fails. Current behavior (`core/os_actions.py`):

- Detect general chooser UI (title/process snippets: “open with”, OpenWith, PickerHost, …)  
- While seen → extend wait (slack after each sighting; hard ceiling ~90s)  
- `open_path_and_verify` default wait ~45s  

### Known softness (tighten later)

Foreground match is substring-based. A pass can occur via `active_url` / title containing the filename while another app (e.g. the IDE) is still focused. Later: prefer focused viewer process, or require title∋filename and process∉launcher list.

Manual probe:

```powershell
$env:PYTHONPATH = (Get-Location).Path
python .\scripts\probe_os_actions.py path-verify "$PWD\README.md"
python .\scripts\probe_os_actions.py url-verify "https://example.com"
```

---

## 11. Implementation map

| Piece | Location |
|---|---|
| `open_path` / `open_url` / verify | `core/os_actions.py` |
| Agent tool wrappers + schemas | `agent/tools.py` |
| Open tool policy in ReAct | `agent/react_agent.py` |
| Manual probe | `scripts/probe_os_actions.py` |
| Idle stretch (capture reliability) | `core/screenshot_scheduler.py` + `get_idle_seconds` in `platform_support.py` |
| ReAct / tools (next) | `agent/react_agent.py`, `agent/tools.py` |
| Retrieval | `agent/retrieval.py` (existing) |

Do **not** invent a parallel agent process for v0.

---

## 12. Baby-step build order

1. ~~Doc + general-tools rule~~  
2. ~~`open_path` / `open_url` + probe~~  
3. ~~`verify_foreground` (patient Open-with) + probe~~  
4. ~~**Wire tools into ReAct + prompt policy**~~  
5. End-to-end chat demo + confirm when ambiguous  
6. `open_app` / `focus_app` / `reveal_in_folder`  
7. a11y `find` + `click_element` + per-step verify  
8. Optional: taught skills; opt-in doc fetch  

---

## 13. v0 success criteria

- Same open tools for browser URLs, local PDFs, and code files  
- No feature flag named after a single third-party app  
- Ambiguous memory → ask  
- Verify `failed` → honest failure; `pending` → tell user to finish the OS dialog  
- User can refuse / disable actions  

---

## 14. Risks

- Wrong open worse than no open → confirm + verify  
- Local 8B + too many tools → wrong/missing calls → **stage tools**  
- Path/URL extraction from memory messy → conservative resolve  
- Open-with / focus quirks → pending status + focus helpers later  
- Per-app RPA creep → reject without a general primitive underneath  
- Privacy → actions local; web docs opt-in only  

---

## 15. Relation to proactive (stuck detector)

| | Plan B (this doc) | Proactive |
|---|---|---|
| Trigger | User asks (later: offer) | Detector proposes |
| Need | Tools + verify | Timing + low false positives |
| Shared | Memory + general actions | Same tools |

Build Plan B capabilities first; proactive later *offers* the same actions.

---

## 16. One-line north star

> **Clippy is a general local computer agent with a memory of your real work — it retrieves what happened, acts with a small staged set of OS/UI primitives, verifies in code, and stays architected so clicks and in-app navigation are more tools on the same spine — not a rewrite.**
