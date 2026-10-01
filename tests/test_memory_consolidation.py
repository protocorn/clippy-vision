"""Candidates stay out of recall until a later day, or the person states them."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-memory-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.memory import recall_memory
from core.distil import _cosine_similarity, distil, ingest_conversation
from core.memory_consolidation import consolidate, local_day, maybe_consolidate, record_candidate
from core.memory_freshness import fact_freshness
from core.storage import clear_data, conn

VEC = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]
OTHER = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0]


def _stamp(year: int, month: int, day: int, hour: int = 12) -> float:
    return time.mktime((year, month, day, hour, 0, 0, 0, 0, -1))


def _vec(slot: int) -> list[float]:
    vector = [0.0] * 16
    vector[slot] = 1.0
    return vector


class MemoryConsolidationTests(unittest.TestCase):
    def setUp(self):
        conn.execute("DELETE FROM memory_candidates")
        conn.commit()

    def test_same_sitting_collapses_and_is_not_a_claim(self):
        first = record_candidate(
            "User spent the sitting on a coast trip outline",
            _vec(8),
            kind="observed",
            about_user=True,
            session_ids=["sit-a"],
            seen_at=_stamp(2026, 12, 10, 9),
        )
        second = record_candidate(
            "User spent the sitting on a coast trip outline",
            _vec(8),
            kind="observed",
            about_user=True,
            session_ids=["sit-b"],
            seen_at=_stamp(2026, 12, 10, 11),
        )
        self.assertEqual(first, second)
        row = conn.execute(
            "SELECT session_ids, status FROM memory_candidates WHERE candidate_id = ?",
            (first,),
        ).fetchone()
        self.assertEqual(json.loads(row[0]), ["sit-a", "sit-b"])
        self.assertEqual(row[1], "open")
        stats = consolidate(classify=lambda *_: "unrelated", now=_stamp(2026, 12, 10, 18))
        self.assertEqual(stats["promoted"], 0)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM memory_facts WHERE text = ?",
            ("User spent the sitting on a coast trip outline",),
        ).fetchone())

    def test_screen_content_is_not_stored(self):
        stored = record_candidate(
            "An article about someone else's medical history",
            _vec(9),
            kind="observed",
            about_user=False,
            session_ids=["page-1"],
            seen_at=_stamp(2026, 12, 10),
        )
        self.assertIsNone(stored)
        count = conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
        self.assertEqual(count, 0)

    def test_later_day_promotes_and_recall_hides_the_candidate(self):
        text = "User outlines a coast trip on separate days"
        record_candidate(
            text, _vec(9), kind="observed", about_user=True,
            session_ids=["dec10"], seen_at=_stamp(2026, 12, 10),
        )
        record_candidate(
            text, _vec(9), kind="inferred", about_user=True,
            session_ids=["dec15"], seen_at=_stamp(2026, 12, 15),
        )
        self.assertNotIn(text, recall_memory())
        promoted_at = _stamp(2026, 12, 15, 18)
        with patch("core.distil._label_for_fact", return_value=("coast_trip", "a repeated trip")):
            stats = consolidate(classify=lambda *_: "unrelated", now=promoted_at)
        self.assertEqual(stats["promoted"], 1)
        row = conn.execute(
            """SELECT support_session_ids, scope, kind, source, valid_from, last_confirmed
               FROM memory_facts WHERE text = ? AND valid_to IS NULL""",
            (text,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row[0]), ["dec10", "dec15"])
        self.assertEqual(row[1], "pattern")
        self.assertEqual(row[2], "observed")
        self.assertEqual(row[3], "distiller")
        self.assertEqual(row[4], promoted_at)
        self.assertEqual(row[5], promoted_at)
        self.assertIn("coast_trip", recall_memory())
        open_rows = conn.execute(
            "SELECT COUNT(*) FROM memory_candidates WHERE status = 'open'"
        ).fetchone()[0]
        self.assertEqual(open_rows, 0)

    def test_replay_of_the_same_session_does_not_add_a_day(self):
        text = "User outlined a coast trip once"
        first = record_candidate(
            text, _vec(10), kind="observed", about_user=True,
            session_ids=["only-once"], seen_at=_stamp(2026, 12, 10),
        )
        replay = record_candidate(
            text, _vec(10), kind="observed", about_user=True,
            session_ids=["only-once"], seen_at=_stamp(2026, 12, 15),
        )
        self.assertEqual(first, replay)
        self.assertEqual(local_day(_stamp(2026, 12, 10)), conn.execute(
            "SELECT local_day FROM memory_candidates WHERE candidate_id = ?",
            (first,),
        ).fetchone()[0])
        stats = consolidate(classify=lambda *_: "unrelated", now=_stamp(2026, 12, 15, 18))
        self.assertEqual(stats["promoted"], 0)

    def test_known_claim_is_confirmed_without_a_new_fact(self):
        from core.distil import _create_cluster

        text = "User takes the same morning train"
        _create_cluster(
            text, _vec(11), source="distiller", label="train", description="commute",
            scope="pattern", kind="observed", session_ids=["old-session"],
            last_confirmed=_stamp(2026, 12, 1), valid_from=_stamp(2026, 12, 1),
        )
        record_candidate(
            text, _vec(11), kind="observed", about_user=True,
            session_ids=["new-session"], seen_at=_stamp(2026, 12, 15),
        )
        confirmed_at = _stamp(2026, 12, 15, 18)
        stats = consolidate(
            classify=lambda observation, claim: "duplicate" if claim == text else "unrelated",
            now=confirmed_at,
        )
        self.assertEqual(stats["absorbed"], 1)
        rows = conn.execute(
            """SELECT support_session_ids, last_confirmed, valid_to
               FROM memory_facts WHERE text = ?""",
            (text,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0][2])
        self.assertEqual(rows[0][1], confirmed_at)
        self.assertIn("new-session", json.loads(rows[0][0]))

    def test_conflict_keeps_the_old_claim(self):
        from core.distil import _create_cluster

        claim = "User drinks tea with breakfast"
        surprise = "User drank coffee with breakfast"
        _create_cluster(
            claim, _vec(12), source="distiller", label="breakfast", description="drink",
            scope="pattern", kind="observed", session_ids=["usual"],
        )
        candidate_id = record_candidate(
            surprise, _vec(12), kind="observed", about_user=True,
            session_ids=["once"], seen_at=_stamp(2026, 12, 15),
        )
        stats = consolidate(
            classify=lambda observation, existing: "conflict" if existing == claim else "unrelated",
            now=_stamp(2026, 12, 15, 18),
        )
        self.assertEqual(stats["flagged"], 1)
        active = conn.execute(
            "SELECT text FROM memory_facts WHERE text = ? AND valid_to IS NULL",
            (claim,),
        ).fetchone()
        self.assertEqual(active[0], claim)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM memory_facts WHERE text = ?",
            (surprise,),
        ).fetchone())
        status = conn.execute(
            "SELECT status FROM memory_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()[0]
        self.assertEqual(status, "flagged")
        conflict = conn.execute(
            "SELECT fact_id_a, fact_id_b FROM memory_conflicts WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        self.assertEqual(conflict[0], conflict[1])

    def test_compatible_detail_revises_the_claim(self):
        from core.distil import _create_cluster

        claim = "User takes the morning train"
        detail = "User takes the morning train from College Park"
        _create_cluster(
            claim, _vec(13), source="distiller", label="train_detail", description="commute",
            scope="pattern", kind="observed", session_ids=["base"],
        )
        record_candidate(
            detail, _vec(13), kind="observed", about_user=True,
            session_ids=["added"], seen_at=_stamp(2026, 12, 15),
        )
        stats = consolidate(
            classify=lambda observation, existing: "compatible" if existing == claim else "unrelated",
            compose=lambda existing, observation: detail,
            now=_stamp(2026, 12, 15, 18),
        )
        self.assertEqual(stats["attached"], 1)
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM memory_facts WHERE text = ? AND valid_to IS NOT NULL",
            (claim,),
        ).fetchone())
        revised = conn.execute(
            "SELECT support_session_ids FROM memory_facts WHERE text = ? AND valid_to IS NULL",
            (detail,),
        ).fetchone()
        self.assertIn("base", json.loads(revised[0]))
        self.assertIn("added", json.loads(revised[0]))

    def test_idle_gate_leaves_candidates_alone(self):
        record_candidate(
            "User sketched a trip", _vec(14), kind="observed", about_user=True,
            session_ids=["d1"], seen_at=_stamp(2026, 12, 10),
        )
        record_candidate(
            "User sketched a trip", _vec(14), kind="observed", about_user=True,
            session_ids=["d2"], seen_at=_stamp(2026, 12, 15),
        )
        with patch("core.platform_support.get_idle_seconds", return_value=0):
            self.assertIsNone(maybe_consolidate())
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM memory_candidates WHERE status = 'open'").fetchone()[0],
            2,
        )

    def test_vectors_used_above_do_not_match_each_other(self):
        self.assertLess(_cosine_similarity(VEC, OTHER), 0.75)

    def test_stated_claims_do_not_decay_and_confirmation_resets_the_clock(self):
        now = 1_800_000_000.0
        text = "User keeps a written note about their preferred editor"
        stated = fact_freshness(
            text=text, source="agent", valid_from=now - 200 * 86400,
            scope="stated", now=now,
        )
        stale = fact_freshness(
            text=text, source="distiller", valid_from=now - 200 * 86400,
            last_confirmed=now - 200 * 86400, scope="pattern", now=now,
        )
        confirmed = fact_freshness(
            text=text, source="distiller", valid_from=now - 200 * 86400,
            last_confirmed=now - 86400, scope="pattern", now=now,
        )
        self.assertGreater(stated, 0.9)
        self.assertGreater(confirmed, stale)

    def test_clear_events_drops_screen_candidates_and_keeps_stated_ones(self):
        record_candidate(
            "Screen-only candidate zz", _vec(15), kind="inferred", about_user=True,
            session_ids=["screen-zz"], seen_at=time.time(), source="screen",
        )
        record_candidate(
            "User-stated candidate zz", OTHER, kind="observed", about_user=True,
            session_ids=[], seen_at=time.time(), source="user",
        )
        clear_data(["events"])
        screen = conn.execute(
            "SELECT 1 FROM memory_candidates WHERE text = ?",
            ("Screen-only candidate zz",),
        ).fetchone()
        stated = conn.execute(
            "SELECT 1 FROM memory_candidates WHERE text = ?",
            ("User-stated candidate zz",),
        ).fetchone()
        self.assertIsNone(screen)
        self.assertIsNotNone(stated)


class DistilWritePathTests(unittest.TestCase):
    def test_screen_distillation_writes_candidates_only(self):
        token = "distil-candidate-coast-outline"
        base = time.time() + 10_000_000
        summary_ids = []
        previous = conn.execute(
            "SELECT key, value FROM memory_meta WHERE key = 'last_distilled_at'"
        ).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO memory_meta (key, value) VALUES ('last_distilled_at', ?)",
            (json.dumps(base - 10),),
        )
        conn.commit()
        try:
            for index in range(5):
                start = base + index * (31 * 60)
                summary_id = f"{token}-{index}"
                summary_ids.append(summary_id)
                conn.execute(
                    """INSERT OR REPLACE INTO sessions (
                        session_id, summary_id, created_at, window_start, window_end,
                        summary, active_task, entities, event_count, expires_at,
                        vision_enriched, private
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', 1, ?, 0, 0)""",
                    (
                        f"sid-{summary_id}", summary_id, start, start, start + 30,
                        f"{token} summary", "trip", start + 86_400,
                    ),
                )
            conn.commit()
            observations = [{
                "text": f"User kept notes for {token}",
                "kind": "observed",
                "about_user": True,
            }]
            with patch("core.distil._extract_observations", return_value=observations), \
                 patch("core.distil.embed_texts", return_value=[_vec(4)]), \
                 patch("core.distil._create_cluster", side_effect=AssertionError("screen write became a claim")), \
                 patch("core.memory_consolidation.maybe_consolidate", return_value=None):
                distil()
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM memory_candidates WHERE text = ?",
                (f"User kept notes for {token}",),
            ).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM memory_facts WHERE text = ?",
                (f"User kept notes for {token}",),
            ).fetchone())
        finally:
            if previous:
                conn.execute(
                    "INSERT OR REPLACE INTO memory_meta (key, value) VALUES ('last_distilled_at', ?)",
                    (previous[1],),
                )
            else:
                conn.execute("DELETE FROM memory_meta WHERE key = 'last_distilled_at'")
            for summary_id in summary_ids:
                conn.execute("DELETE FROM sessions WHERE summary_id = ?", (summary_id,))
            conn.execute("DELETE FROM memory_candidates WHERE text LIKE ?", (f"%{token}%",))
            conn.commit()

    def test_identity_is_not_copied_into_a_claim(self):
        field = "home_city_probe"

        def chat(messages, *args, **kwargs):
            system = messages[0]["content"]
            if "contains_facts" in system:
                return {"message": {"content": {"contains_facts": True}}}
            if "biographical" in system:
                return {"message": {"content": {"updates": [{
                    "field": field, "op": "set", "value": "College Park", "items": None,
                }]}}}
            if "atomic facts" in system:
                return {"message": {"content": {"facts": ["The user lives in College Park."]}}}
            raise AssertionError(system[:120])

        try:
            with patch("core.distil.gateway.chat", side_effect=chat):
                result = ingest_conversation("I live in College Park", "")
            self.assertIn(field, result["profile"])
            self.assertEqual(result["facts"], [])
            row = conn.execute(
                "SELECT value FROM memory_meta WHERE key = ?",
                (f"identity.{field}",),
            ).fetchone()
            self.assertIn("College Park", row[0])
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM memory_facts WHERE text = ?",
                ("The user lives in College Park.",),
            ).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM memory_candidates WHERE text = ?",
                ("The user lives in College Park.",),
            ).fetchone())
        finally:
            conn.execute("DELETE FROM memory_meta WHERE key = ?", (f"identity.{field}",))
            conn.commit()

    def test_a_one_time_statement_stays_a_candidate(self):
        sentence = "User spent one afternoon on the export screen"

        def chat(messages, *args, **kwargs):
            system = messages[0]["content"]
            if "contains_facts" in system:
                return {"message": {"content": {"contains_facts": True}}}
            if "biographical" in system:
                return {"message": {"content": {"updates": []}}}
            if "atomic facts" in system:
                return {"message": {"content": {"facts": [sentence]}}}
            raise AssertionError(system[:120])

        try:
            with patch("core.distil.gateway.chat", side_effect=chat), \
                 patch("core.distil.embed_texts", return_value=[_vec(5)]):
                result = ingest_conversation("I spent the afternoon on the export screen", "")
            self.assertEqual(result["facts"], [sentence])
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM memory_candidates WHERE text = ? AND source = 'user' AND status = 'open'",
                (sentence,),
            ).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM memory_facts WHERE text = ?",
                (sentence,),
            ).fetchone())
        finally:
            conn.execute("DELETE FROM memory_candidates WHERE text = ?", (sentence,))
            conn.commit()


if __name__ == "__main__":
    unittest.main()
