"""Nearby timeline cards group only when time and summary embeddings both agree."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.timeline_groups import GROUP_GAP_SECONDS, assign_similar_session_groups


def _session(summary_id: str, start: float, end: float) -> dict:
    return {
        "summary_id": summary_id,
        "window_start": start,
        "window_end": end,
    }


class TimelineGroupTests(unittest.TestCase):
    def test_close_similar_neighbors_share_a_group(self):
        sessions = [
            _session("newer", 120, 180),
            _session("older", 0, 60),
        ]
        assign_similar_session_groups(sessions, [[1.0, 0.0], [1.0, 0.0]])
        self.assertEqual(sessions[0]["group_id"], sessions[1]["group_id"])
        self.assertEqual(sessions[0]["group_id"], "older")

    def test_a_long_gap_keeps_similar_summaries_apart(self):
        sessions = [
            _session("early", 0, 60),
            _session("later", 60 + GROUP_GAP_SECONDS + 1, 60 + GROUP_GAP_SECONDS + 120),
        ]
        assign_similar_session_groups(sessions, [[1.0, 0.0], [1.0, 0.0]])
        self.assertIsNone(sessions[0]["group_id"])
        self.assertIsNone(sessions[1]["group_id"])

    def test_a_weak_middle_does_not_bridge_the_sessions_around_it(self):
        sessions = [
            _session("leetcode", 0, 60),
            _session("notification", 120, 150),
            _session("leetcode-again", 180, 240),
        ]
        assign_similar_session_groups(
            sessions,
            [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]],
        )
        self.assertIsNone(sessions[0]["group_id"])
        self.assertIsNone(sessions[1]["group_id"])
        self.assertIsNone(sessions[2]["group_id"])

    def test_each_close_hop_extends_the_same_group(self):
        sessions = [
            _session("a", 0, 60),
            _session("b", 90, 150),
            _session("c", 180, 240),
        ]
        assign_similar_session_groups(
            sessions,
            [[1.0, 0.0], [0.99, 0.1], [0.98, 0.15]],
        )
        self.assertEqual(sessions[0]["group_id"], "a")
        self.assertEqual(sessions[1]["group_id"], "a")
        self.assertEqual(sessions[2]["group_id"], "a")

    def test_a_missing_embedding_stays_alone(self):
        sessions = [
            _session("a", 0, 60),
            _session("b", 90, 150),
        ]
        assign_similar_session_groups(sessions, [[1.0, 0.0], None])
        self.assertIsNone(sessions[0]["group_id"])
        self.assertIsNone(sessions[1]["group_id"])
