import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path

from agent.helpers.time_resolver import resolve_temporal_range

DB_PATH = Path("core/data/events.db")
conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
conn.commit()


def san(kw):
    c = re.sub(r"[^\w]", "", kw)
    return f'"{c}"' if c else ""


probes = [
    (
        "describe photo AI image",
        ["describe", "photo", "generating", "image"],
        "describe the photo I used for generating the image by AI in last 20 days",
    ),
    (
        "clippy vision errors",
        ["errors", "clippy", "vision"],
        "what errors did I get in clippy vision in last 20 days",
    ),
    (
        "router classifier",
        ["router", "classifier"],
        "what was I working on related to the router classifier in last 20 days",
    ),
    (
        "graduation screen",
        ["graduation"],
        "what was I seeing on the screen related to graduation in last 20 days",
    ),
]

NOISE = (
    "(e.event_type NOT IN ('typing_burst','deviation','context_change') "
    "OR e.vision_ocr_text IS NOT NULL)"
)

# Test with fts alias (as used in specific_recall.py)
sql_alias = f"""
    SELECT e.timestamp, e.summary, e.current_window_title, fts.rank
    FROM events_fts fts JOIN events e ON fts.rowid = e.rowid
    WHERE fts MATCH ?
      AND e.timestamp >= ? AND e.timestamp < ?
      AND e.interesting = 1 AND {NOISE}
    ORDER BY fts.rank LIMIT 5
"""

# Test without alias
sql_noalias = f"""
    SELECT e.timestamp, e.summary, e.current_window_title, events_fts.rank
    FROM events_fts JOIN events e ON events_fts.rowid = e.rowid
    WHERE events_fts MATCH ?
      AND e.timestamp >= ? AND e.timestamp < ?
      AND e.interesting = 1 AND {NOISE}
    ORDER BY events_fts.rank LIMIT 5
"""

for label, keywords, query_str in probes:
    tr = resolve_temporal_range(query_str)
    fts_q = " OR ".join(x for x in (san(k) for k in keywords) if x)
    print(
        f"--- {label} ---  fts_q={fts_q}  range={time.strftime('%m-%d', time.localtime(tr.start_ts))} to {time.strftime('%m-%d', time.localtime(tr.end_ts))}"
    )

    for name, sql in [("alias", sql_alias), ("no-alias", sql_noalias)]:
        try:
            rows = conn.execute(sql, (fts_q, tr.start_ts, tr.end_ts)).fetchall()
            if rows:
                print(
                    f"  [{name}] {len(rows)} rows, top rank={rows[0][3]:.3f}, title={rows[0][2][:50]}"
                )
            else:
                print(f"  [{name}] 0 rows")
        except Exception as ex:
            print(f"  [{name}] ERROR: {ex}")
    print()
