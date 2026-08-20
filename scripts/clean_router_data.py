"""
Clean the router training data, per testing/router_eval/FINDINGS.md.

Steps (in order):
  1. Strip the multi-turn "User:/Clippy:" format from NON-follow_up_inherit rows —
     the generation rule says only follow_up_inherit may use it. The last user turn
     becomes the text; rows whose last turn is too short to stand alone are dropped.
  2. Drop rows that near-duplicate the frozen golden eval set (leakage guard).
  3. Dedupe near-duplicates within the training data (SequenceMatcher >= 0.85 on
     normalized text, same-label only; seeds always win over generated rows).
  4. Cap over-represented categories via seeded random subsample (generated rows
     only; seeds are never dropped).

Writes the cleaned rows back to router_generated.jsonl (backup saved first).
router_seed.jsonl is never modified.

Usage:
    python scripts/clean_router_data.py --dry-run
    python scripts/clean_router_data.py --cap 250
"""

import argparse
import json
import random
import re
import shutil
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).parent.parent
SEED_FILE = ROOT / "core" / "data" / "router_seed.jsonl"
GENERATED_FILE = ROOT / "core" / "data" / "router_generated.jsonl"
GOLDEN_FILE = ROOT / "testing" / "router_eval" / "golden_set.jsonl"

SEED = 42
NEAR_DUP_RATIO = 0.85
GOLDEN_LEAK_RATIO = 0.90
MIN_STANDALONE_WORDS = 4

_USER_SPLIT_RE = re.compile(r"(?i)\buser:\s*")


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def norm(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def is_multiturn(text: str) -> bool:
    return (
        bool(_USER_SPLIT_RE.match(text.strip()))
        or "\nUser:" in text
        or "\nuser:" in text
    )


def last_user_turn(text: str) -> str:
    parts = _USER_SPLIT_RE.split(text)
    return parts[-1].strip() if parts else text.strip()


def similar(a: str, b: str, threshold: float) -> bool:
    # cheap length prefilter before the expensive ratio
    if abs(len(a) - len(b)) > max(len(a), len(b)) * (1 - threshold):
        return False
    m = SequenceMatcher(None, a, b)
    if m.real_quick_ratio() < threshold or m.quick_ratio() < threshold:
        return False
    return m.ratio() >= threshold


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cap",
        type=int,
        default=250,
        help="Max generated+seed rows per category after cleaning (default: 250)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    seeds = load_jsonl(SEED_FILE)
    generated = load_jsonl(GENERATED_FILE)
    golden_norms = [norm(r["text"]) for r in load_jsonl(GOLDEN_FILE)]

    print(f"Input: {len(seeds)} seed rows, {len(generated)} generated rows")

    # ── Step 1: strip multi-turn format from non-follow_up rows ──
    stripped, dropped_short = 0, 0
    step1 = []
    for r in generated:
        if r.get("primary") != "follow_up_inherit" and is_multiturn(r["text"]):
            turn = last_user_turn(r["text"])
            if len(turn.split()) < MIN_STANDALONE_WORDS:
                dropped_short += 1
                continue
            r = dict(r)
            r["text"] = turn
            stripped += 1
        step1.append(r)
    print(
        f"[1] multi-turn stripped: {stripped}, dropped (last turn too short): {dropped_short}"
    )

    # ── Step 2: golden-set leakage guard ──
    step2 = []
    leaked = 0
    for r in step1:
        n = norm(r["text"])
        if any(similar(n, g, GOLDEN_LEAK_RATIO) for g in golden_norms):
            leaked += 1
            continue
        step2.append(r)
    print(f"[2] dropped as golden-set near-dups (>= {GOLDEN_LEAK_RATIO}): {leaked}")

    # ── Step 3: near-dup dedupe (seeds first so they always win) ──
    kept: list[dict] = []
    kept_norm_by_bucket: dict[
        str, list[tuple[str, str]]
    ] = {}  # bucket -> [(norm, label)]
    exact_seen: set[str] = set()
    dupes_exact = dupes_near = 0

    def bucket_key(n: str) -> str:
        return " ".join(n.split()[:4])

    for r, is_seed in [(r, True) for r in seeds] + [(r, False) for r in step2]:
        n = norm(r["text"])
        label = r.get("primary", "")
        if n in exact_seen:
            dupes_exact += 1
            continue
        bk = bucket_key(n)
        # same-label near-dup within the same 4-word-prefix bucket => drop
        if any(
            similar(n, kn, NEAR_DUP_RATIO)
            for kn, kl in kept_norm_by_bucket.get(bk, [])
            if kl == label
        ):
            dupes_near += 1
            continue
        exact_seen.add(n)
        kept_norm_by_bucket.setdefault(bk, []).append((n, label))
        kept.append({**r, "_is_seed": is_seed})
    print(
        f"[3] removed exact dupes: {dupes_exact}, near dupes (>= {NEAR_DUP_RATIO}, same label): {dupes_near}"
    )

    # ── Step 4: cap over-represented categories (drop generated rows only) ──
    rng = random.Random(SEED)
    by_cat: dict[str, list[dict]] = {}
    for r in kept:
        by_cat.setdefault(r["primary"], []).append(r)

    final: list[dict] = []
    for cat, rows in by_cat.items():
        if len(rows) <= args.cap:
            final.extend(rows)
            continue
        seed_rows = [r for r in rows if r["_is_seed"]]
        gen_rows = [r for r in rows if not r["_is_seed"]]
        n_keep_gen = max(0, args.cap - len(seed_rows))
        rng.shuffle(gen_rows)
        final.extend(seed_rows + gen_rows[:n_keep_gen])
        print(f"[4] capped {cat}: {len(rows)} -> {len(seed_rows) + n_keep_gen}")
    final.sort(key=lambda r: (r["primary"], r["text"]))

    print("\nFinal distribution (seed + generated):")
    for cat, n in Counter(r["primary"] for r in final).most_common():
        print(f"  {cat:<20} {n}")
    print(f"Total: {len(final)}")

    if args.dry_run:
        print("\n[dry-run] no files written")
        return

    # seeds stay in router_seed.jsonl; only write generated rows back
    out_rows = [r for r in final if not r["_is_seed"]]
    for r in out_rows:
        r.pop("_is_seed", None)

    backup = GENERATED_FILE.with_suffix(".jsonl.bak")
    if not backup.exists():
        shutil.copy(GENERATED_FILE, backup)
        print(f"\nBackup: {backup}")
    with GENERATED_FILE.open("w", encoding="utf-8") as f:
        for r in out_rows:
            f.write(json.dumps(r) + "\n")
    print(f"Wrote {len(out_rows)} generated rows -> {GENERATED_FILE}")


if __name__ == "__main__":
    main()
