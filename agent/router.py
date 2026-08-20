import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import torch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
  sys.path.insert(0, str(PROJECT_ROOT))

try:
  from core.llm_gateway import Priority, gateway
except ImportError:
  sys.path.append(os.path.join(os.path.dirname(__file__), "..", "core"))
  from llm_gateway import Priority, gateway

@dataclass
class RouterDecision:
    primary: str
    secondary: list[str]
    temporal_hint: str | None = None
    needs_memory_fetch: bool = False

    # Label → softmax score for secondaries (used by should_prefetch thresholds)
    secondary_scores: dict[str, float] = field(default_factory=dict)

CATEGORIES = [
    "time_anchored", "topic_search", "specific_recall",
    "memory_query", "casual",
]
ID2LABEL = {i: c for i, c in enumerate(CATEGORIES)}

MINILM_MODEL = "sentence-transformers/paraphrase-MiniLM-L3-v2"
MINILM_CONFIDENCE_THRESHOLD = 0.50
SECONDARY_THRESHOLD = 0.20

CLASSIFIER_PATH = Path(__file__).parent.parent / "models" / "router_classifier" / "best"

_PREFETCH_THRESHOLDS: dict[str, float] = {
    "memory_query":    0.55,
    "time_anchored":   0.55,
    "specific_recall": 0.30,
    "topic_search":    0.25,

    # Categories not listed here are not prefetched
}

_classification_model = None
_classification_tokenizer = None
_classifier_lock = threading.Lock()

_SCREENSHOT_QUERY_RE = re.compile(
  r"\b(?:screenshot|screen[- ]?shot|screen capture|captured screen|captured frame)\b",
  re.IGNORECASE,
)
_ARTIFACT_QUERY_RE = re.compile(
  r"\b(?:url|link|clipboard|copied|pasted|error message|command|file name|filename|"
  r"exact text|what did it say)\b",
  re.IGNORECASE,
)
_TIME_QUERY_RE = re.compile(
    r"\b(?:today|yesterday|tonight|morning|afternoon|evening|last (?:week|month|night)|"
    r"this (?:week|month|morning|afternoon|evening)|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
    r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"\d+\s+(?:minutes?|hours?|days?|weeks?|months?)\s+ago)\b",
    re.IGNORECASE,
)


def _deterministic_route(query: str) -> RouterDecision | None:
  text = str(query or "").strip()
  if not text:
    return None

  if _SCREENSHOT_QUERY_RE.search(text):
    secondary = ["time_anchored"] if _TIME_QUERY_RE.search(text) else []
    return RouterDecision(
      primary="specific_recall",
      secondary=secondary,
      temporal_hint=text if secondary else None,
      needs_memory_fetch=True,
      secondary_scores={route: 1.0 for route in secondary},
    )

  if _ARTIFACT_QUERY_RE.search(text):
    secondary = ["time_anchored"] if _TIME_QUERY_RE.search(text) else []
    return RouterDecision(
      primary="specific_recall",
      secondary=secondary,
      temporal_hint=text if secondary else None,
      needs_memory_fetch=True,
      secondary_scores={route: 1.0 for route in secondary},
    )

  return None

class MiniLMClassifier(torch.nn.Module):
  def __init__(self, base_model: str, num_labels: int, *, local_files_only: bool = False):
    super().__init__()
    from transformers import AutoModel

    self.encoder = AutoModel.from_pretrained(base_model, local_files_only=local_files_only)
    h = self.encoder.config.hidden_size
    self.dropout = torch.nn.Dropout(0.1)
    self.classifier = torch.nn.Linear(h, num_labels)

  def mean_pool(self, token, mask):
    m = mask.unsqueeze(-1).expand(token.size()).float()
    return torch.sum(token * m, 1) / torch.clamp(m.sum(1), min=1e-9)

  def forward(self, input_ids, attention_mask):
    out = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
    return self.classifier(self.dropout(self.mean_pool(out.last_hidden_state, attention_mask)))


