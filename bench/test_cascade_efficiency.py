"""
Benchmark: Classification Cascade Efficiency
Measures how much the 3-tier cascade reduces LLM inference calls.
"""

import json
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Change to parent directory so relative imports work
import os

os.chdir(Path(__file__).parent.parent)

from classifier.tier_one_classifier import tier1_score
from classifier.tier_zero_classifier import tier_zero_classifier
from core.storage import conn


def test_cascade_efficiency(limit=1000):
    """
    Run historical events through the cascade and measure filtering at each tier.
    """
    # Get sample events from database
    rows = conn.execute(f"""
        SELECT event_id, event_type, process_name, current_window_title,
               payload, timestamp, summary, previous_process_name, previous_window_title
        FROM events
        ORDER BY timestamp DESC
        LIMIT {limit}
    """).fetchall()

    if len(rows) == 0:
        print("No events in database. Run screen_capture.py first to collect data.")
        return

    total = len(rows)
    tier0_filtered = 0
    tier1_filtered = 0
    tier2_required = 0

    print(f"\n{'=' * 60}")
    print(f"Classification Cascade Efficiency Test")
    print(f"{'=' * 60}")
    print(f"Testing {total} events...\n")

    for row in rows:
        event = {
            "event_id": row[0],
            "event_type": row[1],
            "process_name": row[2],
            "window_context": {"current_window_title": row[3], "process_name": row[2]},
            "previous_window_context": {
                "process_name": row[7],
                "current_window_title": row[8],
            }
            if row[7]
            else None,
            "payload": row[4],  # Already a JSON string from DB
            "timestamp": row[5],
            "summary": row[6],
        }

        # Tier 0: Rule-based
        tier0_result = tier_zero_classifier(event)
        if tier0_result and tier0_result["verdict"] != "uncertain":
            tier0_filtered += 1
            continue

        # Tier 1: Feature-based
        tier1_result = tier1_score(event, conn)
        if tier1_result and tier1_result["verdict"] != "uncertain":
            tier1_filtered += 1
            continue

        # Tier 2: Would require LLM
        tier2_required += 1

    # Calculate percentages
    tier0_pct = (tier0_filtered / total) * 100
    tier1_pct = (tier1_filtered / total) * 100
    tier2_pct = (tier2_required / total) * 100
    llm_reduction = 100 - tier2_pct

    print(f"Results:")
    print(
        f"  Tier 0 (Rules):    {tier0_filtered:4d} / {total} ({tier0_pct:5.1f}%) filtered"
    )
    print(
        f"  Tier 1 (Features): {tier1_filtered:4d} / {total} ({tier1_pct:5.1f}%) filtered"
    )
    print(
        f"  Tier 2 (LLM):      {tier2_required:4d} / {total} ({tier2_pct:5.1f}%) required"
    )
    print(f"\n{'=' * 60}")
    print(f"  ** LLM Reduction:   {llm_reduction:.1f}%")
    print(f"{'=' * 60}\n")

    print(f"Resume Bullet:")
    print(f'   "Reduced LLM inference calls by {llm_reduction:.0f}% through a 3-tier')
    print(f'    classification cascade (rule-based → feature-based → LLM)"')
    print()


if __name__ == "__main__":
    test_cascade_efficiency(limit=1000)
