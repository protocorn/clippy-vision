"""
Router evaluation harness.

Compares three routing strategies on an independent hand-labeled golden set
(testing/router_eval/golden_set.jsonl — written separately from the training data):

  1. minilm   - the fine-tuned MiniLM classifier (agent/router.py)
  2. baseline - keyword/regex rules implementing the labelling policy (0 ML)
  3. llm      - qwen3:8b with the routing system prompt (optional, slow: --with-llm)

Reports:
  - overall + per-class precision/recall/F1, confusion matrix
  - accuracy by tag (easy / paraphrase / typo / boundary / ood / bare)
  - prefetch-gate analysis: at the thresholds in agent/router.py, what fraction of
    queries fire the prefetch path, and what is the precision among those fired?
  - confidence calibration: accuracy in confidence buckets
  - latency per query

Usage (from repo root):
    python testing/router_eval/run_eval.py
    python testing/router_eval/run_eval.py --with-llm
"""

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

GOLDEN = Path(__file__).parent / "golden_set.jsonl"
RESULTS = Path(__file__).parent / "results"

CATEGORIES = [
    "time_anchored", "topic_search", "aggregation", "specific_recall",
    "memory_query", "casual", "follow_up_inherit",
]

# Kept in sync with agent/router.py
PREFETCH_THRESHOLDS = {
    "aggregation":     0.50,
    "memory_query":    0.50,
    "time_anchored":   0.55,
    "specific_recall": 0.50,
    "topic_search":    0.30,
}

# Action-level grouping: misrouting *within* a group is cheap (same data source),
# across groups is expensive (wrong prefetch pollutes context / wastes latency).
ACTION_GROUP = {
    "time_anchored":   "activity_log",
    "topic_search":    "activity_log",
    "aggregation":     "activity_log",
    "specific_recall": "activity_log_fine",
    "memory_query":    "memory",
    "casual":          "none",
    "follow_up_inherit": "none",
}


def load_golden() -> list[dict]:
    rows = []
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# ─────────────────────────────────────────────────────────────
# Router wrappers — each returns (label, confidence, latency_ms)
# ─────────────────────────────────────────────────────────────

def make_minilm_router():
    from agent.router import classify_query

    def run(text: str):
        t0 = time.perf_counter()
        decision, conf = classify_query(text)
        ms = (time.perf_counter() - t0) * 1000
        return decision.primary, conf, ms

    return run


def make_baseline_router():
    from baseline_router import baseline_classify

    def run(text: str):
        t0 = time.perf_counter()
        label = baseline_classify(text)
        ms = (time.perf_counter() - t0) * 1000
        return label, 1.0, ms

    return run


def make_llm_router():
    """qwen3:8b router using the system prompt archived in agent/router.py."""
    import re as _re

    from core.llm_gateway import Priority, gateway

    src = (ROOT / "agent" / "router.py").read_text(encoding="utf-8")
    m = _re.search(r'SYSTEM_PROMPT = ```\n(.*?)```', src, _re.DOTALL)
    system_prompt = m.group(1)

    schema = {
        "type": "object",
        "properties": {
            "primary":   {"type": "string", "enum": CATEGORIES},
            "secondary": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}},
            "temporal_hint": {"type": "string"},
        },
        "required": ["primary", "secondary"],
    }

    def run(text: str):
        t0 = time.perf_counter()
        try:
            body = gateway.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                model="qwen3:8b",
                format=schema,
                think=False,
                options={"temperature": 0},
                priority=Priority.INTERACTIVE,
                timeout=120,
            )
            content = body["message"]["content"]
            parsed = json.loads(content) if isinstance(content, str) else content
            label = parsed.get("primary", "casual")
            if label not in CATEGORIES:
                label = "casual"
        except Exception as e:
            print(f"    [llm error] {e}")
            label = "ERROR"
        ms = (time.perf_counter() - t0) * 1000
        return label, 1.0, ms

    return run


# ─────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────

def per_class_report(golds, preds) -> str:
    lines = [f"{'category':<20} {'prec':>6} {'rec':>6} {'f1':>6} {'support':>8}"]
    f1s = []
    for cat in CATEGORIES:
        tp = sum(1 for g, p in zip(golds, preds) if g == cat and p == cat)
        fp = sum(1 for g, p in zip(golds, preds) if g != cat and p == cat)
        fn = sum(1 for g, p in zip(golds, preds) if g == cat and p != cat)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        support = sum(1 for g in golds if g == cat)
        f1s.append(f1)
        lines.append(f"{cat:<20} {prec:>6.2f} {rec:>6.2f} {f1:>6.2f} {support:>8}")
    lines.append(f"{'macro-F1':<20} {'':>6} {'':>6} {sum(f1s)/len(f1s):>6.2f}")
    return "\n".join(lines)


