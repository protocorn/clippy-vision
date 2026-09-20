# Insight cards — prompt lab results

Experiment: draft → generate from real Clippy data → score → refine on **different** days.
Coverage days used: **2026-08-08, 08-15, 08-20, 09-09, 09-10, 09-14, 09-15, 09-16**.

---

## Method

1. Pull `activity_coverage` → only days with `event_count > 0`.
2. Build an **evidence pack** per day: `list_sessions` + `app_time_summary` + `list_urls` (+ bounded events if sessions empty).
3. Run prompt → produce card → score:
   - **Grounded**: every claim maps to evidence (or marked `uncertain`)
   - **Useful**: would the user learn something non-obvious?
   - **Safe**: no invented completion / intent / “you should…”
4. Refine prompt; **never re-test on the same day** used for the previous version.

---

## Card 1 — Day Card (automatic daily)

### v1 prompt (failed)

> Summarize the user's day in a friendly paragraph. Mention what they worked on and how productive they were.

**Failures on Sep 14 / Sep 10:** inflated “productivity”; merged duplicate session noise; invented emotional tone; ignored days with events but **0 sessions**.

### v2 prompt (better, still soft)

> Write 5 bullets: themes, apps, notable URLs. Be factual.

**Failures on Aug 8:** without sessions, collapsed to “used Cursor a lot” — true but empty.

### **FINAL Day Card prompt (generalized)** — validated on Sep 10, Aug 14, Aug 15

Domain-agnostic: no career / research / product vocabulary baked into the instructions.
Themes and nouns come only from the evidence pack.

```text
You write an automatic DAY CARD for date={{DATE}}.

You receive an evidence pack (only source of truth):
- event_count
- app_time: [{process, minutes}, ...]
- sessions: [{start_local, end_local, active_task, summary}, ...]  // may be []
- urls: [{url, count, title}, ...]  // may be []
- title_sample: [window_title, ...]  // use when sessions is empty

Hard rules:
1. Use ONLY facts present in the evidence pack. Never invent topics, people, or outcomes.
2. Do not judge productivity, mood, success, or what the user "should" do.
3. Ignore process names that look like filenames (*.jpg) or URLs.
4. Merge near-duplicate sessions into one theme (same project/file/site recurring).
5. Prefer concrete nouns from titles/summaries/URLs over vague labels like "coding" or "browsing".
6. If event_count is 0 → output exactly: "# {{DATE}} — no capture"
7. If sessions is empty but titles/apps exist → build themes from titles + apps; set capture_note to "events only".
8. Max ~180 words. No preamble outside the template.

Output template (markdown):
# {{DATE}}
**Headline:** <≤12 words; concrete; drawn from evidence>
**Active:** <local time span if known, else "partial / unknown">
**Time (approx):** <top 1–3 apps as `Process ~Xm`; skip junk names>
**Themes:**
- <short label from evidence>: <one grounded sentence>
**Artifacts:**
- <url or distinctive title worth keeping, or `(none)`>
**Capture note:** <one of: "session summaries" | "events only" | "thin day" | "noisy/duplicate sessions">
```

### Generalized Day Card samples

#### 2026-09-10

```markdown
# 2026-09-10
**Headline:** GitHub traffic scripts + PAT setup; later “prove value” chat
**Active:** ~00:19–00:49 and ~23:17–23:41
**Time (approx):** Cursor ~39m · chrome ~11m · electron ~8m
**Themes:**
- Repo traffic tooling: `collect_github_traffic.ps1`, scheduled-task tweak, repohistory.com
- Auth plumbing: GitHub personal-access-token pages + device login success URL
- Product-eval chat: repeated Cursor Agents prompts asking for a concrete real-data example
**Artifacts:**
- https://github.com/login/device/success
- https://github.com/settings/personal-access-tokens/new
- https://repohistory.com/
**Capture note:** noisy/duplicate sessions
```

#### 2026-08-14 (events only)

