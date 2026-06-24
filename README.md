# Clippy Vision V1.0

## Motivation

When building projects, I don't just write code...I juggle between reading articles, studying similar products, diving into documentation, and figuring out how to debug issues. Every time I turn to an LLM like ChatGPT or Claude for help, I have to re-explain everything: my idea, what I've already researched, what I've already tried. This gets exhausting fast. And when a conversation thread grows too long and you start a new session, all that intermediate context is gone, and the model has no idea where you left off.

Even if these tools did manage to answer your questions, they will never be able to do it with full privacy. Your data is not truly private; companies openly acknowledge using it to improve their models, which makes sense: **MORE DATA = BETTER MODELS**. But some things need to stay private. When you share something deeply personal with an AI assistant, there's no guarantee that data isn't being captured, logged, or seen by other parties. That's a real privacy threat.

This is why I built Clippy Vision. It solves the context problem entirely. It watches your work passively, 24/7, so you always have an assistant that knows exactly what you've been doing. More importantly, the entire infrastructure (database, model gateway, and the LLM itself) runs locally. No cloud. No data leakage. It learns continuously from your interactions; the more you use it, the better it understands you. Think of it as going from a stranger to a close companion, except this one never forgets a single detail about you and answers every question without you needing to provide any context upfront.

## Tech Stack

1. **Ollama**: Used as the local model gateway between Clippy Vision and the LLMs.
   - `qwen3:8b` - the main reasoning brain: handles classification, summarization, SQL generation, and question answering.
   - `qwen3-vl:4b` - handles vision classification and OCR.
   - `nomic-embed-text` - converts text into vector embeddings.

2. **Pywin32**: Captures high-level on-screen signals on Windows - foreground window title, process name, clipboard contents, and more.

3. **SQLite**: A lightweight, fully local database efficient enough to handle large volumes of data. Stores events, summaries, agent memories/facts, and conversation history.

## Architecture of Clippy Vision

---

## Segment 1: Data Capture

This is the most critical segment of the entire architecture. It builds the foundation by capturing on-screen data and acts as the entry point for every downstream component.

### What is Captured?

In Version 1, the following data is captured:

1. Active foreground window (title, process name, active URLs if any)
2. Clipboard contents (copy and paste events)
3. Context switches (foreground window changes)
4. Keystroke dynamics with adaptive baseline (key bursts, deviation from baseline)
5. Screenshots (description and OCR text are stored in the database; raw images are kept on disk)

Planned for future versions:

1. Mouse events (clicks and movement for idle detection)
2. File watcher (actively monitors file modifications as the user works)
3. On-demand continuous screen capture (screen sharing mode)
4. On-demand audio capture

### How is it Captured?

- `core/screen_capture.py` starts the capture daemon, which detects and stores events.
- Each detected event is assigned an **interest score** (0–10). A threshold determines whether the event is interesting enough to process further.
- Every event passes through a **three-tier classification pipeline** before being stored in the database.

---

### Tier 0: Rule-Based

Fast, deterministic rules that immediately flag high-confidence signals.

- **(a)** Typing burst has fewer than 2 words **OR** character-to-keypress ratio < 0.30 (mostly modifier/arrow keys) → **NOT INTERESTING** (score = 0)
- **(b)** Window switches to a known background system process (e.g., `msiexec.exe`, `SearchHost.exe`) → **NOT INTERESTING** (score = 0)
- **(c)** Duplicate context switch (title unchanged after tab switch) → **NOT INTERESTING** (score = 0)
- **(d)** Pasted clipboard content is fewer than 3 words → **NOT INTERESTING** (score = 1)
- **(e)** High deviation detected in typing from the established baseline → **INTERESTING** (score = 9)

---

### Tier 1: Feature-Based

Scoring starts at a neutral value of 5. Multiple features are evaluated and scores are added or subtracted. Two thresholds decide the final label:
- `INTERESTING_THRESHOLD = 7` → score > 7 = **INTERESTING**
- `NOT_INTERESTING_THRESHOLD = 4` → score < 4 = **NOT INTERESTING**

**Features and scoring:**

**(a) Typing deviation**
- > 1.5 → score += 2
- < 1.0 → score -= 3

**(b) Context novelty** (how many times this process was seen in the last 7 days)
- Never seen → score += 2.5
- Seen < 5 times → score += 1.5
- Seen 5–49 times → score += 1.0
- Seen ≥ 50 times → score += 0.5 (minor boost)

**(c) Typing intensity** (compared against the per-app baseline)

