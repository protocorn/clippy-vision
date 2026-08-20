"""Make the project root and core/ importable when running `python bench/runner.py`.

The project deliberately has no __init__.py files and relies on both the repo root
and core/ being on sys.path (see distil.py mixing `from agent.memory ...` and
`from storage ...`). We reproduce that here so the bench can import the real gateway.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "core")):
    if p not in sys.path:
        sys.path.insert(0, p)
