# The Stuck Detector — Proactive Help From Your Own History

> **One-line pitch:** Clippy notices you are stuck *before you ask for help* — on anything, not just code — and offers help from your own past or from the context it has already seen, locally and privately, at the right moment.

Status: **Active design doc.** Scaffold exists (`core/stuck_detector.py` window fetch + background loop). **Build priority revised:** mouse/idle for reliable capture **before** investing in proactive dogfood. See §8.

---

## 1. Product decision: generic stuck, not a coding-error product

**Decision: optimize for generic stuck.** Coding errors are one domain among many, not the center of the product.

**Why not coding-errors-only:**

People already take stack traces to Claude, Cursor, ChatGPT, Copilot Chat. That path is fast, familiar, and good enough for "fix this error." Building a local watcher whose best demo is "I saw your KeyError" competes with something users already do in five seconds. Low differentiation. Low urgency to install Clippy.

**Where the real value is:**

Stuck is usually *not* a clean error string. It is:

- looking for something and not finding it
- knowing you did this before but not remembering where / how
- missing a step, a file, a setting, a decision you already made
- looping between apps without making progress
- comparing options and stalling
- filling the same form / repeating the same search with tiny variations
- writing / rewriting the same paragraph
- debugging, yes — but as one shape of stuck, not the product

Those moments are hard to paste into a chatbot because there is no single artifact. The struggle is distributed across Finder/Explorer, Slack, Notion, browser tabs, email, Excel, IDE, PDFs. **That** is what cloud chat cannot see, and what Clippy already watches.

So the product promise is not "I debug your code." It is:

> **"I notice when you are going in circles — on anything — and I help from what I already saw on your machine."**

Coding can still appear in demos and in early signal work (repeated error text is an easy, high-precision example of *repetition*). It must not define the feature.

**Assistance, not diagnosis.** Prefer “unusual interaction pattern” / “same loop” over “you seem stressed.” Never present medical or psychological labels.

---

## 2. Two decisions (not one score)

Collapse early multi-state taxonomies (`FRUSTRATED`, `OVERLOAD`, `STUCK`, …). They overlap and fake precision.

Use one continuous score plus a separate interruption policy:

### Decision 1 — Should Clippy help?

```text
need_for_help ∈ [0, 1]
```

Built from behavioral + contextual features (repetition, thrashing, low novelty, typing deviation, later mouse aggregates). High score means “worth investigating,” not “interrupt now.”

### Decision 2 — Should Clippy interrupt *now*?

Even when `need_for_help` is high, do not interrupt immediately. Consider:

- natural task breakpoint (OASIS-style idea; Clippy-specific signals, not copied study timings)
- persistence (anomaly lasted long enough)
- enough context to offer useful help (**expected benefit**)
- recent interruption budget / cooldown
- user mid-engagement (typing / active HID) vs idle edge
- whether a better breakpoint is likely soon (defer-to-best)

Conceptual intuition (not a literal implementation contract):

```text
intervene ≈ need_for_help × interruptibility
            and usefulness is high enough
            and cooldown allows it
```

**Optimize for useful intervention, not maximum detection.** A detector that fires constantly and helps rarely is worse than a quieter one users trust.

---

## 3. The idea in a few scenes (not one coding scene)

**Finding something:** You've opened the same Downloads folder three times, searched Slack for "invoice Q3", then Gmail, then a Drive tab with a similar query. Clippy:

> You've been looking for something like "invoice Q3" across Slack, Gmail, and Drive for ~18 minutes. Last Thursday you opened `Acme_Q3_Invoice.pdf` from Downloads after a similar search. Open that session?

**Missed / forgot a step:** You're setting up a tool, bouncing between the docs tab and the settings window, re-reading the same checklist. Clippy:

> You started this setup yesterday and stopped after step 3 (API key). Your notes from that session say you still needed the webhook URL. Resume from there?

**Decision thrash:** Three pricing pages and two comparison tabs, short dwell, same three sites for 25 minutes. Clippy:

> You've compared these three options a few times today. Here's a short summary of what you opened and what you copied — want it as a decision note?

**Writing stall:** Same doc, high revision ratio, long pauses, little net new text. Clippy:

> You've been rewriting the same section for a while. Want the earlier draft from this morning's session, or a handoff of the surrounding context to chat?

**Coding (one domain among others):** Same error / same failing test / same file loop. Still valuable — especially when the help is *your past fix*, not a generic explanation — but not the only story we tell.

In every case the shape is the same: notice the loop → name what it looks like you're stuck on → offer something concrete from history or assembled context → never a vague "Need help?"