WPM z-score:
```
wpm_z = (current WPM − baseline mean WPM) / baseline WPM std dev
```
- wpm_z > 1.5 → score += 2 (unusually fast)
- wpm_z < −1.5 → score += 1.5 (unusually slow)
- No stable baseline, but WPM > 0 → score += 0.5 (minor boost)

Revision z-score:
```
rev_z = (current revision ratio − baseline mean) / baseline std dev
```
- rev_z > 1.5 → score += 1.5 (unusually high revision)
- Current revision ratio > 0.3 → score += 0.5 (minor boost)

**(d) Clipboard/paste content length**
- Word count > 50 → score += 2
- Word count > 15 → score += 1

---

### Tier 2: LLM Classification Fallback

Events that fall in the ambiguous range (score 4–7) cannot be reliably labeled by rules or features alone, typically because each event is classified in isolation, without surrounding context. Tier 2 resolves this by feeding the **last N=3 events** alongside the current event to the LLM, giving it enough context to make a confident decision.

The LLM always outputs one of three labels:
- **INTERESTING**
- **NOT INTERESTING**
- **NEEDS_VISION** - the event is forwarded to the vision model for further analysis

---

### Tier 2.5: Vision Classification

Not every event can be classified by signals alone; sometimes you need to actually see the screen. This tier handles those cases.

**The timing problem:** A screenshot cannot be taken at the moment Tier 2 routes an event to vision, because by then the screen has already moved on. Capturing a screenshot per event would also be prohibitively expensive in storage.

**The solution:** Screenshots are pre-captured proactively, specifically when a typing burst is detected. Three screenshots are taken with exponential delay, capturing what happens right after. When the vision model needs to analyze an event, it matches the event timestamp to the nearest pre-captured screenshot and uses that for classification.

> **Note:** The model used, `qwen3-vl:4b`, only supports one image per prompt, so multi-screenshot context is not possible with it. The architecture is designed to support multiple images, and models with that capability can be swapped in.

**Screenshot processor (`core/screenshot_processor.py`):**

A background daemon runs every 10 seconds to process accumulated screenshots. Rather than running the vision model on every screenshot individually, it first computes a **perceptual hash (pHash)** for each image and groups visually identical screenshots using Union-Find (bit distance ≤ 2 = same screen). Vision runs once on the most recent screenshot in each group (the representative), and its verdict is copied to all duplicates in the group — avoiding redundant LLM calls on screens that haven't changed.

Each processed screenshot is matched to the nearest event in the database (within ±10 seconds). If no event exists at that timestamp, a new `screenshot_analysis` event is created automatically so no vision output is ever orphaned.

The processor also prioritises recent screenshots (within the last 60 seconds) over older ones to keep classification latency low during active use, while still working through the backlog when idle.

---

## Typing Dynamics

Typing baselines are tracked **per application**, not as a single global average. This matters because people type very differently depending on what they're doing: coding is not the same as messaging a friend on WhatsApp.

Metrics tracked:
- Typing speed (WPM)
- Average dwell time
- Average inter-key interval (IKI)
- Revision ratio
- Max pause duration

The baseline updates continuously using an **exponential moving average** with `alpha = 0.05`, meaning each new typing sample nudges the baseline slightly without overwriting it. The baseline only activates after **30 samples**, a threshold found to be the sweet spot where deviation scores begin to stabilize.

Deviation is calculated as:

```
overall_deviation = round(math.sqrt(sum(z**2 for z in z_scores.values()) / len(z_scores)), 2)
```

- `overall_deviation > 2.0` → flagged as an anomaly

A **global personal baseline** is also planned for future versions, serving as a fallback when a per-app sample count hasn't yet reached the 30-sample threshold.

---

## Segment 2: Summarization

Thousands of raw events accumulate quickly for an average user working 5–6 hours a day. To keep this manageable, events are summarized every 5 minutes using `qwen3:8b`.

To avoid wasted LLM calls, the summarizer only runs if there are **more than 3 interesting events** in the pipeline:
- **A)** A 5-minute window with zero interesting events has nothing worth summarizing.
- **B)** 1–2 events alone don't provide enough signal to justify a summary.

The summarizer runs in **two passes per tick**:
- **Pass 1:** Immediately summarizes all pending events without waiting for vision classification to complete.
- **Pass 2:** Goes back and re-summarizes any sessions where vision has since finished, overwriting the earlier summary with richer, vision-informed data.

**Why not wait for vision?**
- The vision model takes 40–60+ seconds per image depending on the device, and waiting for it creates a growing backlog.
- Tier 2 provides classification labels almost instantly, so the first pass is cheap and fast.
- The two-pass approach gives the best of both worlds: immediate availability and eventual accuracy.

