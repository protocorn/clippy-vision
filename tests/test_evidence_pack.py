from __future__ import annotations

import unittest

from agent.evidence_pack import collapse_sessions, is_noise_session


def _s(task: str, *, events: int = 1, start: float = 1000.0, end: float = 1001.0, summary: str = ""):
    return {
        "active_task": task,
        "event_count": events,
        "window_start": start,
        "window_end": end,
        "window_start_local": "t-start",
        "window_end_local": "t-end",
        "summary": summary or f"Summary for {task}",
    }


class CollapseSessionsTests(unittest.TestCase):
    def test_drops_viewing_noise(self):
        rows = [
            _s("viewing Cursor Agents interface", events=1),
            _s("viewing Cursor Agents interface", events=1, start=1100),
            _s("collect_github_traffic.ps1", events=3, start=1200, summary="Ran traffic script"),
        ]
        themes, meta = collapse_sessions(rows, max_themes=14)
        self.assertEqual(meta["dropped_noise"], 2)
        self.assertEqual(len(themes), 1)
        self.assertIn("collect_github_traffic.ps1", themes[0]["active_task"])

    def test_merges_paraphrase_tasks(self):
        rows = [
            _s("Proving Clippy Vision's worth with a concrete example", events=7, start=2000),
            _s("prove clippy vision's worth", events=1, start=2010),
            _s("Provide a concrete example of Clippy Vision's utility", events=2, start=2020),
            _s("collect_github_traffic.ps1", events=1, start=1000),
            _s("collect_github_traffic.ps1", events=1, start=1010),
        ]
        themes, meta = collapse_sessions(rows, max_themes=14)
        self.assertEqual(meta["raw_sessions"], 5)
        self.assertEqual(len(themes), 2)
        by_task = {t["active_task"]: t for t in themes}
        # Same script filename → one theme with merged_from=2
        traffic = [t for t in themes if "collect_github_traffic" in (t["active_task"] or "")][0]
        self.assertEqual(traffic["merged_from"], 2)
        prove = [t for t in themes if t is not traffic][0]
        self.assertGreaterEqual(prove["merged_from"], 3)
        self.assertGreaterEqual(prove["event_count"], 10)

    def test_merges_github_pat_variants(self):
        rows = [
            _s("GitHub personal access token setup", events=5, start=1),
            _s("create GitHub Personal Access Token", events=1, start=2),
            _s("generate_github_personal_access_token", events=1, start=3),
        ]
        themes, _ = collapse_sessions(rows, max_themes=14)
        self.assertEqual(len(themes), 1)
        self.assertEqual(themes[0]["merged_from"], 3)

    def test_caps_to_max_themes(self):
        rows = [
            _s(f"zephyr{i} orchid{i} lantern task", events=i + 1, start=float(i))
            for i in range(20)
        ]
        themes, meta = collapse_sessions(rows, max_themes=14)
        self.assertEqual(len(themes), 14)
        self.assertEqual(meta["capped_away"], 6)

    def test_is_noise(self):
        self.assertTrue(is_noise_session(_s("viewing Electron application interface")))
        self.assertFalse(is_noise_session(_s("collect_github_traffic.ps1")))


if __name__ == "__main__":
    unittest.main()
