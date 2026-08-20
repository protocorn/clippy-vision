"""The four memory strategies under test.

Fairness contract:
  - All strategies share the SAME embedding model and the SAME LLM (via the gateway),
    temperature 0, and the SAME retrieval k.
  - The ONLY thing that varies is the write/consolidation policy (and, for clusters,
    the retrieval routing that is intrinsic to the idea).
  - Each strategy counts its own LLM and embedding calls so cost is comparable.
  - Embedding cache is PER-strategy, so no strategy free-rides on another's work.
"""

import json
import math
import time

import _paths  # noqa: F401  (sets up sys.path)

from core.llm_gateway import Priority, gateway

MODEL = "qwen3:8b"


def _cos(a, b):
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class MemoryStrategy:
    name = "base"

    def __init__(self):
        self.llm_calls = 0
        self.embed_calls = 0
        self.add_seconds = 0.0
        self._emb_cache = {}

    # --- instrumented primitives -------------------------------------------
    def _embed(self, text):
        cached = self._emb_cache.get(text)
        if cached is not None:
            return cached
        self.embed_calls += 1
        vec = gateway.embed(text, priority=Priority.FOREGROUND)
        self._emb_cache[text] = vec
        return vec

    def _chat_json(self, system, user, schema):
        self.llm_calls += 1
        body = gateway.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            MODEL,
            format=schema,
            think=False,
            options={"temperature": 0},
            priority=Priority.FOREGROUND,
        )
        content = body["message"]["content"]
        return json.loads(content) if isinstance(content, str) else content

    # --- public API (timed) ------------------------------------------------
    def add(self, fact, ts):
        t0 = time.time()
        self._add(fact, ts)
        self.add_seconds += time.time() - t0

    def _add(self, fact, ts):
        raise NotImplementedError

    def query(self, text, k=5):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    @staticmethod
    def _topk(query_vec, items, k):
        """items: list of (text, vec). Returns top-k texts by cosine."""
        ranked = sorted(items, key=lambda it: _cos(query_vec, it[1]), reverse=True)
        return [t for t, _ in ranked[:k]]


# ---------------------------------------------------------------------------
# Control: append-only (today's behavior). No dedup, no supersession.
# ---------------------------------------------------------------------------
class Control(MemoryStrategy):
    name = "control (append-only)"

    def __init__(self):
        super().__init__()
        self.items = []  # (text, vec)

    def _add(self, fact, ts):
        self.items.append((fact, self._embed(fact)))

    def query(self, text, k=5):
        return self._topk(self._embed(text), self.items, k)

    def dump(self):
        return [t for t, _ in self.items]


# ---------------------------------------------------------------------------
# A: online threshold clustering. Groups for retrieval; does NOT merge text.
# ---------------------------------------------------------------------------
_LABEL_SYS = (
    "Give a short 1-3 word snake_case label for the topic of this fact about a user. "
    'Return JSON {"label": "..."}.'
)
_LABEL_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}},
    "required": ["label"],
}


class Clusters(MemoryStrategy):
    name = "A: clusters"
    THRESHOLD = 0.62

    def __init__(self):
        super().__init__()
        self.clusters = []  # {label, centroid, items:[(text,vec)]}

    def _add(self, fact, ts):
        v = self._embed(fact)
        best, best_sim = None, -1.0
        for c in self.clusters:
            sim = _cos(v, c["centroid"])
            if sim > best_sim:
                best, best_sim = c, sim
        if best is None or best_sim < self.THRESHOLD:
            label = self._chat_json(_LABEL_SYS, fact, _LABEL_SCHEMA).get(
                "label", "misc"
            )
            self.clusters.append(
                {"label": label, "centroid": list(v), "items": [(fact, v)]}
            )
        else:
            best["items"].append((fact, v))
            n = len(best["items"])
            best["centroid"] = [
                (c * (n - 1) + x) / n for c, x in zip(best["centroid"], v)
            ]

    def query(self, text, k=5):
        qv = self._embed(text)
        routed = sorted(
            self.clusters, key=lambda c: _cos(qv, c["centroid"]), reverse=True
        )[:2]
        pool = [it for c in routed for it in c["items"]]
        return self._topk(qv, pool, k)

    def dump(self):
        return [t for c in self.clusters for t, _ in c["items"]]


# ---------------------------------------------------------------------------
# B: flat RAG + LLM write-decision (Mem0-style ADD / UPDATE / NOOP).
# ---------------------------------------------------------------------------
_WRITE_SYS = (
    "You maintain a list of durable facts about a user. Given a NEW fact and the most "
    "SIMILAR existing facts (each with its index), choose ONE action:\n"
    "- NOOP: the new fact is already represented (exact duplicate or paraphrase).\n"
    "- UPDATE: the new fact supersedes or refines exactly one existing fact; set "
    "target_index to that fact's index and text to the single best up-to-date fact.\n"
    "- ADD: the new fact is genuinely new information.\n"
    'Return JSON {"action", "target_index", "text"}. For ADD use target_index null and '
    "text = the new fact. Never invent facts."
)
_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["ADD", "UPDATE", "NOOP"]},
        "target_index": {"type": ["integer", "null"]},
        "text": {"type": "string"},
    },
    "required": ["action"],
}