---

## 4. Why this and why nobody else does it

**Every assistant today is pull-based.** ChatGPT, Claude, Cursor — they wait for you to realize you're stuck, formulate the question, reconstruct context, and paste it in. Steps 1–3 are the expensive part. No mainstream tool owns them.

**Recall tools stop at search.** Rewind, Microsoft Recall, Limitless record your screen and let you search. They do not notice struggle or intervene.

**Coding assistants own "paste the error."** They do not own "you've been hunting a file across four apps for 20 minutes" or "you already finished half this setup yesterday."

**Why the gap stays open:**

1. **Stuck is a pattern over time across apps**, not a single message. You need continuous cross-app capture + typing/window/clipboard signals. Clippy already has that.
2. **False positives kill trust.** Wrong interruptions get the feature muted forever. Conservative thresholds and personal baselines are hard work — and a moat.
3. **Only local can watch this closely.** Streaming full activity to the cloud for struggle detection is a privacy and cost non-starter. This feature is viable *because* Clippy stays on-device.

The bet: *searchable recall* and *paste-into-chat coding help* are crowded. *Timely, unprompted help when you are looping — on anything — from your own machine history* is not.

---

## 5. What "stuck" / friction means (generic definition)

**Working definition:**

> Over a recent window of time, the user is expending effort (switches, searches, edits, copies) but **progress is low**: the same goals, queries, places, or content keep recurring with little new outcome.

This raises `need_for_help`. It is **behavioral + semantic**, not "error keywords present."

### 5.1 Shapes (for phrasing later — not early enum states)

| Shape | What it looks like | What help can look like |
|---|---|---|
| **Can't find** | Repeated similar searches, same folders/apps revisited, short dwell | "You opened X last time you looked for this" / assemble where you already looked |
| **Forgot / left unfinished** | Return to same setup/doc/task after a gap; re-reading the same checklist | "You left off here yesterday" |
| **Missing something** | Loop between instructions and work surface; re-copy of partial config | Surface the missed step or the earlier session note |
| **Decision stall** | Same few comparison destinations; copy/paste of options; little commit | Summarize options already seen into a decision note |
| **Writing / editing stall** | High revision, low net progress, same section | Earlier draft / surrounding context to chat |
| **Technical failure (incl. code)** | Repeated error text, failing CI, same failing action | Past personal fix, or assembled attempt history — *not* competing with "explain this stack trace" |

v1 does not need perfect domain labels. Score the shared loop; name the loop only when intervening (optional LLM later).

### 5.2 Shared signals (domain-agnostic)

**Repetition (strongest, cross-domain):**

| Signal | Source |
|---|---|
| Same / near-same search query again | `active_url`, screen/accessibility text |
| Same clipboard content copied multiple times | `clipboard_change` / `paste` |
| Same window/title/folder revisited with short gaps | `context_change` + dwell |
| Same chunk of screen text recurring | `vision_ocr_text` / accessibility text — **errors not privileged** |

**Thrashing:**

| Signal | Source |
|---|---|
| Rapid switches, short dwell | `context_change` + `dwell_ms` |
| Ping-pong between a small set of apps/sites | sequences over `process_name` / URL |
| Copy → search → return → copy again | clipboard + context-change correlation |

**Typing / HID dynamics (personal baseline):**

| Signal | Source |
|---|---|
| Unusually slow typing for *you* in this app | baseline WPM z-score |
| Revision-ratio spike | `revision_ratio` vs baseline |
| Idle / away vs engaged | **Phase A mouse/idle** — OS last-input, aggregates |
| Click/scroll burstiness | Phase A aggregates (not raw streams) |

**Low novelty / low progress:** high effort, few unique destinations / near-duplicate titles/URLs.

### 5.3 Content hints (optional boosters)

Error keywords, empty search results, CI red, form validation — boosters only. Not required for a high `need_for_help`.

### 5.4 What this is not

- **Reading / watching:** one surface, low input, steady progress
- **Meetings / calls:** conferencing app foreground → suppress
- **Healthy research:** many destinations, novel content
- **Idle / away:** no HID → gone, not help-needed
- **Flow:** deep work, normal personal baselines, little thrashing

---

## 6. Detection + intervention architecture (tiered)

Same philosophy as the event classifier: cheap and silent first, LLM rarely, intervene only when there is something useful to offer. Scoring runs on a **rolling window (~20–30 min)** of already-stored events — not on the capture hot path.

**Tier 0 — hard gates:** capture off, meeting foreground, idle/away (real HID once Phase A lands), cooldown, suppression list → `need_for_help = 0` / skip.