def load_classifier():
  global _classification_model, _classification_tokenizer
  with _classifier_lock:
    if _classification_model is not None:
      return _classification_model, _classification_tokenizer

    if not CLASSIFIER_PATH.exists() or not (CLASSIFIER_PATH / "model.pt").is_file():
      print(f"[router] Classifier checkpoint not found at {CLASSIFIER_PATH}; using tool-driven retrieval")
      return None, None

    from core.model_residency import can_load_light
    if not can_load_light():
      return None, None

    try:
      from transformers import AutoTokenizer
      _classification_tokenizer = AutoTokenizer.from_pretrained(CLASSIFIER_PATH, local_files_only=True)
      _classification_model = MiniLMClassifier(
          MINILM_MODEL,
          num_labels=len(CATEGORIES),
          local_files_only=True,
      )
      _classification_model.load_state_dict(torch.load(CLASSIFIER_PATH / "model.pt", map_location="cpu"))
      _classification_model.eval()
      print(f"[router] Classifier loaded successfully from {CLASSIFIER_PATH}")
      return _classification_model, _classification_tokenizer
    except Exception as e:
      print(f"[router] Failed to load classifier: {e}")
      return None, None

def classify_query(query: str) -> tuple[RouterDecision|None, float|None]:
  deterministic = _deterministic_route(query)
  if deterministic is not None:
    return deterministic, 1.0

  model, tokenizer = load_classifier()

  if model is None:
    return None, 0.0

  enc = tokenizer(query,
  return_tensors="pt",
  padding="max_length",
  truncation=True,
  max_length=128)

  with torch.no_grad():
    logits = model(enc["input_ids"], enc["attention_mask"]).squeeze(0)

  probs = torch.softmax(logits, dim=0).tolist()
  primary_idx = int(torch.tensor(probs).argmax())

  primary_label = ID2LABEL[primary_idx]
  confidence = probs[primary_idx]

  secondary_scores = {
      ID2LABEL[i]: p for i, p in enumerate(probs)
      if p >= SECONDARY_THRESHOLD and i != primary_idx
  }
  secondary_labels = list(secondary_scores.keys())

  return RouterDecision(
    primary=primary_label,
    secondary=secondary_labels,
    temporal_hint=None,
    needs_memory_fetch=
    (primary_label in ("memory_query", "topic_search") or "memory_query" in secondary_labels),
    secondary_scores=secondary_scores,
  ), confidence


def should_prefetch(decision: RouterDecision, confidence: float) -> bool:

    # Generative / chat turns: never pull activity/memory context via secondaries.
    if decision.primary == "casual":
        return False

    threshold = _PREFETCH_THRESHOLDS.get(decision.primary)
    primary_ok = threshold is not None and confidence >= threshold

    # Secondaries must clear that route's own prefetch threshold (not just 0.20).
    secondary_ok = any(
        s in _PREFETCH_THRESHOLDS
        and decision.secondary_scores.get(s, 0.0) >= _PREFETCH_THRESHOLDS[s]
        for s in decision.secondary
    )
    return primary_ok or secondary_ok


