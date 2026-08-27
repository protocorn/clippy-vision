"""
Router training data generator.

Generates diverse labeled examples per category using qwen3:8b, auto-labels them
via classify_query(), flags mismatches, and appends to router_generated.jsonl.

Usage:
    python scripts/generate_router_data.py                  # 20 examples per category
    python scripts/generate_router_data.py --per-category 30
    python scripts/generate_router_data.py --categories time_anchored topic_search
    python scripts/generate_router_data.py --review-flagged  # show flagged rows only
"""

import argparse
import json
import random
import sys
import time
import uuid
from pathlib import Path

# --- Path setup ---
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "core"))

from agent.router import OLLAMA_MODEL, classify_query
from core.llm_gateway import Priority, gateway

# --- Paths ---
SEED_FILE      = ROOT / "core" / "data" / "router_seed.jsonl"
OUTPUT_FILE    = ROOT / "core" / "data" / "router_generated.jsonl"
POLICY_FILE    = ROOT / "docs" / "router_labelling_policy.md"

# --- Config ---
CATEGORIES = [
    "time_anchored",
    "topic_search",
    "aggregation",
    "specific_recall",
    "memory_query",
    "casual",
    "follow_up_inherit",
]

SEEDS_PER_CATEGORY_IN_PROMPT = 5  # how many seed examples to show qwen per generation call

# Topic domains injected into generation prompt to prevent coding monoculture
TOPIC_DIVERSITY_INSTRUCTION = """
Topic domains to use (rotate across all of these — do NOT focus only on coding):
  - Student:         essays, research papers, studying, online courses, lecture notes
  - Designer:        Figma, Canva, Photoshop, mockups, color palettes, UI projects
  - Business/office: emails, spreadsheets, presentations, meetings, reports, Slack
  - Writer:          articles, blog posts, editing, research, drafts, publishing
  - General/personal: recipes, shopping, news, social media, YouTube, travel planning
  - Health/fitness:  nutrition, workout tracking, medical research, habit apps
  - Developer:       coding, debugging, GitHub, terminals, APIs (max 35% of examples)

At least 4 out of every 10 examples must use a non-developer domain.
"""

# --- Generation schema ---
GENERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "examples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text":         {"type": "string"},
                    "primary":      {"type": "string"},
                    "secondary":    {"type": "array", "items": {"type": "string"}},
                    "temporal_hint": {"type": "string"},
                },
                "required": ["text", "primary", "secondary"],
            },
        }
    },
    "required": ["examples"],
}


# ─────────────────────────────────────────────────────────────
# Load helpers
# ─────────────────────────────────────────────────────────────

def load_policy() -> str:
    if not POLICY_FILE.exists():
        print(f"[warn] Policy file not found at {POLICY_FILE}. Proceeding without it.")
        return ""
    return POLICY_FILE.read_text(encoding="utf-8")


def load_seeds() -> dict[str, list[dict]]:
    """Load seed examples grouped by primary category."""
    seeds: dict[str, list[dict]] = {cat: [] for cat in CATEGORIES}
    if not SEED_FILE.exists():
        print(f"[warn] Seed file not found at {SEED_FILE}.")
        return seeds
    for line in SEED_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ex = json.loads(line)
            cat = ex.get("primary")
            if cat in seeds:
                seeds[cat].append(ex)
        except json.JSONDecodeError:
            pass
    return seeds


def load_generated() -> list[dict]:
    """Load previously generated examples (for deduplication)."""
    examples = []
    if not OUTPUT_FILE.exists():
        return examples
    for line in OUTPUT_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                examples.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return examples


# ─────────────────────────────────────────────────────────────
# Generation prompt builder
# ─────────────────────────────────────────────────────────────

def _format_seed(ex: dict) -> str:
    parts = [f'  text: "{ex["text"]}"']
    if ex.get("context"):
        parts.append(f'  context: "{ex["context"]}"')
    parts.append(f'  primary: {ex["primary"]}')
    parts.append(f'  secondary: {json.dumps(ex.get("secondary", []))}')
    hint = ex.get("temporal_hint")
    parts.append(f'  temporal_hint: {json.dumps(hint)}')
    return "\n".join(parts)


