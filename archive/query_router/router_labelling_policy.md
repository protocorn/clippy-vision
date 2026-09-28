# Router Labeling Policy

This document defines the ground truth labeling rules for router training data.
All generated examples must follow these rules exactly.
When qwen's generated label conflicts with a rule here, the rule wins.

---

## Category Definitions

### time_anchored
The user is asking about their activity at a time period that can be resolved
to a specific calendar date, day, or hour.

Valid time anchors (non-exhaustive):
- yesterday, today, tonight, this morning, this afternoon, this evening, last night
- last week, last month, this week, this month
- N hours/days/weeks ago (e.g. "3 days ago", "2 hours ago")
- Specific days: Monday, last Tuesday, this Friday
- Specific dates: June 23, 2026-06-23
- "earlier today", "just now", "an hour ago"

NOT valid time anchors (classify as topic_search instead):
- "lately", "recently", "these days" — too vague, no calendar date resolvable
- "before sleeping", "after lunch", "before the meeting" — relative to an event, not a date
- "the other day" — unresolvable
- "in the mornings", "usually in the afternoon" — habitual, not a specific instance

### topic_search
The user is asking about their activity related to a specific topic, project,
entity, or technology — with NO explicit time anchor.
The answer requires searching the activity log by subject across all time.

Includes:
- Project names (the notes app, a client dashboard, a class assignment)
- Technologies (React, Python, Kubernetes, LLMs)
- Open-ended activity questions with vague time ("lately", "recently", "before")
- Habitual/pattern questions without aggregation keywords ("what do I usually work on?")

### aggregation
The user is asking for a count, total, duration, frequency, ratio, average,
breakdown, or any statistical summary of their activities.

Aggregation keywords (non-exhaustive):
- how many, how often, how long, how much time
- total, count, frequency, average, per day/week
- breakdown, summary, most, least, longest, shortest
- "give me a weekly summary" — summary = aggregation
- "what app did I use the most?" — most = aggregation

Note: "what do I usually work on in the mornings?" is topic_search NOT aggregation
unless combined with a quantitative signal ("how much time do I usually spend").

### specific_recall
The user wants to retrieve a specific artifact from their activity log:
a URL, link, clipboard text, pasted content, exact text on screen,
file name, command, error message, article title, package name.

The user wants a specific item, not a summary or overview.

Includes:
- "what URL was I on?"
- "what did I paste?"
- "what was the error message?"
- "what was the article I was reading?"
- "what was the command I ran?"
- "remember when I had that crash — what was the error?" — specific_recall even
  though "remember" sounds like memory_query. The artifact is in the event log.

### memory_query
The user is asking about facts the assistant has MEMORIZED from past conversations:
identity, skills, job, stated preferences, explicitly shared personal information.

The answer comes from long-term memory — NOT from the activity log.

Includes:
- "what do you know about me?"
- "what are my hobbies / skills / goals?"
- "where do I work / what is my job?"
- "what projects have I told you about?" — explicit "told you" = memory
- "what programming languages do I know?" — ONLY if user has explicitly told the assistant
  (if this is inferred from coding activity, it is topic_search)

### casual
General conversational chat, factual/general knowledge, creative requests,
opinions, or technical questions that require NO personal data of any kind.

Includes:
- Math, general coding questions, language questions
- Requests for explanations, comparisons, recommendations (general)
- Creative tasks: write a haiku, tell a joke, draft an email

### follow_up_inherit
A vague or incomplete follow-up that has no standalone meaning.
The current query CANNOT be answered without the prior turn.

Signals:
- "what else?", "anything more?", "what about that?"
- "no, something else", "not that one", "I mean the other one"
- "check it properly", "look again", "you are not understanding"
- Implicit reference: "and the day before?", "what about wednesday?"
- Pronoun-only: "what about it?", "tell me more about that"

When classified as follow_up_inherit:
- secondary MUST contain the category inherited from the prior turn
- temporal_hint should be populated if the follow-up introduces a new time reference

---

## Priority Order

