"""Quick diagnostic: ingest the stream into the Hybrid strategy and show what it
stored + what it retrieves for each query, so we can see why hit-rate is low.
"""

import _paths  # noqa: F401
import dataset
from strategies import Hybrid

h = Hybrid()
for t, fid, text, canon in dataset.stream():
    h.add(text, t)

print("\n=== TYPED CORE ===")
for slot, val in h.typed.items():
    print(f"  {slot}: {val}")

print("\n=== LONG TAIL ===")
for t, _ in h.longtail:
    print(f"  - {t}")

print("\n=== RETRIEVAL PER QUERY ===")
for q in dataset.QUERIES:
    hits = h.query(q["q"], k=5)
    print(f"\n[{q['id']}] {q['q']}")
    print(f"   expect={q.get('expect')} forbid={q.get('forbid')}")
    for hpos, ht in enumerate(hits):
        print(f"   {hpos + 1}. {ht}")