---

## Segment 3: Distiller

Summaries solve the raw event volume problem, but summaries themselves can pile up over time. The Distiller adds another level of abstraction, running every 5 sessions to extract high-level facts and behavioral patterns from recent summaries.

**Session definition:**
- (i) Consecutive summaries must be less than 30 minutes apart
- (ii) A session cannot contain more than 20 summaries
- (iii) *(Planned)* Sessions will eventually break based on detected shifts in user activity

**How facts are stored:**

Each extracted fact is vector-embedded and compared against existing cluster centroids. If similarity exceeds `CLUSTER_THRESHOLD = 0.75`, the fact is routed to the closest cluster; otherwise a new cluster is created.

Routing alone isn't enough. Once a matching cluster is found, a second LLM call decides what to do with the fact:
- **(i) ADD** - new information not yet captured
- **(ii) UPDATE** - refines or replaces an existing fact
- **(iii) NOOP** - fact is already present; no action needed
- **(iv) CONFLICT** - new fact directly contradicts an existing one; both are preserved and flagged for resolution

This keeps memory clean, non-redundant, and up to date.

The Distiller also runs after the second pass of the summarizer (post-vision re-summarization), not only on the regular 5-session schedule.

> **Conflict resolution:** When a newly extracted fact directly contradicts an existing one, the incoming fact is stored as a new active fact and a record is written to the `memory_conflicts` table (both sides are preserved — neither is silently dropped). Unresolved conflicts are surfaced to the agent at query time so the user can decide which version to keep. When the user explicitly corrects a fact via `save_identity` with `op=override`, all open conflicts involving that fact are automatically closed.

---

## Segment 4: The Agent

The agent is the interface between the user and everything Clippy Vision has learned. It's built as a **ReAct agent with function calling**, giving it the ability to reason across raw events, summaries, and memory before answering.

**Tools available to the model:**

| Tool | Description |
|---|---|
| `search_sessions` | Generates and executes SQL queries on the summaries table |
| `search_events` | Generates and executes SQL queries on the events table |
| `recall_memory` | Lists all memory cluster labels and descriptions - a directory of what Clippy knows about you |
| `fetch_cluster` | Fetches relevant memory facts from a cluster |
| `save_identity` | Saves the user's autobiographical details |
| `save_note` | Saves explicit information the user asks to remember |

**Prompt components:**

**(1) Conversation history** - provided in two tiers:
- **Tier 1 (always included):** Last 2 rolling summaries + last 8 turns (4 full exchanges). Every 5 saved messages, the conversation is summarized and its vector embedding is stored.
- **Tier 2 (deep conversations):** Tier 1 + 2 most semantically relevant summaries retrieved via embedding search.

**(2) User Profile** - autobiographical information about the user, injected directly into context.

**(3) Memory Context** - the user query is embedded and the top 8 most relevant memory facts are retrieved using `MEMORY_MIN_SIM = 0.30` as the minimum similarity threshold.

**(4) Tool/function calling** - the model is explicitly prompted to call tools when it needs more information. A correction loop handles failures: if SQL generation fails, the error is fed back and the model retries. If a tool returns `None` or irrelevant data, the model is prompted to call additional tools.

The ReAct loop is capped at `MAX_STEPS = 10` to prevent hallucination or infinite loops.

When the user chats with the agent, the conversation is also passed to the Distiller to extract new facts using the same clustering algorithm. If a fact from conversation conflicts with one extracted passively, **the agent's version always takes precedence**.

---

## Segment 5: SQLite Database

All data accessible to the agent is stored locally in SQLite. See `core/storage.py` for full table schemas.

**Tables:**
1. `events` - raw captured events (typing bursts, clipboard, window title, etc.)
2. `sessions` - summaries of events
3. `memory_clusters` - cluster metadata
4. `memory_meta` - autobiographical memory and distiller metadata
5. `memory_facts` - individual facts within clusters
6. `memory_conflicts` - unresolved contradictions between facts (flagged for user resolution)
7. `conversation` - full conversation history

Virtual tables for `events` and `sessions` support full-text search (planned for future versions).

**Memory types:**

**1. Persistent memory** - never expires
- `conversations`, `memory_clusters`, `memory_meta`, `memory_facts`
- Data is never deleted, though it can be updated.

**2. Non-persistent memory** - each record has a TTL (time-to-live)
- `events` expire after **7 days**
- `sessions` expire after **90 days**
