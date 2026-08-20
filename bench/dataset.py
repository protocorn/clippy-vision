"""Frozen benchmark dataset for the memory bake-off.

Design rules (so the benchmark stays honest):
  - Written query-first: the QUERIES below describe what the agent must be able to
    answer; the FACT stream is the raw material. Do NOT tune this file after seeing
    results.
  - Each fact has a `canon` key = the canonical thing it is about. Facts sharing a
    canon should collapse to ONE durable entry after perfect consolidation. That is
    how IDEAL_SIZE is derived (no hand-picked number).
  - `t` is a synthetic monotonic timestamp (ordering matters for supersession).

Hard cases embedded on purpose:
  - exact duplicate + paraphrase    -> dedup           (canon "fav_language")
  - value changes over time         -> supersession    (canon "location", "employment", "drink")
  - many facts about one project    -> fragmentation    (canon "clippy", 8 facts -> ideally 1)
  - genuinely distinct facts        -> must NOT merge   (various)
  - multi-topic fact                -> straddles topics  (clippy + tooling)
"""

# (t, id, text, canon)
FACTS = [
    (1, "f01", "Sahil's name is Sahil.", "name"),
    (2, "f02", "Sahil lives in College Park, Maryland.", "location"),
    (3, "f03", "Sahil is currently looking for a full-time job.", "employment"),
    (
        4,
        "f04",
        "Sahil graduated from the University of Maryland, College Park.",
        "education",
    ),
    (5, "f05", "Sahil's favorite programming language is Python.", "fav_language"),
    (6, "f06", "Sahil drinks coffee every morning.", "drink"),
    (
        7,
        "f07",
        "Sahil is building Clippy_Vision, a personal AI activity monitor.",
        "clippy",
    ),
    (8, "f08", "In Clippy_Vision, Sahil is debugging the intent classifier.", "clippy"),
    (9, "f09", "Clippy_Vision runs local Ollama models like qwen3.", "clippy"),
    (
        10,
        "f10",
        "Sahil added a natural-language time parser to Clippy_Vision.",
        "clippy",
    ),
    (
        11,
        "f11",
        "Sahil also works on Launchway, a project about agentic workflows and RAG.",
        "launchway",
    ),
    (12, "f12", "Sahil prefers concise answers from his assistant.", "pref_concise"),
    (13, "f13", "Sahil uses Windows with PowerShell.", "os_shell"),
    (14, "f14", "Sahil's editor of choice is Cursor.", "editor"),
    (
        15,
        "f15",
        "Clippy_Vision's vision classifier uses qwen3-vl to read screenshots.",
        "clippy",
    ),
    (
        16,
        "f16",
        "Sahil is optimizing the SQL query generator in Clippy_Vision.",
        "clippy",
    ),
    (17, "f17", "Clippy_Vision stores activity events in SQLite.", "clippy"),
    (18, "f18", "Sahil enjoys playing chess.", "hobby_chess"),
    (19, "f19", "Sahil has a younger brother named Arjun.", "sibling"),
    (
        20,
        "f20",
        "Sahil's favorite programming language is Python.",
        "fav_language",
    ),  # exact duplicate of f05
    (
        21,
        "f21",
        "Python is Sahil's preferred language to code in.",
        "fav_language",
    ),  # paraphrase
    (
        22,
        "f22",
        "Sahil is refactoring the memory distiller in Clippy_Vision.",
        "clippy",
    ),
    (
        23,
        "f23",
        "While building Clippy_Vision, Sahil benchmarks Ollama models inside Cursor.",
        "clippy",
    ),  # multi-topic
    (24, "f24", "Sahil recently took up rock climbing on weekends.", "hobby_climbing"),
    # --- later updates that should SUPERSEDE earlier values ---
    (
        40,
        "f40",
        "Sahil moved to San Francisco, California.",
        "location",
    ),  # supersedes f02
    (
        45,
        "f45",
        "Sahil started a full-time job as an AI engineer at Acme Corp.",
        "employment",
    ),  # supersedes f03
    (
        48,
        "f48",
        "Sahil quit caffeine and now drinks herbal tea in the morning.",
        "drink",
    ),  # supersedes f06
]