def _decide_and_apply(strategy, items, fact, vec):
    """Shared Mem0-style write decision over `items` (list of [text, vec])."""
    if not items:
        items.append([fact, vec])
        return
    order = sorted(
        range(len(items)), key=lambda i: _cos(vec, items[i][1]), reverse=True
    )[:3]
    similar = [{"index": i, "text": items[i][0]} for i in order]
    out = strategy._chat_json(
        _WRITE_SYS, json.dumps({"new_fact": fact, "similar": similar}), _WRITE_SCHEMA
    )
    action = (out.get("action") or "ADD").upper()
    target = out.get("target_index")
    text = out.get("text") or fact
    if action == "NOOP":
        return
    if action == "UPDATE" and isinstance(target, int) and 0 <= target < len(items):
        items[target] = [text, strategy._embed(text)]
        return
    items.append([fact, vec])


class FlatRAG(MemoryStrategy):
    name = "B: flat-rag + llm"

    def __init__(self):
        super().__init__()
        self.items = []  # [text, vec]

    def _add(self, fact, ts):
        _decide_and_apply(self, self.items, fact, self._embed(fact))

    def query(self, text, k=5):
        return self._topk(self._embed(text), [(t, v) for t, v in self.items], k)

    def dump(self):
        return [t for t, _ in self.items]


# ---------------------------------------------------------------------------
# C: hybrid typed-core + long-tail. Known slots are deterministic UPSERTs;
#    everything else falls back to the Mem0-style long-tail store.
# ---------------------------------------------------------------------------
_ROUTE_SYS = (
    "Classify a fact about a user into a profile slot. Slots:\n"
    "name, location, employment, education, editor, os_shell, fav_language, hobbies.\n"
    "If the fact clearly fits one slot, return that slot and the concise value to store. "
    'If it does not fit any, return slot "none".\n'
    'Return JSON {"slot", "value"}.'
)
_ROUTE_SCHEMA = {
    "type": "object",
    "properties": {"slot": {"type": "string"}, "value": {"type": "string"}},
    "required": ["slot"],
}


class Hybrid(MemoryStrategy):
    name = "C: hybrid"
    SCALARS = {
        "name",
        "location",
        "employment",
        "education",
        "editor",
        "os_shell",
        "fav_language",
    }
    LISTS = {"hobbies"}

    def __init__(self):
        super().__init__()
        self.typed = {}  # slot -> value | [values]
        self.longtail = []  # [text, vec]

    def _add(self, fact, ts):
        vec = self._embed(fact)
        out = self._chat_json(_ROUTE_SYS, fact, _ROUTE_SCHEMA)
        slot = (out.get("slot") or "none").strip().lower()
        value = (out.get("value") or fact).strip()
        if slot in self.SCALARS:
            self.typed[slot] = value  # overwrite == supersession
        elif slot in self.LISTS:
            lst = self.typed.setdefault(slot, [])
            if value.lower() not in {x.lower() for x in lst}:
                lst.append(value)
        else:
            _decide_and_apply(self, self.longtail, fact, vec)

    def _typed_items(self):
        """Compact form, used for the memory dump / size metric."""
        out = []
        for slot, val in self.typed.items():
            if isinstance(val, list):
                out.extend(f"{slot}: {x}" for x in val)
            else:
                out.append(f"{slot}: {val}")
        return out

    def _typed_sentences(self):
        """Natural-language form, used for retrieval so typed slots embed comparably
        to full-sentence long-tail facts (terse 'slot: value' lines embed poorly)."""
        out = []
        for slot, val in self.typed.items():
            label = slot.replace("_", " ")
            values = val if isinstance(val, list) else [val]
            for x in values:
                out.append(f"Sahil's {label} is {x}.")
        return out

    def _pool(self):
        return [(t, self._embed(t)) for t in self._typed_sentences()] + [
            (t, v) for t, v in self.longtail
        ]

    def query(self, text, k=5):
        return self._topk(self._embed(text), self._pool(), k)

    def dump(self):
        return self._typed_items() + [t for t, _ in self.longtail]


# ---------------------------------------------------------------------------
# A2: clusters + merge. Route to nearest cluster, then run the Mem0-style write
#     decision scoped to that cluster's members (local merge instead of blind append).
# ---------------------------------------------------------------------------
class ClustersMerge(MemoryStrategy):
    name = "A2: clusters+merge"
    THRESHOLD = 0.62

    def __init__(self):
        super().__init__()
        self.clusters = []  # {label, centroid, items:[[text, vec]]}

    def _recentroid(self, c):
        n = len(c["items"])
        if n == 0:
            return
        dim = len(c["items"][0][1])
        c["centroid"] = [sum(it[1][i] for it in c["items"]) / n for i in range(dim)]

    def _add(self, fact, ts):
        v = self._embed(fact)
        best, best_sim = None, -1.0
        for c in self.clusters:
            sim = _cos(v, c["centroid"])
            if sim > best_sim:
                best, best_sim = c, sim
        if best is None or best_sim < self.THRESHOLD:
            label = self._chat_json(_LABEL_SYS, fact, _LABEL_SCHEMA).get(
                "label", "misc"
            )
            self.clusters.append(
                {"label": label, "centroid": list(v), "items": [[fact, v]]}
            )
        else:
            _decide_and_apply(self, best["items"], fact, v)
            self._recentroid(best)

    def query(self, text, k=5):
        qv = self._embed(text)
        routed = sorted(
            self.clusters, key=lambda c: _cos(qv, c["centroid"]), reverse=True
        )[:2]
        pool = [(it[0], it[1]) for c in routed for it in c["items"]]
        return self._topk(qv, pool, k)

    def dump(self):
        return [it[0] for c in self.clusters for it in c["items"]]


def all_strategies():
    return [Control(), Clusters(), ClustersMerge(), FlatRAG(), Hybrid()]
