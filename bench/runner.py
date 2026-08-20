"""Memory bake-off runner.

Feeds one frozen fact stream into every strategy, runs the eval queries, grades them,
and prints a single scoreboard. Run from the project root:

    python bench/runner.py
"""

import time

import _paths  # noqa: F401
import dataset
from grader import Grader
from strategies import all_strategies

K = 5


def run():
    grader = Grader()
    facts = dataset.stream()
    results = []

    for strat in all_strategies():
        t0 = time.time()
        print(f"\n=== {strat.name}: ingesting {len(facts)} facts ===")
        for t, fid, text, canon in facts:
            strat.add(text, t)

        correct = 0
        sup_total = 0
        sup_ok = 0
        for q in dataset.QUERIES:
            retrieved = strat.query(q["q"], k=K)
            c, sok = grader.grade(q, retrieved)
            correct += 1 if c else 0
            if q.get("forbid"):
                sup_total += 1
                sup_ok += 1 if sok else 0

        dump = strat.dump()
        clippy_items = sum(1 for d in dump if "clippy" in d.lower())
        results.append(
            {
                "name": strat.name,
                "hit": correct / len(dataset.QUERIES),
                "sup": (sup_ok / sup_total) if sup_total else 1.0,
                "size": len(dump),
                "bloat": len(dump) / dataset.IDEAL_SIZE,
                "clippy": clippy_items,
                "llm": strat.llm_calls,
                "emb": strat.embed_calls,
                "secs": time.time() - t0,
            }
        )

    _print_table(results)
    print(
        f"\nIdeal final size = {dataset.IDEAL_SIZE} distinct facts | "
        f"ideal Clippy_Vision items = {dataset.IDEAL_CLIPPY_ITEMS} | "
        f"judge calls = {grader.judge_calls} (not charged to strategies)"
    )
    print(
        "\nLegend: hit=answer accuracy (higher better) | suprsd=supersession correct "
        "(higher better) | size/bloat=final memory entries (lower better, but not below "
        "ideal) | clippy=fragmentation (closer to 1 better) | llm/emb=cost"
    )


def _print_table(results):
    header = f"{'strategy':<24}{'hit':>6}{'suprsd':>8}{'size':>6}{'bloat':>7}{'clippy':>8}{'llm':>6}{'emb':>6}{'secs':>7}"
    print("\n" + header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r['name']:<24}{r['hit']:>6.0%}{r['sup']:>8.0%}{r['size']:>6}"
            f"{r['bloat']:>7.2f}{r['clippy']:>8}{r['llm']:>6}{r['emb']:>6}{r['secs']:>7.1f}"
        )


if __name__ == "__main__":
    run()