# IDEAL_SIZE = number of distinct canonical things among durable facts.
IDEAL_SIZE = len({canon for (_, _, _, canon) in FACTS})

# How many durable entries SHOULD relate to Clippy_Vision after perfect consolidation.
# (Used for the fragmentation metric; ground truth is "about one project".)
IDEAL_CLIPPY_ITEMS = 1


# Each query: grade is "det" (deterministic substring check) or "judge" (LLM judge).
#   expect : substrings, at least one must appear in retrieved memory (det)
#   forbid : stale substrings that must NOT appear in retrieved memory (det, supersession)
#   answer : the ground-truth answer string (used by the LLM judge)
QUERIES = [
    {
        "id": "q01",
        "q": "What is Sahil's name?",
        "grade": "det",
        "expect": ["Sahil"],
        "forbid": [],
    },
    {
        "id": "q02",
        "q": "Where does Sahil live now?",
        "grade": "det",
        "expect": ["San Francisco"],
        "forbid": ["College Park, Maryland"],
    },
    {
        "id": "q03",
        "q": "What does Sahil do for work now?",
        "grade": "judge",
        "expect": ["Acme"],
        "forbid": ["looking for"],
        "answer": "AI engineer at Acme Corp",
    },
    {
        "id": "q04",
        "q": "Where did Sahil study?",
        "grade": "det",
        "expect": ["University of Maryland"],
        "forbid": [],
    },
    {
        "id": "q05",
        "q": "What is Sahil's favorite programming language?",
        "grade": "det",
        "expect": ["Python"],
        "forbid": [],
    },
    {
        "id": "q06",
        "q": "What project is Sahil building to monitor his activity?",
        "grade": "judge",
        "expect": ["Clippy_Vision"],
        "forbid": [],
        "answer": "Clippy_Vision, a personal AI activity monitor",
    },
    {
        "id": "q07",
        "q": "What models does Clippy_Vision use?",
        "grade": "judge",
        "expect": ["qwen3"],
        "forbid": [],
        "answer": "local Ollama models, qwen3 and qwen3-vl",
    },
    {
        "id": "q08",
        "q": "What other project does Sahil work on besides Clippy_Vision?",
        "grade": "det",
        "expect": ["Launchway"],
        "forbid": [],
    },
    {
        "id": "q09",
        "q": "What does Sahil drink in the morning now?",
        "grade": "det",
        "expect": ["tea"],
        "forbid": ["coffee"],
    },
    {
        "id": "q10",
        "q": "What are Sahil's hobbies?",
        "grade": "judge",
        "expect": ["chess"],
        "forbid": [],
        "answer": "chess and rock climbing",
    },
    {
        "id": "q11",
        "q": "What code editor does Sahil use?",
        "grade": "det",
        "expect": ["Cursor"],
        "forbid": [],
    },
    {
        "id": "q12",
        "q": "What operating system and shell does Sahil use?",
        "grade": "det",
        "expect": ["PowerShell"],
        "forbid": [],
    },
    {
        "id": "q13",
        "q": "Does Sahil have any siblings?",
        "grade": "det",
        "expect": ["Arjun"],
        "forbid": [],
    },
    {
        "id": "q14",
        "q": "How does Sahil like his assistant to respond?",
        "grade": "judge",
        "expect": ["concise"],
        "forbid": [],
        "answer": "concisely",
    },
    {
        "id": "q15",
        "q": "What database does Clippy_Vision use?",
        "grade": "det",
        "expect": ["SQLite"],
        "forbid": [],
    },
]


def stream():
    """Yield facts in timestamp order."""
    return sorted(FACTS, key=lambda r: r[0])