def build_generation_prompt(category: str, seeds: list[dict], policy: str, n: int, existing_texts: set[str]) -> str:
    seed_block = "\n\n".join(_format_seed(s) for s in seeds[:SEEDS_PER_CATEGORY_IN_PROMPT])

    existing_hint = ""
    if existing_texts:
        sample = list(existing_texts)[:10]
        existing_hint = (
            "\nAlready generated (do NOT repeat these or similar phrasings):\n"
            + "\n".join(f'  - "{t}"' for t in sample)
        )

    prompt = f"""You are generating training data for a query router classifier.

---
LABELING POLICY (follow strictly)
---
{policy[:3000]}
[...policy truncated for brevity — follow the rules above...]

---
TOPIC DOMAINS
---
{TOPIC_DIVERSITY_INSTRUCTION}

---
TASK
---
Generate exactly {n} unique, realistic user queries for the category: {category}

Requirements:
- Each query must clearly belong to the {category} category per the policy above.
- CONVERSATION FORMAT RULE: The "User: ... Clippy: ... User: ..." multi-turn format
  MUST ONLY be used for follow_up_inherit examples. ALL other categories MUST be plain
  single-turn text. NEVER wrap casual, memory_query, time_anchored, topic_search,
  aggregation, or specific_recall examples in conversation format.
- PURITY RULE: Every query must have ONE dominant signal, no mixed signals:
    * time_anchored: clear time word (yesterday, last week, this morning) but NO specific
      artifact — "what did I search for this week?" is specific_recall, not time_anchored.
    * memory_query: facts the user stated in conversation (name, job, skills). No time
      anchors, no activity references, plain single-turn text only.
    * casual: requires zero personal data, plain single-turn question only. No conversation
      context, no references to the user's own computer activity.
    * follow_up_inherit: must be incomplete/vague with no standalone meaning. ONLY this
      category uses the multi-turn format.
    * topic_search: topic/project question, no calendar time anchor, no specific artifact.
- Use varied sentence structures, lengths, and topics.
- Rotate across the topic domains listed above. At least 4 out of 10 examples must use a non-developer domain.
- Include at least 2 examples with realistic spelling errors or typos.
- Include at least 1 vague or indirect phrasing.
- For follow_up_inherit: include the prior turn as context in the "text" field,
  formatted as "User: [prior]\nClipper: [actual response]\nUser: [follow-up]". Use a
  realistic Clippy response — never write "[response]" as a placeholder.
- Do NOT repeat patterns from the seed examples or already-generated examples below.
- secondary: add ONLY when two distinct retrieval strategies are genuinely required.
- temporal_hint: extract exact time phrase only when time_anchored is primary or secondary.

Seed examples for {category}:
{seed_block}
{existing_hint}

Return a JSON object with an "examples" array. Each item has:
  text (string), primary (string), secondary (array), temporal_hint (string or null)
"""
    return prompt


# ─────────────────────────────────────────────────────────────
# Core generation
# ─────────────────────────────────────────────────────────────

def generate_for_category(
    category: str,
    seeds: list[dict],
    policy: str,
    n: int,
    existing_texts: set[str],
) -> list[dict]:
    """Ask qwen to generate n examples for the given category. Returns raw parsed list."""
    prompt = build_generation_prompt(category, seeds, policy, n, existing_texts)

    body = gateway.chat(
        messages=[{"role": "user", "content": prompt}],
        model=OLLAMA_MODEL,
        format=GENERATION_SCHEMA,
        think=False,
        options={"temperature": 0.7},   # some creativity for diversity
        priority=Priority.BACKGROUND,
    )

    content = body["message"]["content"]
    parsed = json.loads(content) if isinstance(content, str) else content
    return parsed.get("examples", [])


def consistency_check(text: str, expected_primary: str) -> bool:
    """Re-classify the generated text and return True if it matches expected_primary."""
    try:
        decision = classify_query(text)
        return decision.primary == expected_primary
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# Batch runner
# ─────────────────────────────────────────────────────────────