```markdown
# 2026-08-14
**Headline:** Screenshot review in editor + tray / capture app toggles
**Active:** ~11:57–12:06 sampled
**Time (approx):** Cursor ~25m · electron ~2m · chrome ~2m
**Themes:**
- Image-in-editor work: window title `*_processed.jpg` under a vision project folder
- Shell/tray: System tray overflow; capture app in tray (“Capturing”)
**Artifacts:**
- https://github.com/alantriesagain/awesome-local-first/pull/13… (Gmail-wrapped link)
**Capture note:** events only
```

#### 2026-08-15 (events only)

```markdown
# 2026-08-15
**Headline:** Performance debugging on a screenshot file; agents talk on YouTube
**Active:** ~12:22–12:27 sampled
**Time (approx):** Cursor ~25m · electron ~6m · chrome ~2m
**Themes:**
- Perf pass: OCR model file + `worker.py` open; chat text about removing redundant ops
- Side tabs: YouTube “How We Build Effective Agents (Anthropic)”; ChatGPT “AI Speech Coaching Tools”; Live Caption
**Artifacts:**
- (none stable https in pack; titles carry YouTube / ChatGPT)
**Capture note:** events only
```

**Score:** Still grounded without prompt-side domain hints. Thin events-only days stay thinner (correct).

---

## Card 2 — Threads in motion (opt-in)

### Older Threads prompt

Had “company” / career-leaning examples in the lab narrative. Replaced below.

### **FINAL Threads prompt (generalized)**

```text
You write a THREADS IN MOTION card from day cards OR raw evidence packs
for dates={{DATES}} (chronological).

A thread = the same project, document, site, person, or topic appearing on
≥2 distinct days — OR a strong single-day arc with a clear lingering artifact
(url, filename, named doc).

Rules:
1. Name each thread using words that appear in the evidence (titles, urls, paths).
2. For each thread list: name · days seen · last day · one factual sentence · optional artifact.
3. No advice, plans, or “next steps.”
4. If <2 multi-day threads, say so; put singles under "One-off signals".
5. Ignore duplicate session spam; count calendar days, not row counts.
6. Under ~150 words. Markdown only.
```

### Generalized Threads sample (09-09 + 09-10 + 09-16)

```markdown
## Threads in motion
1. **Local vision / capture product** — 09-09, 09-10, 09-16 — last 09-16 — Benchmarks, PAT/traffic scripts, Plan B / screen_capture / PROJECT_VISION discussion
2. **Editor + agents workflow** — 09-09, 09-10, 09-16 — last 09-16 — Cursor Agents chats, code review (`platform_support` / crop scoring), idle-screenshot policy
3. **External applications / forms** — 09-09 — (one-off unless later days included) — Handshake listings + Makeable Builder pages

### One-off signals
- Anthropic agents YouTube / speech-coaching ChatGPT (stronger on 08-15 pack, not in this Sep window)
```

Works without career-specific instructions; career items only appear when evidence has them.

---

## Rejected prompts (do not ship)

```text
# BAD — What next
Given the day card, tell the user the single most important thing to do tomorrow.
```

```text
# BAD — Multi-week plan
Create a 3-week plan for the user's projects and goals.
```

These failed: they require goals Clippy doesn’t observe; they hallucinate deadlines; they conflict with the foolproof bar.

---

## Recommended product set (finalized)

| Card | When | Prompt |
|------|------|--------|
| **Day Card** | Auto if `event_count > 0` | **FINAL Day Card (generalized)** |
| **Threads in motion** | Opt-in / weekly | **FINAL Threads (generalized)** |

**Default UX:** Day Card lands daily → chip “Threads in motion” for multi-day view.

---

## Evidence-pack recipe (implemented)

Code: ``agent/evidence_pack.py`` → ``agent/insight_cards.py`` → ``GET/POST /insight/*``.

Per day, before prompting:

1. `activity_coverage(day, day, bucket_hours=24)` → gate
2. `app_time_summary(day, day)` (top apps; drop image-like process names)
3. `list_sessions(day, day, limit=100)` then **collapse** near-duplicates / noise → ≤14 themes
4. `list_urls(start=day, end=day, limit=30)`
5. If sessions empty after collapse: `search_bounded` events → `title_sample`

Do **not** inject full `recall_memory()` into Day Card. Threads prefer stored day-card markdown; otherwise compact packs.

Cards persist under ``core/data/insight_cards/``.