def confusion_matrix(golds, preds) -> str:
    short = {c: c[:9] for c in CATEGORIES}
    header = f"{'gold \\ pred':<18}" + "".join(f"{short[c]:>10}" for c in CATEGORIES)
    lines = [header]
    for g in CATEGORIES:
        row = [f"{short[g]:<18}"]
        for p in CATEGORIES:
            n = sum(1 for gg, pp in zip(golds, preds) if gg == g and pp == p)
            row.append(f"{n if n else '.':>10}")
        lines.append("".join(row))
    return "\n".join(lines)


def evaluate(name: str, rows: list[dict], router, warmup: bool = True) -> dict:
    print(f"\n[{name}] evaluating {len(rows)} queries...")
    if warmup:
        router("warmup query")

    records = []
    for i, row in enumerate(rows):
        label, conf, ms = router(row["text"])
        records.append({
            "text": row["text"], "gold": row["primary"], "pred": label,
            "conf": conf, "ms": ms, "tag": row.get("tag", "easy"),
        })
        if (i + 1) % 40 == 0:
            print(f"  ...{i + 1}/{len(rows)}")

    golds = [r["gold"] for r in records]
    preds = [r["pred"] for r in records]
    acc = sum(g == p for g, p in zip(golds, preds)) / len(records)

    group_acc = sum(
        ACTION_GROUP.get(g) == ACTION_GROUP.get(p) for g, p in zip(golds, preds)
    ) / len(records)

    tag_acc = {}
    for tag in sorted({r["tag"] for r in records}):
        sub = [r for r in records if r["tag"] == tag]
        tag_acc[tag] = (sum(r["gold"] == r["pred"] for r in sub) / len(sub), len(sub))

    lat = sorted(r["ms"] for r in records)
    latency = {
        "mean": sum(lat) / len(lat),
        "p50": lat[len(lat) // 2],
        "p95": lat[int(len(lat) * 0.95)],
    }

    return {
        "name": name, "records": records, "accuracy": acc, "group_accuracy": group_acc,
        "tag_acc": tag_acc, "latency": latency,
        "report": per_class_report(golds, preds),
        "confusion": confusion_matrix(golds, preds),
    }


def prefetch_analysis(records: list[dict]) -> str:
    """At the current per-category prefetch thresholds, how often does the gate
    open, and how precise is the routing when it does?"""
    fired = [r for r in records if PREFETCH_THRESHOLDS.get(r["pred"]) is not None
             and r["conf"] >= PREFETCH_THRESHOLDS[r["pred"]]]
    held = [r for r in records if r not in fired]

    lines = []
    n = len(records)
    lines.append(f"Prefetch gate fired: {len(fired)}/{n} ({len(fired)/n:.0%} of queries)")
    if fired:
        correct = sum(1 for r in fired if r["gold"] == r["pred"])
        group_ok = sum(1 for r in fired if ACTION_GROUP[r["gold"]] == ACTION_GROUP[r["pred"]])
        lines.append(f"  precision when fired (exact label):  {correct/len(fired):.1%}")
        lines.append(f"  precision when fired (action group): {group_ok/len(fired):.1%}")
    # among held-back queries, how many were prefetchable categories we missed?
    missed = [r for r in held if r["gold"] in PREFETCH_THRESHOLDS]
    lines.append(f"  prefetchable queries NOT fired (missed speedups): {len(missed)}/{n}")

    lines.append("\n  Per-category gate behaviour:")
    for cat, thr in PREFETCH_THRESHOLDS.items():
        cat_fired = [r for r in fired if r["pred"] == cat]
        if not cat_fired:
            lines.append(f"    {cat:<18} thr={thr:.2f}  fired=0")
            continue
        ok = sum(1 for r in cat_fired if r["gold"] == cat)
        lines.append(f"    {cat:<18} thr={thr:.2f}  fired={len(cat_fired):>3}  precision={ok/len(cat_fired):.1%}")
    return "\n".join(lines)


def calibration(records: list[dict]) -> str:
    buckets = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 0.95), (0.95, 1.01)]
    lines = [f"{'confidence':<14} {'n':>5} {'accuracy':>9}"]
    for lo, hi in buckets:
        sub = [r for r in records if lo <= r["conf"] < hi]
        if not sub:
            lines.append(f"{f'[{lo:.2f},{hi:.2f})':<14} {0:>5} {'—':>9}")
            continue
        acc = sum(r["gold"] == r["pred"] for r in sub) / len(sub)
        lines.append(f"{f'[{lo:.2f},{hi:.2f})':<14} {len(sub):>5} {acc:>9.1%}")
    return "\n".join(lines)


