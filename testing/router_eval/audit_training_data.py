"""Audit router training data: distribution, duplication, near-duplicates.

Near-duplicate rate matters because train/eval split is random — if generated
examples are heavily templated, eval accuracy overstates real performance.
"""

import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
FILES = [ROOT / "core" / "data" / "router_seed.jsonl",
         ROOT / "core" / "data" / "router_generated.jsonl"]

rows = []
for f in FILES:
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))

clean = [r for r in rows if not r.get("flagged")]
print(f"Total rows: {len(rows)}  clean (used in training): {len(clean)}")
print("\nPrimary distribution (clean):")
for cat, n in Counter(r["primary"] for r in clean).most_common():
    print(f"  {cat:<20} {n}")

def norm(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()

texts = [norm(r["text"]) for r in clean]
exact_dupes = sum(n - 1 for n in Counter(texts).values() if n > 1)
print(f"\nExact duplicates (after normalization): {exact_dupes}")

# Near-duplicate scan via first-4-words bucketing then SequenceMatcher
buckets = {}
for i, t in enumerate(texts):
    key = " ".join(t.split()[:4])
    buckets.setdefault(key, []).append(i)

near = 0
examples = []
for key, idxs in buckets.items():
    if len(idxs) < 2:
        continue
    for a in range(len(idxs)):
        for b in range(a + 1, len(idxs)):
            r = SequenceMatcher(None, texts[idxs[a]], texts[idxs[b]]).ratio()
            if r >= 0.85 and texts[idxs[a]] != texts[idxs[b]]:
                near += 1
                if len(examples) < 12:
                    examples.append((round(r, 2), clean[idxs[a]]["text"], clean[idxs[b]]["text"]))

print(f"Near-duplicate pairs (ratio>=0.85, same 4-word prefix): {near}")
for r, a, b in examples:
    print(f"  {r}  {a!r}  ~  {b!r}")

# Opening-phrase concentration
print("\nTop 15 opening trigrams:")
tri = Counter(" ".join(t.split()[:3]) for t in texts)
for k, n in tri.most_common(15):
    print(f"  {n:>4}  {k}")
