"""
Measure the marginal cost of one ReAct tool-selection round: a qwen3:8b call
with the real system prompt + TOOL_SCHEMAS that ends in a tool call.
This is (roughly) the latency the router's prefetch path can remove per round.
No tools are executed; no DB writes happen.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from agent.tools import TOOL_SCHEMAS
from core.llm_gateway import Priority, gateway

SYSTEM = (
    "You are Clippy, a local personal AI assistant. Answer from evidence using "
    "local memory and activity data. Activity questions require tools: any question "
    "about what the user did or was doing MUST call search_sessions first."
)

QUERIES = [
    "what did I do yesterday?",
    "how many hours did I code this week?",
    "what was the link I copied about React?",
    "what have I been working on for Clippy Vision?",
    "what did I work on this morning?",
]

times = []
for q in QUERIES:
    t0 = time.perf_counter()
    body = gateway.chat(
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": q},
        ],
        model="qwen3:8b",
        tools=TOOL_SCHEMAS,
        priority=Priority.INTERACTIVE,
        timeout=180,
        think=False,
    )
    ms = (time.perf_counter() - t0) * 1000
    tc = body["message"].get("tool_calls") or []
    times.append(ms)
    print(f"{ms:>8.0f} ms  tool_calls={[t['function']['name'] for t in tc]}  {q!r}")

print(f"\nMean tool-selection round: {sum(times) / len(times):.0f} ms")
