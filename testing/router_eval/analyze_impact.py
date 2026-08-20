"""
Production-impact analysis of the MiniLM router records.

A misclassification only matters through what the prefetch gate DOES:
  - harmful fire:  gate fires with the wrong action group -> we prefetch the
                   wrong data source (wasted latency, polluted context)
  - useless fire:  gate fires on a casual/follow_up query -> pure waste
  - benign fire:   gate fires, label wrong, but same action group -> prefetch
                   still pulls from the right store
  - missed fire:   prefetchable query held back -> no harm, just no speedup

Also sweeps per-category thresholds to suggest better values.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent
records = [
    json.loads(line)
    for line in (HERE / "results" / "records_minilm.jsonl")
    .read_text(encoding="utf-8")
    .splitlines()
    if line.strip()
]

PREFETCH_THRESHOLDS = {
    "aggregation": 0.50,
    "memory_query": 0.50,
    "time_anchored": 0.55,
    "specific_recall": 0.50,
    "topic_search": 0.30,
}
ACTION_GROUP = {
    "time_anchored": "activity_log",
    "topic_search": "activity_log",
    "aggregation": "activity_log",
    "specific_recall": "activity_log_fine",
    "memory_query": "memory",
    "casual": "none",
    "follow_up_inherit": "none",
}

fired = [
    r
    for r in records
    if PREFETCH_THRESHOLDS.get(r["pred"]) is not None
    and r["conf"] >= PREFETCH_THRESHOLDS[r["pred"]]
]

harmful, useless, benign, exact = [], [], [], []
for r in fired:
    if r["gold"] == r["pred"]:
        exact.append(r)
    elif ACTION_GROUP[r["gold"]] in ("none",):
        useless.append(r)
    elif ACTION_GROUP[r["gold"]] == ACTION_GROUP[r["pred"]]:
        benign.append(r)
    else:
        harmful.append(r)

n = len(records)
print(f"Total queries: {n} | gate fired: {len(fired)} ({len(fired) / n:.0%})")
print(f"  exact-correct fires: {len(exact)}")
print(f"  benign fires (wrong label, same data source): {len(benign)}")
print(f"  useless fires (query was casual/follow-up):   {len(useless)}")
print(f"  harmful fires (wrong data source prefetched): {len(harmful)}")

for name, group in [("USELESS", useless), ("HARMFUL", harmful)]:
    if group:
        print(f"\n{name} fires:")
        for r in group:
            print(
                f"  conf={r['conf']:.2f}  pred={r['pred']:<16} gold={r['gold']:<16} {r['text'][:60]!r}"
            )

# Per-category threshold sweep
print(
    "\n\nPer-category threshold sweep (fired-count / exact precision / group precision):"
)
for cat in PREFETCH_THRESHOLDS:
    print(f"\n  pred={cat}  (current thr={PREFETCH_THRESHOLDS[cat]})")
    preds = [r for r in records if r["pred"] == cat]
    for thr in [0.25, 0.30, 0.40, 0.50, 0.55, 0.60, 0.70]:
        sub = [r for r in preds if r["conf"] >= thr]
        if not sub:
            print(f"    thr={thr:.2f}  fired=0")
            continue
        p = sum(r["gold"] == cat for r in sub) / len(sub)
        gp = sum(ACTION_GROUP[r["gold"]] == ACTION_GROUP[cat] for r in sub) / len(sub)
        print(f"    thr={thr:.2f}  fired={len(sub):>3}  exact={p:.0%}  group={gp:.0%}")
