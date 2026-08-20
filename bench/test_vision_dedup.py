"""
Benchmark: Vision Deduplication Efficiency
Measures how much pHash clustering reduces vision inference calls.
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Change to parent directory so relative imports work
import os

os.chdir(Path(__file__).parent.parent)

import imagehash
from PIL import Image

PHASH_THRESHOLD = 2  # same as production
SCREENSHOT_DIR = Path(__file__).parent.parent / "core" / "data" / "screenshots"


def test_vision_dedup():
    """
    Compute pHash for all screenshots and group by similarity.
    Measures compression ratio achieved by deduplication.
    """
    screenshots = list(SCREENSHOT_DIR.glob("*.jpg"))

    if len(screenshots) == 0:
        print("\nNo screenshots found. Run screen_capture.py first to collect data.")
        return

    # Remove already-processed screenshots from test
    screenshots = [s for s in screenshots if "_processed" not in s.stem]

    if len(screenshots) == 0:
        print(
            "\nAll screenshots already processed. Collect fresh screenshots for benchmarking."
        )
        return

    total = len(screenshots)

    print(f"\n{'=' * 60}")
    print(f"Vision Deduplication Efficiency Test")
    print(f"{'=' * 60}")
    print(f"Testing {total} screenshots...\n")

    # Compute hashes
    hashes = {}
    for path in screenshots:
        try:
            hashes[path.stem] = imagehash.phash(Image.open(path))
        except Exception as e:
            print(f"  ⚠️  Failed to hash {path.name}: {e}")

    valid = [p for p in screenshots if p.stem in hashes]

    # Union-Find clustering (same as production)
    parent = {p.stem: p.stem for p in valid}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str):
        parent[find(x)] = find(y)

    # Group by similarity
    for i, pa in enumerate(valid):
        for pb in valid[i + 1 :]:
            if (hashes[pa.stem] - hashes[pb.stem]) <= PHASH_THRESHOLD:
                union(pa.stem, pb.stem)

    # Count unique groups
    groups = {}
    for p in valid:
        root = find(p.stem)
        groups.setdefault(root, []).append(p)

    unique_groups = len(groups)
    reduction_pct = ((total - unique_groups) / total) * 100

    # Show some group stats
    group_sizes = sorted([len(g) for g in groups.values()], reverse=True)

    print(f"Results:")
    print(f"  Total screenshots:        {total}")
    print(f"  Unique visual groups:     {unique_groups}")
    print(f"  Duplicate screenshots:    {total - unique_groups}")
    print(f"  Largest group size:       {group_sizes[0] if group_sizes else 0}")
    print(f"\n{'=' * 60}")
    print(f"  ** Vision Reduction:      {reduction_pct:.1f}%")
    print(f"{'=' * 60}\n")

    print(f"Resume Bullet:")
    print(f'   "Reduced vision processing by {reduction_pct:.0f}% through perceptual')
    print(f'    hashing and Union-Find clustering of duplicate screenshots"')
    print()


if __name__ == "__main__":
    test_vision_dedup()
