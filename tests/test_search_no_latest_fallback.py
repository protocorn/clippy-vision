"""An empty session search stays empty. It does not substitute the newest rows."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.retrieval import search_sessions
from core.storage import conn, store_summary


class SearchSessionsNoLatestFallbackTests(unittest.TestCase):
    def setUp(self):
        self.prefix = f"search-fallback-{uuid4().hex}"

    def tearDown(self):
        conn.execute("DELETE FROM sessions WHERE summary_id LIKE ?", (f"{self.prefix}%",))
        conn.commit()

    def test_empty_window_does_not_return_newest_sessions(self):
        stamp = time.time()
        store_summary(
            {
                "session_id": f"{self.prefix}-session",
                "summary_id": f"{self.prefix}-summary",
                "created_at": stamp,
                "window_start": stamp,
                "window_end": stamp + 30,
                "summary": "Edited the Launchway one pager in Cursor.",
                "active_task": "one pager",
                "entities": ["Cursor", "Launchway"],
                "event_count": 2,
            },
            vision_enriched=True,
        )
        sql = (
            "SELECT summary, active_task, entities, "
            "datetime(window_start,'unixepoch','localtime') as time "
            "FROM sessions WHERE window_start > 9999999999"
        )
        with patch("agent.retrieval._generate_sql", return_value=sql), patch(
            "agent.retrieval._semantic_sessions", return_value=([], 0)
        ):
            result = search_sessions("scooter locks from june")

        self.assertIn("no matching session summaries", result)
        self.assertNotIn("most recent", result.casefold())
        self.assertNotIn("Launchway", result)


if __name__ == "__main__":
    unittest.main()