When a query matches multiple categories, the primary is chosen using this order
(left = higher priority):
specific_recall > time_anchored > aggregation > topic_search > memory_query > casual > follow_up_inherit


---

## Secondary Label Rules

Add secondary only when the query EXPLICITLY REQUIRES two distinct retrieval
strategies to produce a complete answer.

When in doubt: leave secondary empty [].

**Add secondary:**
- "what URL was I reading this morning?" → specific_recall + [time_anchored]
  (needs specific artifact AND date filter)
- "how many hours did I code this week?" → aggregation + [time_anchored]
  (needs count AND date-scoped query)
- "what was I researching last Tuesday and what are my goals for that project?"
  → topic_search + [time_anchored, memory_query]
  (needs topic search AND date filter AND memory for goals)

**Do NOT add secondary:**
- "what did I do yesterday?" → time_anchored, secondary: []
  (aggregation not required to answer "what did I do")
- "what have I been working on for the notes app?" → topic_search, secondary: []
  (no time anchor needed, no memory needed)
- "what is 2+2?" → casual, secondary: []
  (nothing personal needed)

---

## Temporal Hint Rules

Populate temporal_hint ONLY when time_anchored is in primary OR secondary.
Extract the EXACT phrasing from the query — do not normalize or paraphrase.

Examples:
- "what did I do yesterday?" → temporal_hint: "yesterday"
- "show me last Tuesday" → temporal_hint: "last Tuesday"
- "2 hours ago" → temporal_hint: "2 hours ago"
- "what have I been working on lately?" → temporal_hint: null (topic_search, no time anchor)
- "what do I usually work on in the mornings?" → temporal_hint: null (not time_anchored)

---

## Hard Boundary Decisions

These are the cases most likely to be labeled inconsistently. Follow these rules exactly.

### "Do you remember..." / "Remember when..."
==> The word "remember" sounds like memory_query but almost always refers to activity recall.
Rule: if the content of what is remembered is an event/activity/artifact → classify by what is being retrieved (time_anchored, topic_search, specific_recall). Use memory_query only if asking about a stored identity fact.

Examples:
- "do you remember what I did yesterday?" → time_anchored (NOT memory_query)
- "remember when I was debugging that crash? what was the error?" → specific_recall + [time_anchored]
- "do you remember my name?" → memory_query (stored identity fact)

### "Lately" / "Recently" / "These days"
==> These are NOT time anchors. No calendar date can be computed.
Rule: always topic_search when the only temporal signal is one of these words.

- "what have I been working on lately?" → topic_search
- "what have I been reading recently?" → topic_search
- "what have I been doing these days?" → topic_search

### "The other day" / "Before sleeping" / "Before the meeting"
==> These are NOT calendar-resolvable time anchors.
Rule: always topic_search.

### "What do I usually..." / "What do I normally..."
==> Habitual pattern questions. If no aggregation keyword → topic_search.
If combined with a count/duration/frequency keyword → aggregation.

- "what do I usually work on in the mornings?" → topic_search
- "how many hours do I usually spend coding?" → aggregation

### "What programming languages do I know?"
==> Ambiguous between memory_query and topic_search.
Rule: if the user has explicitly told the assistant their skills in a past conversation → memory_query.
If the question is about inferred knowledge from activity → topic_search.
Default when ambiguous: memory_query (it asks what "I know", which is a self-fact).

### "No, you are not understanding" / "That's not what I meant"
==> Always follow_up_inherit. These are corrections that reference a prior exchange.

### "What is the best/worst/most..." over a project/topic
==> topic_search (the "best" is a qualitative judgment over activity, not aggregation).
Only aggregation if asking for a measurable count/duration.

- "which is the best project I have worked on?" → topic_search
- "which project did I spend the most hours on?" → aggregation

---

## Generating Diverse Examples

When generating data, ensure each batch includes:
- At least 2 examples with spelling errors or typos
- At least 2 vague/indirect phrasings
- At least 1 multi-label example (secondary not empty)
- Use a mix of project names, technologies, and apps. Do not reuse one product name across the batch.

Reject batches where more than 50% of examples share the same sentence structure
or opening phrase.