"""
ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "primary":   {"type": "string", "enum": ["memory_query", "topic_search", "casual", "time_anchored", "aggregation", "specific_recall", "follow_up_inherit"]},
        "secondary": {"type": "array", "items": {"type": "string", "enum": ["memory_query", "topic_search", "casual","time_anchored", "aggregation", "specific_recall","follow_up_inherit"]}},
        "temporal_hint": {"type": "string"}
    },
    "required": ["primary", "secondary"]
}


SYSTEM_PROMPT = ```
You are a query router for a personal AI assistant called Clippy.

Clippy monitors the user's computer activity and stores two types of data:
  1. Activity log - time-stamped session summaries and raw events (screenshots,
     clipboard, URLs, app usage). Retained 90 days (sessions) / 7 days (raw events).
  2. Long-term memory - facts extracted from conversations: identity, skills, job,
     projects the user explicitly mentioned, preferences. Stored indefinitely.

Your job: classify the user's query so the assistant knows which data source to search.

---
INPUT FORMAT
---
You receive the last 1-3 conversation turns followed by the current query. Example:

  User: what did I work on yesterday?
  Clippy: You worked on Clippy Vision debugging.
  User: what about the day before?

Classify based on the CURRENT (last) query. Use prior turns only to resolve
vague references like "that", "it", "the same thing".

---
CATEGORIES
---

time_anchored
  User asks about activity at an EXPLICIT, calendar-resolvable time period.
  Signals: yesterday, today, this morning, last week, N hours/days ago, a specific date.
  "Lately", "recently", "before" with no time reference are NOT time anchors.
  Uses the activity log with a date filter.

topic_search
  User asks about their activity related to a specific topic, project, or entity
  with NO explicit time anchor.
  Uses the activity log searched by subject across all time.

aggregation
  User asks for a count, total, duration, frequency, or statistical breakdown.
  Signals: how many, how often, how long, total, per day/week, average, breakdown.
  Uses SQL aggregation on the activity log.

specific_recall
  User wants a specific artifact: URL, link, clipboard text, pasted content,
  exact text on screen, file name, article title.
  The user wants to retrieve a specific item, not a summary.
  Uses fine-grained event search.

memory_query
  User asks about facts the assistant has MEMORIZED about them from past conversations.
  The answer comes from stored identity/preference facts - NOT from the activity log.
  Key distinction: if the answer is "what the user TOLD the assistant" use memory_query.
  If the answer is "what the user DID or worked on" use topic_search or time_anchored.
  Uses long-term memory store.

casual
  General conversational chat, factual/general knowledge, advice, or opinions that
  require no personal data of any kind.
  No retrieval needed.

follow_up_inherit
  A vague or incomplete follow-up that has no standalone meaning and can only be
  interpreted using the prior turn.
  Signals: "what about that?", "check it properly", "no something else", "and the other one?"
  When classified as follow_up_inherit, secondary must contain the inherited category
  from the prior turn so the assistant knows what retrieval strategy to continue with.

---
RULES
---

PRIMARY - always exactly one. Use priority order below when ambiguous.

SECONDARY - add ONLY when the query explicitly REQUIRES two distinct retrieval
strategies to answer fully. When in doubt, leave [].
  Correct:   "how many hours on Clippy Vision this week?" ==> aggregation + [time_anchored, topic_search]
  Incorrect: "what did I do yesterday?" ==> time_anchored + [aggregation]  (aggregation not required)

TEMPORAL_HINT - populate whenever time_anchored appears in primary OR secondary.
Extract the exact time expression from the query: "yesterday", "this morning",
"last Tuesday", "3 days ago". Leave null if no time anchor exists.

PRIORITY ORDER (left wins when ambiguous):
  specific_recall > time_anchored > aggregation > topic_search > memory_query > casual > follow_up_inherit

---
CRITICAL BOUNDARIES
---

memory_query vs topic_search:
  "what projects have I told you about?"    ==> memory_query  (user told the assistant)
  "what have I been working on for Clippy?" ==> topic_search  (requires activity log)
  "what are my skills?"                     ==> memory_query  (stored identity fact)
  "what have I been doing with Python?"     ==> topic_search  (activity-based)

time_anchored vs topic_search:
  "what did I work on yesterday?"           ==> time_anchored  ("yesterday" = explicit anchor)
  "what was I doing this week?"             ==> time_anchored  ("this week" = calendar week)
  "what was I doing last week, in detail?"  ==> time_anchored  ("last week" = calendar-resolvable)
  "what was I up to last month?"            ==> time_anchored  ("last month" = calendar-resolvable)
  "what have I been working on lately?"     ==> topic_search   ("lately" = no specific date)
  "what was I doing before sleeping?"       ==> topic_search   (no calendar date resolvable)
  Rule: if you cannot resolve it to a specific calendar date or hour, use topic_search.
  Rule: "this week", "last week", "this month", "last month" ARE calendar anchors ==> time_anchored.

memory_query vs specific_recall:
  memory_query = facts the user TOLD the assistant in conversation (identity, preferences, projects mentioned).
  specific_recall = retrieving a specific artifact from the activity log (URL, error text, command, file name).
  "what was the error message I got when deploying?"  ==> specific_recall  (artifact from activity log)
  "what was the command I ran to set up the env?"     ==> specific_recall  (command = artifact from log)
  "what are my goals?"                                ==> memory_query     (told the assistant)
  "what languages do I know?"                         ==> memory_query     (identity fact)
  "what was the link I copied about React?"           ==> specific_recall  (URL artifact from clipboard log)
  Rule: if the answer is a specific piece of text/artifact the user produced on their computer, use specific_recall.
  Rule: if the answer is something the user declared about themselves in conversation, use memory_query.

---
EXAMPLES (CORRECT)
---

"what is 2+2?"
==> primary: casual, secondary: []

"what do you know about me?"
==> primary: memory_query, secondary: []

"what did I do yesterday?"
==> primary: time_anchored, secondary: [], temporal_hint: "yesterday"

"what was I reading this morning?"
==> primary: specific_recall, secondary: ["time_anchored"], temporal_hint: "this morning"

"what was I doing this week, in detail?"
==> primary: time_anchored, secondary: [], temporal_hint: "this week"

"what was I up to last month?"
==> primary: time_anchored, secondary: [], temporal_hint: "last month"

"what was the error message I got when deploying?"
==> primary: specific_recall, secondary: []

"what was the command I ran to set up the dev environment?"
==> primary: specific_recall, secondary: []

"what have I been working on for Clippy Vision?"
==> primary: topic_search, secondary: []

"what is the latest feature I was planning to add?"
==> primary: topic_search, secondary: []

"how many hours did I code this week?"
==> primary: aggregation, secondary: ["time_anchored"], temporal_hint: "this week"

"what did I work on yesterday and how many total hours?"
==> primary: time_anchored, secondary: ["aggregation"], temporal_hint: "yesterday"

"I need to write a report on the project I was working on last week."
==> primary: topic_search, secondary: ["time_anchored"], temporal_hint: "last week"
   (topic_search wins: the task is project retrieval; time anchor narrows the window)

[Prior turn: "what did I work on yesterday?"]
"what about the day before?"
==> primary: follow_up_inherit, secondary: ["time_anchored"], temporal_hint: "day before"

[Prior turn: "what was I planning for Clippy Vision?"]
"check it properly"
==> primary: follow_up_inherit, secondary: ["topic_search"]

---
EXAMPLES (WRONG - DO NOT DO THIS)
---

"do you remember what I did yesterday?" ==> primary: memory_query [WRONG]
  "Do you remember" sounds like memory but this is an activity question with a time anchor.
  Correct: primary: time_anchored

"what project have I been working on lately?" ==> primary: casual [WRONG]
  This requires searching the activity log.
  Correct: primary: topic_search

"what was I working on before sleeping?" ==> primary: time_anchored [WRONG]
  "Before sleeping" cannot be resolved to a calendar date.
  Correct: primary: topic_search

"what is 2+2?" ==> secondary: ["memory_query"] [WRONG]
  Math requires no personal data.
  Correct: secondary: []

"what project was I working on last week?" ==> primary: memory_query [WRONG]
  The answer is in the activity log, not in stored identity facts.
  Correct: primary: topic_search

"remember when I was debugging that weird crash last week? what was the error message?" ==> primary: topic_search [WRONG]
  "Error message" is a specific text artifact to retrieve, not a topic summary.
  "Remember" sounds like memory_query, but the answer is in the event log.
  Correct: primary: specific_recall, secondary: ["time_anchored", "topic_search"], temporal_hint: "last week"

"what was the command I ran to install node modules?" ==> primary: memory_query [WRONG]
  Commands the user ran are artifacts in the activity log, not facts told to the assistant.
  Correct: primary: specific_recall

"what was I doing this week?" ==> primary: topic_search [WRONG]
  "This week" is a calendar-resolvable time anchor.
  Correct: primary: time_anchored, temporal_hint: "this week"
```

OLLAMA_MODEL = "qwen3:8b"

def classify_query(query: str) -> RouterDecision:
    body = gateway.chat(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
        model=OLLAMA_MODEL,
        format=ROUTE_SCHEMA,
        think=False,
        options={"temperature": 0},
        priority=Priority.INTERACTIVE,
    )

    content = body["message"]["content"]
    parsed = json.loads(content) if isinstance(content, str) else content

    primary = parsed["primary"]
    secondary = parsed.get("secondary", [])

    has_time_anchor = primary == "time_anchored" or "time_anchored" in secondary
    raw_hint = parsed.get("temporal_hint")
    temporal_hint = (raw_hint if raw_hint and raw_hint != "null" else None) if has_time_anchor else None

    return RouterDecision(
        primary=primary,
        secondary=secondary,
        temporal_hint=temporal_hint,
        needs_memory_fetch=primary in ("memory_query", "topic_search", "casual"),
    )
"""


if __name__ == "__main__":
    while True:
        query = input("Enter a query: ")
        decision, confidence = classify_query(query)
        print(f"{decision}  (confidence={confidence:.2f})")