def threshold_sweep(records: list[dict]) -> str:
    """Single global confidence threshold sweep: coverage vs precision."""
    lines = [f"{'threshold':>9} {'coverage':>9} {'precision':>10} {'group-prec':>11}"]
    for thr in [0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
        sub = [r for r in records if r["conf"] >= thr]
        if not sub:
            lines.append(f"{thr:>9.2f} {'0%':>9} {'—':>10} {'—':>11}")
            continue
        cov = len(sub) / len(records)
        prec = sum(r["gold"] == r["pred"] for r in sub) / len(sub)
        gprec = sum(ACTION_GROUP[r["gold"]] == ACTION_GROUP[r["pred"]] for r in sub) / len(sub)
        lines.append(f"{thr:>9.2f} {cov:>9.0%} {prec:>10.1%} {gprec:>11.1%}")
    return "\n".join(lines)


def dump_errors(records: list[dict], path: Path):
    errors = [r for r in records if r["gold"] != r["pred"]]
    with path.open("w", encoding="utf-8") as f:
        for r in sorted(errors, key=lambda r: (r["gold"], -r["conf"])):
            f.write(json.dumps(r) + "\n")
    return len(errors)


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-llm", action="store_true", help="also run qwen3:8b router (slow)")
    args = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    rows = load_golden()
    dist = Counter(r["primary"] for r in rows)
    print(f"Golden set: {len(rows)} queries")
    for cat in CATEGORIES:
        print(f"  {cat:<20} {dist.get(cat, 0)}")

    results = []
    results.append(evaluate("baseline-regex", rows, make_baseline_router()))
    results.append(evaluate("minilm", rows, make_minilm_router()))
    if args.with_llm:
        results.append(evaluate("qwen3-8b-llm", rows, make_llm_router()))

    out_lines = []
    for res in results:
        out_lines.append("=" * 70)
        out_lines.append(f"ROUTER: {res['name']}")
        out_lines.append("=" * 70)
        out_lines.append(f"Overall accuracy:      {res['accuracy']:.1%}")
        out_lines.append(f"Action-group accuracy: {res['group_accuracy']:.1%}   "
                         "(pred maps to same data source as gold)")
        out_lines.append(f"Latency ms: mean={res['latency']['mean']:.1f}  "
                         f"p50={res['latency']['p50']:.1f}  p95={res['latency']['p95']:.1f}")
        out_lines.append("\nAccuracy by tag:")
        for tag, (acc, n) in res["tag_acc"].items():
            out_lines.append(f"  {tag:<12} {acc:>6.1%}  (n={n})")
        out_lines.append("\n" + res["report"])
        out_lines.append("\nConfusion matrix:")
        out_lines.append(res["confusion"])

        if res["name"] == "minilm":
            out_lines.append("\nPrefetch gate analysis (current thresholds in agent/router.py):")
            out_lines.append(prefetch_analysis(res["records"]))
            out_lines.append("\nConfidence calibration:")
            out_lines.append(calibration(res["records"]))
            out_lines.append("\nGlobal threshold sweep (route if conf >= thr, else fall back to ReAct):")
            out_lines.append(threshold_sweep(res["records"]))

        n_err = dump_errors(res["records"], RESULTS / f"errors_{res['name']}.jsonl")
        out_lines.append(f"\nErrors written: {n_err} -> results/errors_{res['name']}.jsonl")
        out_lines.append("")

    report = "\n".join(out_lines)
    print("\n" + report)
    (RESULTS / "report.txt").write_text(report, encoding="utf-8")

    # Also save raw records for later analysis
    for res in results:
        with (RESULTS / f"records_{res['name']}.jsonl").open("w", encoding="utf-8") as f:
            for r in res["records"]:
                f.write(json.dumps(r) + "\n")
    print(f"\nFull report saved to {RESULTS / 'report.txt'}")


if __name__ == "__main__":
    main()