**Tier 1 — `need_for_help` (no LLM):** additive, inspectable features → float in `[0, 1]`. Silence is default. High bar.

**Decision 2 layer — interruptibility + usefulness:** breakpoint-ish signals, persistence, cooldown, “do we have a concrete artifact?” Only then is an interrupt candidate logged or shown.

**Optional later — LLM naming (only on strong candidates):** name the loop in one sentence; do not invent advice.

**Offer paths:**

- **Gold — past self:** retrieve related sessions/events/facts.
- **Silver — assemble context:** handoff to Clippy chat / later MCP.
- **Nothing useful → stay silent.**

**Privacy split:**

- **Decision layer:** prefer stats, fingerprints, hashes, counts, aggregates.
- **Help layer (only if intervening):** may use existing local screen/session memory — that is Clippy’s moat.

---

## 7. Intervention design

1. **Silence is default.** Aim for ~2–3 interventions per day max.
2. **Never steal focus.** Quiet toast / tray; no modal; dismissible.
3. **Always concrete.** Name the loop and offer an artifact. Ban: "You seem stuck. Need help?" / "You seem stressed."
4. **Cooldowns + interruption budget.** Per loop-fingerprint + global gap; dismissals raise thresholds.
5. **Feedback teaches the system.** Show me / Not now / I wasn't stuck (or equivalent) — adapt thresholds.
6. **User controls.** Global toggle, per-app suppress, sensitivity (default: conservative), quiet hours.

---

## 8. Build order (revised)

**Priority flip:** mouse/idle for **reliable capture first** → then proactive thin slice. Do not invest in tuning `stuck_detector` thresholds until Phase A is done.

| Phase | Name | Goal |
|---|---|---|
| **A** | Mouse / idle for capture | Reliable capture gating + bounded HID aggregates |
| **B** | Decision 1 lite | Silent `need_for_help` + JSONL dogfood |
| **C** | Decision 2 stub | Log WAIT vs WOULD_INTERRUPT (still mostly silent) |
| **D** | Usefulness + first quiet UI | Gold/silver intervention + feedback |
| **E+** | Research depth | Richer mouse baselines, learned breakpoints, optional labels on top of the score |

**Explicit non-goals early:** coding-only product; multi-state frustration taxonomy; raw click/scroll storage; cloud APIs for the behavioral layer.

### Phase A — Mouse / idle *(do this now)*

**Goal:** Make capture smarter when the user is away vs active. Proactive dogfood waits.

1. **OS last-input / idle age** (Windows first; macOS follow).
2. **Mouse activity listener** (existing `pynput` stack): move / click / scroll as **live activity**.
3. **Bounded aggregates only** — e.g. `idle_ms`, last HID timestamp, short-window click/scroll rates, optional `mouse_burst` summary events (same spirit as `typing_burst`).
4. **Wire into capture reliability:** idle **stretches** background screenshot gap (still captures so automated on-screen work is not missed); short polls so returning from away resets within ~10s. Do **not** hard-skip on HID idle alone. Existing phash dedup drops truly static frames. Active HID → engaged; long away ≠ stuck for future Tier 0.

**Constraint (freeze this):**

> Store aggregates and activity state; **do not** store raw click/scroll streams. Mouse exists first to make capture reliable; second to feed `need_for_help` / interruptibility.

**Non-goals for A:** velocity/acceleration trajectory ML; proactive toast; threshold tuning on stuck JSONL.

**Done when:** capture clearly behaves better when you walk away vs when active; idle visible in logs/metrics; aggregates available for Phase B.

Scaffold note: `core/stuck_detector.py` may keep running; **do not prioritize Tier-1 scoring work** until A lands.

### Phase B — Decision 1 lite *(after mouse)*

Silent `need_for_help ∈ [0, 1]` from existing events **plus** Phase A aggregates.

- Tier 0 gates (including real idle)
- Tier 1 additive features → score
- Fingerprint + JSONL under `{data}/stuck_detector/`
- No UI, no LLM, no retrieval yet

**Done when:** dogfood shows rare high scores that often feel like real friction (including non-coding).

### Phase C — Decision 2 stub

Combine `need_for_help`, simple interruptibility (app switch, idle edge, end of typing burst, not mid-burst), persistence, cooldown, cheap usefulness proxy. Log `WAIT` vs `WOULD_INTERRUPT`.

### Phase D — First quiet intervention

Only when need + timing + usefulness all clear: gold past-self and/or silver context bundle; feedback loop; still conservative budget.