def run_batch(categories: list[str], per_category: int) -> None:
    policy  = load_policy()
    seeds   = load_seeds()
    already = load_generated()

    existing_texts: set[str] = {ex["text"] for ex in already}
    for line in SEED_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            existing_texts.add(json.loads(line)["text"])
        except (json.JSONDecodeError, KeyError):
            pass

    total_generated = 0
    total_flagged   = 0

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT_FILE.open("a", encoding="utf-8") as out:
        for category in categories:
            print(f"\n[gen] Generating {per_category} examples for: {category}")
            cat_seeds = seeds.get(category, [])
            if not cat_seeds:
                print(f"  [warn] No seeds found for {category} — generation quality may be lower.")

            try:
                examples = generate_for_category(
                    category, cat_seeds, policy, per_category, existing_texts
                )
            except Exception as e:
                print(f"  [error] Generation failed for {category}: {e}")
                continue

            accepted = skipped = flagged = 0
            for ex in examples:
                text = ex.get("text", "").strip()
                if not text or text in existing_texts:
                    skipped += 1
                    continue

                # Normalise fields
                primary      = ex.get("primary", category)
                secondary    = ex.get("secondary", [])
                raw_hint     = ex.get("temporal_hint")
                has_time     = primary == "time_anchored" or "time_anchored" in secondary
                temporal_hint = (raw_hint if raw_hint and raw_hint not in ("null", "None") else None) if has_time else None

                # Consistency check — re-classify and compare
                is_consistent = consistency_check(text, primary)
                flag = not is_consistent

                row = {
                    "id":            str(uuid.uuid4())[:8],
                    "text":          text,
                    "context":       ex.get("context", ""),
                    "primary":       primary,
                    "secondary":     secondary,
                    "temporal_hint": temporal_hint,
                    "source":        "generated",
                    "reviewed":      False,
                    "flagged":       flag,
                }
                out.write(json.dumps(row) + "\n")
                existing_texts.add(text)

                accepted += 1
                if flag:
                    flagged += 1
                    print(f"  [flag] Mismatch: \"{text[:60]}...\" — generated as {primary}, router returned different")

            total_generated += accepted
            total_flagged   += flagged
            print(f"  accepted={accepted}  skipped={skipped}  flagged={flagged}")

    print(f"\n[done] Total generated: {total_generated}  |  Flagged for review: {total_flagged}")
    print(f"       Output: {OUTPUT_FILE}")
    if total_flagged > 0:
        print(f"\n  Run with --review-flagged to inspect flagged examples.")


# ─────────────────────────────────────────────────────────────
# Review mode
# ─────────────────────────────────────────────────────────────

def review_flagged() -> None:
    examples = load_generated()
    flagged  = [ex for ex in examples if ex.get("flagged")]
    if not flagged:
        print("No flagged examples found.")
        return

    print(f"\n{len(flagged)} flagged examples:\n")
    for ex in flagged:
        print(f"  id:       {ex.get('id', '?')}")
        print(f"  text:     {ex['text'][:100]}")
        print(f"  labeled:  primary={ex['primary']}  secondary={ex.get('secondary', [])}")
        print()


def fix_flagged(mode: str = "relabel") -> None:
    """
    Re-evaluate all flagged rows using the current router.

    mode="relabel"  : update the row's label to whatever the router now returns,
                      clear the flag and mark reviewed=True.
    mode="drop"     : remove rows where the router still disagrees with the original label.
    mode="accept"   : clear flags without re-running the router (manual bulk accept).
    """
    examples = load_generated()
    unflagged   = [ex for ex in examples if not ex.get("flagged")]
    flagged     = [ex for ex in examples if ex.get("flagged")]

    if not flagged:
        print("No flagged examples found — nothing to do.")
        return

    print(f"\n[fix] Mode={mode} | Processing {len(flagged)} flagged rows...")

    kept = 0
    dropped = 0
    relabeled = 0
    accepted = 0
    fixed_rows: list[dict] = []

    for ex in flagged:
        text = ex["text"]

        if mode == "accept":
            ex["flagged"]  = False
            ex["reviewed"] = True
            fixed_rows.append(ex)
            accepted += 1
            continue

        try:
            decision = classify_query(text)
        except Exception as e:
            print(f"  [error] classify failed for '{text[:50]}': {e} — keeping as-is")
            fixed_rows.append(ex)
            kept += 1
            continue

        router_primary = decision.primary

        if router_primary == ex["primary"]:
            # Router now agrees with the original label — clear flag
            ex["flagged"]  = False
            ex["reviewed"] = True
            fixed_rows.append(ex)
            kept += 1
            print(f"  [ok]      router agrees: {router_primary}  '{text[:60]}'")
        elif mode == "relabel":
            # Update to router's classification
            has_time = router_primary == "time_anchored" or "time_anchored" in decision.secondary
            ex["primary"]       = router_primary
            ex["secondary"]     = decision.secondary
            ex["temporal_hint"] = decision.temporal_hint if has_time else None
            ex["flagged"]       = False
            ex["reviewed"]      = True
            fixed_rows.append(ex)
            relabeled += 1
            print(f"  [relabel] {ex.get('_orig_primary', '?')} ==> {router_primary}  '{text[:60]}'")
        else:  # mode == "drop"
            dropped += 1
            print(f"  [drop]    still mismatch (router={router_primary})  '{text[:60]}'")

    # Rebuild full file
    all_rows = unflagged + fixed_rows
    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        for row in all_rows:
            f.write(json.dumps(row) + "\n")

    print(f"\n[done] kept={kept}  relabeled={relabeled}  accepted={accepted}  dropped={dropped}")
    print(f"       Total rows now: {len(all_rows)}")
    flagged_remaining = sum(1 for r in all_rows if r.get("flagged"))
    print(f"       Still flagged:  {flagged_remaining}")


# ─────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────

def print_stats() -> None:
    seed_data      = load_seeds()
    generated_data = load_generated()

    seed_counts = {cat: len(v) for cat, v in seed_data.items()}
    gen_counts: dict[str, int] = {cat: 0 for cat in CATEGORIES}
    gen_flagged: dict[str, int] = {cat: 0 for cat in CATEGORIES}

    for ex in generated_data:
        cat = ex.get("primary", "unknown")
        if cat in gen_counts:
            gen_counts[cat] += 1
            if ex.get("flagged"):
                gen_flagged[cat] += 1

    print(f"\n{'Category':<22} {'Seeds':>6} {'Generated':>10} {'Flagged':>8} {'Total':>6}")
    print("-" * 58)
    for cat in CATEGORIES:
        s = seed_counts.get(cat, 0)
        g = gen_counts.get(cat, 0)
        f = gen_flagged.get(cat, 0)
        print(f"  {cat:<20} {s:>6} {g:>10} {f:>8} {s+g:>6}")
    print("-" * 58)
    total_s = sum(seed_counts.values())
    total_g = sum(gen_counts.values())
    total_f = sum(gen_flagged.values())
    print(f"  {'TOTAL':<20} {total_s:>6} {total_g:>10} {total_f:>8} {total_s+total_g:>6}")


# ─────────────────────────────────────────────────────────────
# Balance mode
# ─────────────────────────────────────────────────────────────

def get_clean_counts() -> dict[str, int]:
    """Return per-category count of non-flagged examples (seeds + generated)."""
    counts: dict[str, int] = {cat: 0 for cat in CATEGORIES}
    for path in [SEED_FILE, OUTPUT_FILE]:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ex = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ex.get("flagged"):
                continue
            cat = ex.get("primary")
            if cat in counts:
                counts[cat] += 1
    return counts


def run_balance(target: int, batch_cap: int) -> None:
    """
    Generate only for categories below `target` clean examples.
    For each under-represented category, generate min(gap, batch_cap) new examples.
    """
    counts = get_clean_counts()

    print(f"\n[balance] Target: {target} clean examples per category")
    print(f"{'Category':<22} {'Have':>6} {'Need':>6} {'Generate':>10}")
    print("-" * 48)

    work: list[tuple[str, int]] = []
    for cat in CATEGORIES:
        have = counts.get(cat, 0)
        need = max(0, target - have)
        to_gen = min(need, batch_cap) if need > 0 else 0
        marker = " <--" if to_gen > 0 else ""
        print(f"  {cat:<20} {have:>6} {need:>6} {to_gen:>10}{marker}")
        if to_gen > 0:
            work.append((cat, to_gen))
    print("-" * 48)

    if not work:
        print("\n[balance] All categories already at or above target. Nothing to do.")
        return

    total_to_gen = sum(n for _, n in work)
    print(f"\n[balance] Generating {total_to_gen} examples across {len(work)} categories...")

    for cat, n in work:
        run_batch([cat], n)

    print("\n[balance] Auto-relabeling any flagged rows from this run...")
    fix_flagged(mode="relabel")


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate router training data")
    parser.add_argument("--per-category", type=int, default=20,
                        help="Examples to generate per category (default: 20)")
    parser.add_argument("--categories", nargs="+", choices=CATEGORIES,
                        help="Limit to specific categories (default: all)")
    parser.add_argument("--balance", metavar="TARGET", type=int, default=None,
                        help="Auto-generate only for categories below TARGET clean examples")
    parser.add_argument("--batch-cap", type=int, default=40,
                        help="Max examples to generate per category per balance run (default: 40)")
    parser.add_argument("--review-flagged", action="store_true",
                        help="Show flagged examples and exit")
    parser.add_argument("--fix-flagged", choices=["relabel", "drop", "accept"], default=None,
                        help="Fix flagged rows: relabel=use router label, drop=remove mismatches, accept=clear all flags")
    parser.add_argument("--stats", action="store_true",
                        help="Show dataset statistics and exit")
    args = parser.parse_args()

    if args.review_flagged:
        review_flagged()
        return

    if args.fix_flagged:
        fix_flagged(mode=args.fix_flagged)
        return

    if args.stats:
        print_stats()
        return

    if args.balance is not None:
        run_balance(target=args.balance, batch_cap=args.batch_cap)
        return

    categories = args.categories or CATEGORIES
    run_batch(categories, args.per_category)


if __name__ == "__main__":
    main()