### Phase E+

Richer mouse baselines, learned breakpoints, BusyBody-style interruptibility, optional coarse tags *on top of* `need_for_help` (never replacing it), audio as a separate track.

---

## 8.1 Phase B detail — silent `need_for_help` scorer

*(Formerly “Phase 0.” Deferred until Phase A completes.)*

### Goal

Prove we can spot *generic* loops with a tolerable false-positive rate **before any user-facing intervention**. Score + log only.

### Success criteria (dogfood, ~1–2 weeks after B starts)

- Would-have-fired / high-score log exists and is easy to review.
- Label entries: true friction / not / ambiguous.
- Target: when score would have interrupted, ≥ ~70% feel real or clearly fixable by raising the threshold.
- A few high scores per day at default thresholds, not dozens.
- Non-coding friction appears in the log.
- Scoring pass stays cheap (use `performance_metrics`).

### Where it runs

- **API process** via `core/background_jobs.py` → `start_stuck_detector()`.
- Module: `core/stuck_detector.py` (scaffold already fetches a 30‑min window).
- **Not** on the capture hot path.

### Loop knobs (starting proposals)

| Knob | Proposal |
|---|---|
| Poll interval | ~45–60 s base (+ `load_backoff_multiplier()`) |
| Rolling window | last **30 minutes** |
| Min events | e.g. ≥ 8 |
| Capture off / idle | Tier 0 suppress |
| Cooldown after high score | global ~15–20 min + per fingerprint ~45–60 min |

### Tier 1 → `need_for_help`

Inspectable features (map to 0–1, conservative threshold):

1. Place / destination repetition  
2. Search / query repetition  
3. Clipboard repetition  
4. Screen-text chunk repetition (generic)  
5. Thrashing + small unique-destination set  
6. Low novelty  
7. Typing z-score boost (alone should not dominate)  
8. Mouse/idle aggregates from Phase A (engagement vs away; burstiness)

Require strong repetition **and** (thrash/low-novelty **or** typing/HID stress), then threshold. Prefer misses over junk.

### Log shape

Path: `{get_data_dir()}/stuck_detector/would_have_fired.jsonl`

Include `need_for_help`, threshold, fingerprint, `signals` breakdown, anchors, sample events. Use `flush=True` on prints under Electron.

### Implementation order for Phase B (when A is done)

1. Tier 0 gates using real idle from Phase A  
2. Pure feature helpers → `need_for_help`  
3. Fingerprint + cooldown  
4. JSONL + metrics  
5. Dogfood / tune — **then** Phase C  

---

## 9. Honest risks

- **Generic is harder than "match error string."** Phase B must prove non-coding loops.
- **False positives remain existential.** Prefer missing moments over wrong interruptions.
- **Mouse done wrong = storage blow-up.** Aggregates only.
- **Retrieval quality varies by shape.** Silence when nothing useful beats a vague toast.
- **"Competing with ChatGPT on errors" trap.** Smell if we only polish error-keyword paths.
- **Perception / privacy.** Local-only, audit trail, suppress lists.
- **Timing quality matters as much as detection.** Decision 2 is not optional polish.

---

## 10. Demo that sells the *generic* product

**Primary demo (finding):** User hunts an invoice across Slack → Gmail → Drive → Downloads. Toast: past session with the PDF. Caption: *"You weren't stuck on code. You were stuck finding something. Clippy already knew where it was."*

**Secondary demo (coding is fine as #2):** Past personal fix — framed as "your history," not "we replace your coding chatbot."

> **"Your computer noticed you were going in circles — and remembered the answer from your own work. 100% local."**

Ideal feel: Clippy spends most of its time doing nothing. When it speaks: *"That was actually a good time to ask."*

---

## 11. Open questions

1. ~~Where does the detector loop live?~~ **Resolved:** API process → `core/stuck_detector.py`.
2. ~~Mouse before or after proactive dogfood?~~ **Resolved:** **Phase A mouse/idle first** (capture reliability), then Phase B `need_for_help`.
3. ~~Multi-state taxonomy vs single score?~~ **Resolved:** `need_for_help ∈ [0, 1]` only for early phases.
4. Intervention UI: Electron toast vs OS notification vs tray? *(Phase D)*
5. Silver path: Clippy chat first, MCP later, or both? *(Phase D)*
6. Audit storage: JSONL vs table for interventions? *(Phase B–D)*
7. Near-duplicate matching aggressiveness (exact vs fuzzy)? Start exact + light normalize in Phase B.
8. Meeting-app suppress list for Tier 0 — which process names first?
