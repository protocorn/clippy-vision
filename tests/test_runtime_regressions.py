from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import imagehash
from PIL import Image

from agent.helpers.time_resolver import resolve_temporal_range
from agent.memory import get_autobiographical_context
from agent.prefetch.memory_query import memory_query
from agent.prefetch.specific_recall import (
    detect_artifact_type,
    search_events_for_artifact,
    specific_recall,
)
from agent.prefetch.topic_search import topic_search
from agent.router import _deterministic_route, classify_query
from classifier.tier_two_classifier import VERDICT_SCHEMA
from classifier.worker import apply_verdict, apply_vision_verdict
from core import rag
from core.accessibility_text import (
    is_useful_accessibility_text,
    normalize_accessibility_text,
)
from core.app_settings import (
    get_capture_settings,
    normalize_capture_settings,
    set_capture_settings,
)
from core.events import Event, WindowMetadata
from core.intro_builder import gather_intro_inputs
from core.llm_gateway import gateway
from core.local_embeddings import (
    MODEL_DIMENSION,
    MODEL_ID,
    embed_text,
    embedding_status,
)
from core.memory_store import save_identity_field, set_introduction
from core.paths import get_data_dir, get_screenshots_dir
from core.privacy_settings import (
    get_privacy_enabled,
    set_privacy_enabled,
    should_redact_window,
)
from core.screenshot_enrichment import enrich_screenshot, remember_accessibility_text
from core.screenshot_processor import (
    _get_nearest_event,
    _group_by_similarity,
    _process_group,
)
from core.screenshot_search import search_screenshots
from core.storage import (
    _ensure_column,
    _table_columns,
    clear_data,
    conn,
    export_data,
    get_data_stats,
    get_user_name,
    set_user_name,
    store_event,
    store_summary,
)
from core.summarizer import _build_prompt
from core.vision import _foreground_accessibility_text, get_screenshots_near


def make_event(event_id: str, event_type: str = "typing_burst", timestamp: float | None = None) -> Event:
    stamp = timestamp or time.time()
    return Event(
        event_id=event_id,
        session_id="test-session",
        timestamp=stamp,
        event_type=event_type,
        window_context=WindowMetadata(
            timestamp=stamp,
            current_window_title="Test window",
            active_url=None,
            process_name="TestApp",
        ),
        previous_window_context=None,
        payload={},
        summary=f"summary {event_id}",
        vector_embedding=None,
        image_embedding=None,
        image_embedding_model=None,
        screenshot_filename=None,
        interest_score=None,
        interest_reason=None,
        interesting=None,
    )


class _FakeProcess:
    """Stand-in for psutil.Process so reaping can be tested without real kills."""

    def __init__(self, pid: int, name: str, ppid: int):
        self.pid = pid
        self.info = {"name": name, "ppid": ppid}
        self.killed = False

    def kill(self) -> None:
        self.killed = True


class RuntimeRegressionTests(unittest.TestCase):
    def test_private_foreground_window_never_exposes_accessibility_text(self):
        metadata = {"process_name": "Slack", "current_window_title": "Private channel"}
        with patch("core.vision.get_window_metadata", return_value=metadata), patch(
            "core.vision.should_redact_window", return_value=True
        ), patch("core.vision.extract_accessibility_text") as extract_text:
            self.assertEqual(_foreground_accessibility_text(), "")
        extract_text.assert_not_called()

    def test_accessibility_text_is_discarded_if_foreground_window_changes(self):
        before = {"process_name": "Code", "current_window_title": "Editor", "active_url": None}
        after = {"process_name": "Slack", "current_window_title": "Private", "active_url": None}
        with patch("core.vision.get_window_metadata", side_effect=[before, after]), patch(
            "core.vision.should_redact_window", return_value=False
        ), patch("core.vision.is_clippy_window", return_value=False), patch(
            "core.vision.extract_accessibility_text", return_value="editor text"
        ):
            self.assertEqual(_foreground_accessibility_text(), "")

    def test_accessibility_text_is_kept_for_stable_safe_window(self):
        metadata = {"process_name": "Code", "current_window_title": "Editor", "active_url": None}
        with patch("core.vision.get_window_metadata", side_effect=[metadata, metadata]), patch(
            "core.vision.should_redact_window", return_value=False
        ), patch("core.vision.is_clippy_window", return_value=False), patch(
            "core.vision.extract_accessibility_text", return_value="editor text"
        ):
            self.assertEqual(_foreground_accessibility_text(), "editor text")

    def test_screen_text_is_included_in_session_summary_prompt(self):
        marker = "project alpha private milestone 4827 planning notes"
        prompt = _build_prompt([{
            "timestamp": 100.0,
            "summary": "Background screenshot",
            "vision_activity": "Code — Editor",
            "vision_ocr_text": marker,
        }])
        self.assertIn(marker, prompt)

    def test_packaged_app_includes_bundled_minilm(self):
        package_path = Path(__file__).resolve().parents[1] / "electron-ui" / "package.json"
        package = json.loads(package_path.read_text())
        filters = package["build"]["extraResources"][0]["filter"]
        self.assertIn("models/embeddings/all-MiniLM-L6-v2/**/*", filters)

    def test_clear_events_removes_activity_derived_memory_but_preserves_chat_memory(self):
        stamp = time.time()
        cluster_id = "mixed-source-cluster"
        conn.execute(
            """INSERT OR REPLACE INTO memory_clusters
               (cluster_id, label, description, centroid, created_at, updated_at, fact_count)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cluster_id, "mixed", "mixed sources", "[0.5, 0.5]", stamp, stamp, 2),
        )
        for fact_id, source, vector in (
            ("captured-fact", "distiller", "[1.0, 0.0]"),
            ("chat-fact", "agent", "[0.0, 1.0]"),
        ):
            conn.execute(
                """INSERT OR REPLACE INTO memory_facts
                   (fact_id, cluster_id, text, vector_embedding, valid_from, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (fact_id, cluster_id, fact_id, vector, stamp, source, stamp),
            )
        conn.commit()
        result = clear_data(["events"])
        self.assertGreaterEqual(result["memory_facts"], 1)
        self.assertIsNone(conn.execute("SELECT 1 FROM memory_facts WHERE fact_id='captured-fact'").fetchone())
        self.assertIsNotNone(conn.execute("SELECT 1 FROM memory_facts WHERE fact_id='chat-fact'").fetchone())
        count = conn.execute(
            "SELECT fact_count FROM memory_clusters WHERE cluster_id=?", (cluster_id,)
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_clear_learned_memory_preserves_user_authored_profile(self):
        save_identity_field("favorite_editor", "Zed", source="user", op="override")
        save_identity_field("inferred_skill", "Python", source="agent", op="override")
        set_introduction("User-authored introduction", source="user")
        result = clear_data(["memory"])
        self.assertGreaterEqual(result["identity_fields"], 1)
        profile = get_autobiographical_context()
        self.assertIn("favorite_editor: Zed", profile)
        self.assertIn("User-authored introduction", profile)
        self.assertNotIn("inferred_skill", profile)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_facts").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_clusters").fetchone()[0], 0)

    def test_only_artifact_queries_bypass_the_classifier(self):
        screenshot_decision, screenshot_confidence = classify_query(
            "What was I doing in the screenshot from 8/4/2026, 1:12:29 PM?"
        )
        self.assertEqual(screenshot_decision.primary, "specific_recall")
        self.assertIn("time_anchored", screenshot_decision.secondary)
        self.assertEqual(screenshot_confidence, 1.0)

        artifact = _deterministic_route("what was the link I copied?")
        self.assertEqual(artifact.primary, "specific_recall")
        self.assertIsNone(_deterministic_route("what do you know about me right now?"))
        self.assertIsNone(_deterministic_route("show me last Tuesday"))
        self.assertIsNone(_deterministic_route("what was I working on?"))

    def test_memory_prefetch_does_not_duplicate_the_profile(self):
        with patch(
            "agent.prefetch.memory_query._fetch_memory",
            return_value="semantic memory facts",
        ):
            result = memory_query("what do you know about me?", q_vec=[1.0])
        self.assertEqual(result, "semantic memory facts")

    def test_screen_keyword_miss_does_not_fall_back_to_recent_frames(self):
        event = make_event("unrelated-screen", event_type="screenshot_analysis")
        event["summary"] = "an unrelated captured frame"
        store_event(event)

        def cleanup_event():
            conn.execute("DELETE FROM events WHERE event_id=?", (event["event_id"],))
            conn.commit()

        self.addCleanup(cleanup_event)

        results = search_events_for_artifact(
            "screen",
            ["review_token_that_does_not_exist_1739"],
        )

        self.assertEqual(results, [])

    def test_exact_numeric_datetime_resolves_to_instant(self):
        result = resolve_temporal_range(
            "screenshot from 8/4/2026, 1:12:29 PM",
            now=datetime(2026, 8, 4, 14, 0, 0),
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.granularity, "instant")
        expected = datetime(2026, 8, 4, 13, 12, 29).timestamp()
        self.assertEqual(result.start_ts, expected - 2)
        self.assertEqual(result.end_ts, expected + 2)

    def test_exact_screenshot_recall_uses_only_matching_frame(self):
        stamp = datetime(2026, 8, 4, 13, 12, 29).timestamp()
        path = get_screenshots_dir() / f"{int(stamp * 1000)}.jpg"
        Image.new("RGB", (3, 3), "white").save(path, format="JPEG")
        event = make_event("exact-screenshot", event_type="screenshot_analysis", timestamp=stamp)
        event["window_context"]["process_name"] = "Clippy Vision"
        event["window_context"]["current_window_title"] = "New chat"
        event["summary"] = "Clippy Vision conversation drawer"
        event["screenshot_filename"] = path.name
        store_event(event)
        conn.execute(
            "UPDATE events SET vision_ocr_text=? WHERE event_id=?",
            ("New chat Conversations", event["event_id"]),
        )
        conn.commit()

        temporal_range = resolve_temporal_range(
            "screenshot from 8/4/2026, 1:12:29 PM",
            now=datetime(2026, 8, 4, 14, 0, 0),
        )
        result = specific_recall(
            "What was I doing in the screenshot from 8/4/2026, 1:12:29 PM?",
            temporal_range=temporal_range,
        )
        self.assertEqual(detect_artifact_type("that screenshot"), "screen")
        self.assertIn("exact screenshot evidence", result)
        self.assertIn("app: Clippy Vision", result)
        self.assertIn("window: New chat", result)
        self.assertIn(f"screenshot_source: {path.name}", result)

    def test_topic_search_falls_back_to_event_rag_without_sessions(self):
        event = make_event("topic-event-fallback", event_type="context_change")
        event["summary"] = "quasarneedle project planning"
        store_event(event)
        with patch("core.rag.get_capture_settings", return_value={"rag_enabled": True}):
            result = topic_search("quasarneedle", q_vec=None)
        self.assertIn("event-level activity fallback", result)
        self.assertIn("quasarneedle", result)

    def test_profile_context_is_not_duplicated_in_memory_prefetch(self):
        set_user_name("Profile Test User")
        set_introduction("I build local-first desktop tools.", source="user")
        save_identity_field("location", "Dubai", source="user", op="override")
        with patch(
            "agent.prefetch.memory_query._fetch_memory",
            return_value="semantic memory facts",
        ):
            result = memory_query(
                "what do you have stored in your local memory for me?",
                q_vec=[1.0],
            )
        self.assertEqual(result, "semantic memory facts")
        self.assertNotIn("Profile Test User", result)
        self.assertNotIn("Dubai", result)
        profile = get_autobiographical_context()
        self.assertIn("name: Profile Test User", profile)
        self.assertIn("I build local-first desktop tools.", profile)
        self.assertIn("location: Dubai", profile)

    def test_profile_name_has_one_canonical_store_and_reaches_intro_builder(self):
        conn.execute(
            "INSERT OR REPLACE INTO memory_meta (key, value) VALUES (?, ?)",
            ("identity.name", '{"type":"scalar","value":"Stale Name"}'),
        )
        conn.commit()
        save_identity_field("name", "Canonical Name", source="agent", op="override")
        self.assertEqual(get_user_name(), "Canonical Name")
        self.assertIsNone(
            conn.execute("SELECT 1 FROM memory_meta WHERE key='identity.name'").fetchone()
        )
        self.assertEqual(gather_intro_inputs()["identity"]["name"], "Canonical Name")
        with self.assertRaises(ValueError):
            set_user_name("   ")

    def test_profile_api_round_trip_uses_canonical_fields_and_rejects_reserved_duplicates(self):
        from api_server import ProfileUpdateRequest, write_user_profile

        result = write_user_profile(ProfileUpdateRequest(
            name="API Profile Name",
            introduction="API introduction",
            identity={"name": "Conflicting Name", "introduction": "Conflicting intro", "location": "Dubai"},
        ))
        self.assertEqual(result["name"], "API Profile Name")
        self.assertEqual(result["introduction"], "API introduction")
        self.assertEqual(result["identity"]["location"], "Dubai")
        self.assertNotIn("name", result["identity"])
        self.assertNotIn("introduction", result["identity"])

    def test_raw_retention_setting_controls_existing_and_new_event_expiry(self):
        original = get_capture_settings()
        try:
            stamp = time.time()
            existing = make_event("retention-existing", timestamp=stamp)
            store_event(existing)
            updated = set_capture_settings({"raw_retention_days": 30})
            self.assertEqual(updated["raw_retention_days"], 30)
            existing_expiry = conn.execute(
                "SELECT expires_at FROM events WHERE event_id=?", (existing["event_id"],)
            ).fetchone()[0]
            self.assertAlmostEqual(existing_expiry, stamp + 30 * 86400, delta=1)

            new_event = make_event("retention-new", timestamp=stamp)
            store_event(new_event)
            new_expiry = conn.execute(
                "SELECT expires_at FROM events WHERE event_id=?", (new_event["event_id"],)
            ).fetchone()[0]
            self.assertAlmostEqual(new_expiry, stamp + 30 * 86400, delta=1)
        finally:
            set_capture_settings(original)

    def test_export_contains_profile_settings_summaries_memory_and_screenshot_metadata(self):
        set_user_name("Export User")
        event = make_event("export-screenshot", event_type="screenshot_analysis")
        event["screenshot_filename"] = "export-frame.jpg"
        store_event(event)
        exported = export_data()
        self.assertEqual(exported["profile"]["name"], "Export User")
        self.assertIn("capture", exported["settings"])
        self.assertIn("privacy", exported["settings"])
        self.assertIn("session_summaries", exported)
        self.assertIn("facts", exported["memory"])
        match = next(item for item in exported["events"] if item["event_id"] == event["event_id"])
        self.assertEqual(match["screenshot_filename"], "export-frame.jpg")

    def test_storage_size_includes_sqlite_wal_and_shared_memory_files(self):
        expected = 0
        for suffix in ("", "-wal", "-shm"):
            path = get_data_dir() / f"events.db{suffix}"
            if path.exists():
                expected += path.stat().st_size
        self.assertEqual(get_data_stats()["database_bytes"], expected)

    def test_privacy_setting_round_trip_changes_runtime_redaction(self):
        original = get_privacy_enabled()
        try:
            updated = set_privacy_enabled({"slack": True})
            self.assertTrue(updated["slack"])
            self.assertTrue(should_redact_window("Slack", "Workspace"))
            set_privacy_enabled({"slack": False})
            self.assertFalse(should_redact_window("Slack", "Workspace"))
        finally:
            set_privacy_enabled(original)

    def test_fts_tracks_event_and_session_changes(self):
        stamp = time.time()
        event = make_event("fts-event", timestamp=stamp)
        event["summary"] = "fts_only_token_9417 phrase"
        store_event(event)
        self.assertIsNotNone(conn.execute("SELECT rowid FROM events_fts WHERE events_fts MATCH 'fts_only_token_9417'").fetchone())
        conn.execute("UPDATE events SET vision_ocr_text='ocr phrase' WHERE event_id=?", (event["event_id"],))
        conn.commit()
        self.assertIsNotNone(conn.execute("SELECT rowid FROM events_fts WHERE events_fts MATCH 'ocr'").fetchone())

        store_summary({
            "session_id": "test-session",
            "summary_id": "fts-session",
            "created_at": stamp,
            "window_start": stamp,
            "window_end": stamp,
            "summary": "session phrase",
            "active_task": "testing",
            "entities": [],
            "event_count": 1,
        })
        self.assertIsNotNone(conn.execute("SELECT rowid FROM sessions_fts WHERE sessions_fts MATCH 'session'").fetchone())

        conn.execute("DELETE FROM events WHERE event_id=?", (event["event_id"],))
        conn.execute("DELETE FROM sessions WHERE summary_id=?", ("fts-session",))
        conn.commit()
        self.assertIsNone(conn.execute("SELECT rowid FROM events_fts WHERE events_fts MATCH 'fts_only_token_9417'").fetchone())
        self.assertIsNone(conn.execute("SELECT rowid FROM sessions_fts WHERE sessions_fts MATCH 'session'").fetchone())

    def test_processed_screenshots_are_discoverable_and_searchable(self):
        stamp = int(time.time() * 1000)
        path = get_screenshots_dir() / f"{stamp}_processed.jpg"
        Image.new("RGB", (3, 3), "white").save(path, format="JPEG")
        event = make_event("screenshot-event", timestamp=stamp / 1000)
        event["screenshot_filename"] = path.name
        event["image_embedding"] = [1.0, 0.0]
        event["image_embedding_model"] = "visual-signature-v1"
        store_event(event)
        names = {item.name for item in get_screenshots_near(event["timestamp"], max_count=4, window_secs=1)}
        self.assertIn(path.name, names)
        result = search_screenshots(limit=-1, offset=-20)
        self.assertTrue(any(item["screenshot_filename"] == path.name for item in result["screenshots"]))

    def test_screenshot_time_filter_is_applied_before_candidate_limit(self):
        target_id = "old-screenshot-target"
        noise_prefix = "newer-screenshot-noise-"
        try:
            conn.execute(
                """INSERT INTO events
                   (event_id, session_id, timestamp, event_type, summary, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (target_id, "test-session", 100.0, "screenshot_analysis", "old target", 9999999999.0),
            )
            conn.execute(
                """WITH RECURSIVE sequence(value) AS (
                       SELECT 1 UNION ALL SELECT value + 1 FROM sequence WHERE value < 3000
                   )
                   INSERT INTO events
                       (event_id, session_id, timestamp, event_type, summary, expires_at)
                   SELECT ? || value, 'test-session', 200.0 + value,
                          'screenshot_analysis', 'newer noise', 9999999999.0
                   FROM sequence""",
                (noise_prefix,),
            )
            conn.commit()

            result = search_screenshots(start_ts=99.0, end_ts=101.0)

            self.assertEqual([item["event_id"] for item in result["screenshots"]], [target_id])
        finally:
            conn.execute("DELETE FROM events WHERE event_id = ? OR event_id LIKE ?", (target_id, f"{noise_prefix}%"))
            conn.commit()

    def test_vision_updates_only_pending_vision_events(self):
        event = make_event("vision-race")
        store_event(event)
        conn.execute("UPDATE events SET classification_status='screenshot_only' WHERE event_id=?", (event["event_id"],))
        conn.commit()
        verdict = {
            "verdict": "interesting",
            "score": 8,
            "reason": "test",
            "ocr_text": "first",
            "user_activity": "testing",
            "suggested_action": None,
        }
        apply_vision_verdict(event["event_id"], verdict, screenshot_filename="first.jpg")
        apply_vision_verdict(event["event_id"], {**verdict, "ocr_text": "stale"}, screenshot_filename="stale.jpg")
        row = conn.execute(
            "SELECT vision_ocr_text, screenshot_filename, classification_status FROM events WHERE event_id=?",
            (event["event_id"],),
        ).fetchone()
        self.assertEqual(row, ("first", "first.jpg", "done"))

    def test_screenshot_enrichment_attaches_to_done_event_without_reclassifying_it(self):
        event = make_event("done-screen-enrichment", timestamp=12345.0)
        store_event(event)
        def cleanup_event():
            conn.execute("DELETE FROM events WHERE event_id=?", (event["event_id"],))
            conn.commit()
        self.addCleanup(cleanup_event)
        apply_verdict(event["event_id"], {
            "verdict": "interesting",
            "score": 9,
            "reason": "important typing",
        })
        nearest = _get_nearest_event(event["timestamp"])
        self.assertEqual(nearest["event_id"], event["event_id"])
        applied = apply_vision_verdict(event["event_id"], {
            "verdict": "not_interesting",
            "score": 5,
            "reason": "screen text captured",
            "ocr_text": "project alpha",
            "user_activity": "TestApp — Test window",
            "suggested_action": None,
        })
        self.assertTrue(applied)
        row = conn.execute(
            "SELECT interesting, interest_score, interest_reason, vision_ocr_text FROM events WHERE event_id=?",
            (event["event_id"],),
        ).fetchone()
        self.assertEqual(row, (1, 9.0, "important typing", "project alpha"))

    def test_phash_group_keeps_each_frames_captured_text(self):
        group = [Path("1000.jpg"), Path("2000.jpg")]
        events = [
            {"event_id": "new", "event_type": "screenshot_analysis", "process_name": "App", "window_context": {}, "summary": "new"},
            {"event_id": "old", "event_type": "screenshot_analysis", "process_name": "App", "window_context": {}, "summary": "old"},
        ]
        with patch("core.screenshot_processor._get_nearest_event", side_effect=events), patch(
            "core.screenshot_processor.enrich_screenshot",
            side_effect=[("new frame text", [1.0], "clip:test"), ("old frame text", None, None)],
        ) as enrich, patch("core.screenshot_processor.apply_vision_verdict", return_value=True) as apply, patch(
            "core.screenshot_processor._mark_as_processed", return_value=True
        ):
            self.assertTrue(_process_group(group))
        self.assertEqual(enrich.call_count, 2)
        self.assertEqual(apply.call_args_list[0].args[1]["ocr_text"], "new frame text")
        self.assertEqual(apply.call_args_list[1].args[1]["ocr_text"], "old frame text")

    def test_phash_bursts_do_not_chain_past_the_time_window(self):
        paths = [Path("1000.jpg"), Path("21000.jpg"), Path("41000.jpg")]
        digest = imagehash.hex_to_hash("0" * 16)
        groups = _group_by_similarity(paths, {path.stem: digest for path in paths})
        self.assertEqual(sorted(len(group) for group in groups), [1, 2])

    def test_accessibility_text_skips_ocr_when_ui_text_is_useful(self):
        path = get_screenshots_dir() / "accessibility-first.jpg"
        Image.new("RGB", (4, 4), "white").save(path, format="JPEG")
        self.addCleanup(path.unlink, missing_ok=True)
        ui_text = normalize_accessibility_text(
            "Project settings\nConfigure local capture and privacy controls"
        )
        self.assertTrue(is_useful_accessibility_text(ui_text))
        remember_accessibility_text(path, ui_text)

        with patch(
            "core.screenshot_enrichment.get_capture_settings",
            return_value={"ocr_enabled": True, "image_embeddings_enabled": False},
        ), patch("core.screenshot_enrichment.extract_text") as extract_ocr:
            captured_text, image_embedding, image_model = enrich_screenshot(path)

        extract_ocr.assert_not_called()
        self.assertEqual(captured_text, ui_text)
        self.assertIsNone(image_embedding)
        self.assertIsNone(image_model)

    def test_sparse_accessibility_text_falls_back_to_ocr(self):
        path = get_screenshots_dir() / "accessibility-fallback.jpg"
        Image.new("RGB", (4, 4), "white").save(path, format="JPEG")
        self.addCleanup(path.unlink, missing_ok=True)
        remember_accessibility_text(path, "OK")

        with patch(
            "core.screenshot_enrichment.get_capture_settings",
            return_value={"ocr_enabled": True, "image_embeddings_enabled": False},
        ), patch("core.screenshot_enrichment.extract_text", return_value="Document body from OCR with enough words to count as useful screen text") as extract_ocr:
            captured_text, _, _ = enrich_screenshot(path)

        extract_ocr.assert_called_once_with(path)
        # Useful OCR replaces weak a11y — do not merge chrome/noise with OCR.
        self.assertEqual(
            captured_text,
            "Document body from OCR with enough words to count as useful screen text",
        )
        self.assertNotIn("OK", captured_text)

    def test_weak_ocr_keeps_accessibility_fallback(self):
        path = get_screenshots_dir() / "accessibility-ocr-weak.jpg"
        Image.new("RGB", (4, 4), "white").save(path, format="JPEG")
        self.addCleanup(path.unlink, missing_ok=True)
        weak_a11y = "remote\nWarnings: 7\nToggle Panel"
        remember_accessibility_text(path, weak_a11y)

        with patch(
            "core.screenshot_enrichment.get_capture_settings",
            return_value={"ocr_enabled": True, "image_embeddings_enabled": False},
        ), patch("core.screenshot_enrichment.extract_text", return_value="OK"):
            captured_text, _, _ = enrich_screenshot(path)

        self.assertIn("Warnings: 7", captured_text)
        self.assertNotIn("OK", captured_text)

    def test_choose_screen_text_prefers_useful_ocr_over_a11y_noise(self):
        from core.screenshot_enrichment import choose_screen_text

        a11y = "\n".join(["Pin conversation", "Open conversation options"] * 8)
        ocr = "Crafting Interview Introduction. Write a strong opening paragraph about your goals."
        self.assertEqual(choose_screen_text(a11y, ocr), ocr)

    def test_capture_models_and_event_rag_are_opt_in(self):
        settings = normalize_capture_settings({})
        self.assertFalse(settings["image_embeddings_enabled"])
        self.assertFalse(settings["rag_enabled"])
        self.assertNotIn("needs_vision", VERDICT_SCHEMA["properties"]["verdict"]["enum"])

    def test_text_embeddings_are_bundled_and_local(self):
        vector = embed_text("local semantic memory test")
        status = embedding_status()
        self.assertEqual(len(vector), MODEL_DIMENSION)
        self.assertEqual(MODEL_DIMENSION, 384)
        self.assertEqual(status["provider"], "bundled")
        self.assertEqual(status["model"], MODEL_ID)
        self.assertTrue(status["bundled"])
        self.assertTrue(status["loaded"])

    def test_gateway_embedding_uses_local_model_without_provider(self):
        vector = gateway.embed("provider-independent embedding", embed_model="remote-provider-model")
        self.assertEqual(len(vector), MODEL_DIMENSION)

    def test_gateway_preempts_background_response_for_waiting_chat(self):
        from core.llm_gateway import Job, Priority

        class FakeResponse:
            closed = False

            def close(self):
                self.closed = True

        job = Job("http://example.invalid", {}, timeout=1)
        response = FakeResponse()
        with gateway._state_lock:
            old_job = gateway._current_job
            old_priority = gateway._current_priority
            gateway._current_job = job
            gateway._current_priority = Priority.BACKGROUND
            job.response = response
        try:
            gateway._cancel_background_for_chat(job)
            self.assertTrue(job.cancel_requested.is_set())
            self.assertTrue(response.closed)
        finally:
            with gateway._state_lock:
                gateway._current_job = old_job
                gateway._current_priority = old_priority

    def test_normal_classifier_can_finish_pending_event(self):
        event = make_event("normal-classification")
        store_event(event)
        apply_verdict(event["event_id"], {
            "verdict": "not_interesting",
            "score": 2,
            "reason": "routine",
        })
        status = conn.execute(
            "SELECT classification_status FROM events WHERE event_id=?",
            (event["event_id"],),
        ).fetchone()
        self.assertEqual(status, ("done",))

    def test_event_rag_keeps_keyword_search_when_embeddings_are_unavailable(self):
        event = make_event("keyword-rag", event_type="context_change")
        event["summary"] = "unique keyword fallback phrase"
        store_event(event)
        original_embed_text = rag.embed_text
        original_embed_texts = rag.embed_texts
        rag.embed_text = lambda *args, **kwargs: []
        rag.embed_texts = lambda texts: [[] for _ in texts]
        try:
            with patch("core.rag.get_capture_settings", return_value={"rag_enabled": True}):
                result = rag.search_event_rag("keyword fallback phrase")
        finally:
            rag.embed_text = original_embed_text
            rag.embed_texts = original_embed_texts
        self.assertIsNotNone(result)
        rows, total = result
        self.assertGreaterEqual(total, 1)
        self.assertIn("keyword-rag", "\n".join(rows))

    def test_rag_indexer_stops_promptly(self):
        rag.stop_event_indexer(wait=True)
        calls = []
        with patch.object(rag, "INDEX_INTERVAL_SECONDS", 0.01), patch.object(
            rag, "get_capture_settings", return_value={"rag_enabled": True}
        ), patch.object(rag, "_index_pending_once", side_effect=lambda: calls.append(time.time())):
            thread = rag.start_event_indexer()
            deadline = time.time() + 1
            while not calls and time.time() < deadline:
                time.sleep(0.01)
            rag.stop_event_indexer(wait=True)
            count = len(calls)
            time.sleep(0.04)
        self.assertIsNotNone(thread)
        self.assertGreater(count, 0)
        self.assertEqual(len(calls), count)

    def test_clearing_screenshots_removes_derived_data(self):
        path = get_screenshots_dir() / "clear-me.jpg"
        path.write_bytes(b"jpeg")
        event = make_event("clear-screen", event_type="screenshot_analysis")
        event["screenshot_filename"] = path.name
        event["image_embedding"] = [1.0]
        event["image_embedding_model"] = "visual-signature-v1"
        store_event(event)
        conn.execute("UPDATE events SET vision_ocr_text='private text' WHERE event_id=?", (event["event_id"],))
        conn.commit()
        result = clear_data(["screenshots"])
        self.assertEqual(result["screenshots"], 1)
        self.assertFalse(path.exists())
        self.assertIsNone(conn.execute("SELECT 1 FROM events WHERE event_id=?", (event["event_id"],)).fetchone())

    def test_ensure_column_is_idempotent(self):
        before = _table_columns("events")
        self.assertIn("classification_status", before)
        _ensure_column("events", "classification_status", "TEXT DEFAULT 'pending'")
        _ensure_column("events", "classification_status", "TEXT DEFAULT 'pending'")
        _ensure_column("sessions", "summary_embedding", "TEXT")
        self.assertEqual(_table_columns("events"), before)

    def test_ram_floors_tier_work_by_model_size(self):
        """CPU-only floors; a GPU box is covered by the gpu_can_host_text test."""
        from core import model_residency as residency
        gb = residency._GB
        with patch.object(residency, "_commit_pressured", return_value=False), patch.object(
            residency, "text_model_loaded", return_value=False
        ), patch.object(residency, "server_reachable", return_value=True), patch.object(
            residency, "gpu_can_host_text", return_value=False
        ):
            with patch.object(residency, "_available", return_value=int(3.0 * gb)):
                self.assertTrue(residency.can_load_text())
            with patch.object(residency, "_available", return_value=int(1.2 * gb)):
                self.assertFalse(residency.can_load_text())
                self.assertTrue(residency.can_load_light())
            with patch.object(residency, "_available", return_value=int(0.6 * gb)):
                self.assertFalse(residency.can_load_light())
                self.assertTrue(residency.can_run_ocr())
            with patch.object(residency, "_available", return_value=int(0.2 * gb)):
                self.assertFalse(residency.can_run_ocr())

    def test_resident_text_model_allows_inference_despite_low_free_ram(self):
        """Chat already paid for the model; summarizer must not defer on available RAM."""
        from core import model_residency as residency
        with patch.object(residency, "text_model_loaded", return_value=True), patch.object(
            residency, "_available", return_value=int(0.4 * residency._GB)
        ), patch.object(residency, "_commit_pressured", return_value=False), patch.object(
            residency, "server_reachable", return_value=True
        ), patch.object(residency, "gpu_can_host_text", return_value=False):
            self.assertTrue(residency.can_load_text())
            # Without a GPU the cold-load floor still applies to a fresh load.
            self.assertFalse(residency.can_cold_load_text())

    def test_ensure_text_model_tries_warm_below_floor(self):
        """Startup/summarizer must attempt load instead of hard-skipping on free RAM."""
        from core import model_residency as residency

        with patch.object(residency, "text_model_loaded", side_effect=[False, True]), patch.object(
            residency, "_available", return_value=int(0.9 * residency._GB)
        ), patch.object(residency, "_commit_pressured", return_value=False), patch.object(
            residency, "gpu_can_host_text", return_value=False
        ), patch.object(residency, "_warm") as warm:
            self.assertTrue(residency.ensure_text_model(force=True))
            warm.assert_called_once()

    def test_ollama_bind_address_is_normalized_for_clients(self):
        """OLLAMA_HOST often holds a bind address; 0.0.0.0 is not connectable."""
        from core import ollama_client

        cases = {
            "0.0.0.0:11434": "http://127.0.0.1:11434",
            "http://0.0.0.0:11434": "http://127.0.0.1:11434",
            "0.0.0.0": "http://127.0.0.1:11434",
            "http://127.0.0.1:11434": "http://127.0.0.1:11434",
            "http://192.168.1.5:11434": "http://192.168.1.5:11434",
        }
        for raw, expected in cases.items():
            with patch.dict(os.environ, {"OLLAMA_HOST": raw, "CLIPPY_OLLAMA_URL": ""}, clear=False):
                self.assertEqual(ollama_client.base_url(), expected, raw)

    def test_ollama_errors_carry_server_message(self):
        """A bare 'HTTP Error 500' hides the cause; the body must be surfaced."""
        import urllib.error
        from core import ollama_client

        body = io.BytesIO(json.dumps({"error": "model requires more system memory"}).encode())
        err = urllib.error.HTTPError("http://x/api/generate", 500, "Internal Server Error", {}, body)
        described = ollama_client.describe_http_error(err)
        self.assertIn("HTTP 500", described)
        self.assertIn("model requires more system memory", described)

    def test_unreachable_ollama_is_distinct_from_rejection(self):
        from core import ollama_client

        with patch.dict(os.environ, {"CLIPPY_OLLAMA_URL": "http://127.0.0.1:1"}, clear=False):
            with self.assertRaises(ollama_client.OllamaUnavailable):
                ollama_client.get_json("/api/version", timeout=2)
            self.assertFalse(ollama_client.server_reachable(timeout=2))

    def test_gpu_machines_ignore_system_ram_floor_for_text_load(self):
        """qwen3:8b loads into VRAM, so free system RAM must not gate the load."""
        from core import model_residency as residency

        with patch.object(residency, "server_reachable", return_value=True), patch.object(
            residency, "gpu_can_host_text", return_value=True
        ), patch.object(residency, "_available", return_value=int(0.3 * residency._GB)), patch.object(
            residency, "_commit_pressured", return_value=False
        ):
            self.assertTrue(residency.can_cold_load_text())

        with patch.object(residency, "server_reachable", return_value=True), patch.object(
            residency, "gpu_can_host_text", return_value=False
        ), patch.object(residency, "_available", return_value=int(0.3 * residency._GB)), patch.object(
            residency, "_commit_pressured", return_value=False
        ):
            self.assertFalse(residency.can_cold_load_text())

    def test_text_load_blocked_when_ollama_is_down(self):
        """An unreachable server must be reported as such, not as low memory."""
        from core import model_residency as residency

        with patch.object(residency, "text_model_loaded", return_value=False), patch.object(
            residency, "server_reachable", return_value=False
        ):
            self.assertFalse(residency.can_cold_load_text())
            self.assertIn("not reachable", residency.text_unavailable_reason())

    def test_orphaned_model_runners_are_reaped_but_live_ones_survive(self):
        """Runners outliving their Ollama squat VRAM until a fresh load OOMs."""
        from core import model_residency as residency

        orphan = _FakeProcess(4001, "llama-server.exe", ppid=9999)
        adopted = _FakeProcess(4002, "llama-server.exe", ppid=1234)
        unrelated = _FakeProcess(4003, "chrome.exe", ppid=9999)

        with patch.object(
            residency.psutil, "process_iter", return_value=[orphan, adopted, unrelated]
        ), patch.object(residency.psutil, "pid_exists", side_effect=lambda pid: pid == 1234):
            self.assertEqual(residency.reap_orphaned_runners(), 1)

        self.assertTrue(orphan.killed)
        self.assertFalse(adopted.killed)
        self.assertFalse(unrelated.killed)

    def test_out_of_memory_warm_reclaims_vram_and_retries(self):
        """A cudaMalloc failure must self-heal instead of leaving text unloaded."""
        from core import model_residency as residency

        attempts = []

        def warm(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("HTTP 500: cudaMalloc failed: out of memory")

        with patch.object(residency, "text_model_loaded", side_effect=[False, True]), patch.object(
            residency, "_commit_pressured", return_value=False
        ), patch.object(residency, "_available", return_value=int(8 * residency._GB)), patch.object(
            residency, "gpu_can_host_text", return_value=True
        ), patch.object(residency, "_warm", side_effect=warm), patch.object(
            residency, "reap_orphaned_runners", return_value=1
        ) as reap:
            self.assertTrue(residency.ensure_text_model(force=True))
            reap.assert_called_once()
            self.assertEqual(len(attempts), 2)

    def test_ensure_text_model_skips_when_commit_pressured(self):
        from core import model_residency as residency

        with patch.object(residency, "text_model_loaded", return_value=False), patch.object(
            residency, "_commit_pressured", return_value=True
        ), patch.object(residency, "_warm") as warm:
            self.assertFalse(residency.ensure_text_model(force=False))
            warm.assert_not_called()

    def test_commit_pressure_defers_work_even_with_free_ram(self):
        """Zombie model runners exhaust Windows commit while physical RAM looks fine."""
        from core import model_residency as residency
        with patch.object(residency, "text_model_loaded", return_value=False), patch.object(
            residency, "_available", return_value=int(8 * residency._GB)
        ), patch.object(residency, "_commit_pressured", return_value=True):
            self.assertFalse(residency.can_load_text())
            self.assertFalse(residency.can_load_light())
            self.assertFalse(residency.can_run_ocr())

    def test_live_classify_defers_ambiguous_events_without_llm(self):
        from classifier.worker import classify_event

        event = make_event("defer-ambiguous", event_type="context_change")
        event["window_context"]["process_name"] = "Cursor.exe"
        event["window_context"]["current_window_title"] = "Clippy_Vision"
        event["summary"] = "Switched to Cursor.exe from chrome.exe after 12s"
        store_event(event)
        payload = {
            "event_id": event["event_id"],
            "timestamp": event["timestamp"],
            "event_type": event["event_type"],
            "process_name": "Cursor.exe",
            "summary": event["summary"],
            "payload": "{}",
            "window_context": event["window_context"],
            "previous_window_context": {
                "process_name": "chrome.exe",
                "current_window_title": "Docs",
            },
        }
        with patch("classifier.worker.tier_zero_classifier", return_value=None), patch(
            "classifier.worker.tier1_score", return_value=None
        ), patch("classifier.worker.lookup_duplicate_verdict", return_value=None), patch(
            "classifier.worker.classify_with_llm"
        ) as llm:
            classify_event(payload, allow_tier2=False)
        llm.assert_not_called()
        status = conn.execute(
            "SELECT classification_status FROM events WHERE event_id=?",
            (event["event_id"],),
        ).fetchone()[0]
        self.assertEqual(status, "deferred")

    def test_duplicate_ambiguous_events_reuse_cached_verdict(self):
        from classifier.worker import classify_event

        first = make_event("dup-first", event_type="context_change", timestamp=1000.0)
        first["window_context"]["process_name"] = "chrome.exe"
        first["window_context"]["current_window_title"] = "Docs"
        first["summary"] = "Switched to chrome.exe - Docs from Cursor.exe"
        store_event(first)
        apply_verdict(first["event_id"], {
            "verdict": "not_interesting",
            "score": 2,
            "reason": "routine app switch",
        })

        second = make_event("dup-second", event_type="context_change", timestamp=1010.0)
        second["window_context"]["process_name"] = "chrome.exe"
        second["window_context"]["current_window_title"] = "Docs"
        second["summary"] = "Switched to chrome.exe - Docs from Cursor.exe"
        store_event(second)
        payload = {
            "event_id": second["event_id"],
            "timestamp": second["timestamp"],
            "event_type": second["event_type"],
            "process_name": "chrome.exe",
            "summary": second["summary"],
            "payload": "{}",
            "window_context": second["window_context"],
            "previous_window_context": {
                "process_name": "Cursor.exe",
                "current_window_title": "Editor",
            },
        }
        with patch("classifier.worker.tier_zero_classifier", return_value=None), patch(
            "classifier.worker.tier1_score", return_value=None
        ), patch("classifier.worker.classify_with_llm") as llm:
            classify_event(payload, allow_tier2=False)
        llm.assert_not_called()
        row = conn.execute(
            "SELECT classification_status, interesting, interest_reason FROM events WHERE event_id=?",
            (second["event_id"],),
        ).fetchone()
        self.assertEqual(row[0], "done")
        self.assertEqual(row[1], 0)
        self.assertIn("Cached duplicate", row[2])

    def test_catch_up_classify_calls_llm_and_completes(self):
        from classifier.worker import classify_event

        event = make_event("catch-up-llm", event_type="context_change")
        event["window_context"]["process_name"] = "UniqueApp.exe"
        event["summary"] = "Switched to UniqueApp.exe for rare work"
        store_event(event)
        conn.execute(
            "UPDATE events SET classification_status='deferred' WHERE event_id=?",
            (event["event_id"],),
        )
        conn.commit()
        payload = {
            "event_id": event["event_id"],
            "timestamp": event["timestamp"],
            "event_type": event["event_type"],
            "process_name": "UniqueApp.exe",
            "summary": event["summary"],
            "payload": "{}",
            "window_context": event["window_context"],
            "previous_window_context": None,
        }
        with patch("classifier.worker.tier_zero_classifier", return_value=None), patch(
            "classifier.worker.tier1_score", return_value=None
        ), patch("classifier.worker.lookup_duplicate_verdict", return_value=None), patch(
            "classifier.worker.classify_with_llm",
            return_value={"verdict": "interesting", "score": 8, "reason": "novel app"},
        ) as llm:
            classify_event(payload, allow_tier2=True)
        llm.assert_called_once()
        row = conn.execute(
            "SELECT classification_status, interesting FROM events WHERE event_id=?",
            (event["event_id"],),
        ).fetchone()
        self.assertEqual(row, ("done", 1))

    def test_catch_up_ram_failure_leaves_deferred_without_sleeping(self):
        from classifier.worker import classify_event

        event = make_event("catch-up-ram", event_type="context_change")
        event["window_context"]["process_name"] = "OtherApp.exe"
        store_event(event)
        conn.execute(
            "UPDATE events SET classification_status='deferred' WHERE event_id=?",
            (event["event_id"],),
        )
        conn.commit()
        payload = {
            "event_id": event["event_id"],
            "timestamp": event["timestamp"],
            "event_type": event["event_type"],
            "process_name": "OtherApp.exe",
            "summary": event["summary"],
            "payload": "{}",
            "window_context": event["window_context"],
            "previous_window_context": None,
        }
        with patch("classifier.worker.tier_zero_classifier", return_value=None), patch(
            "classifier.worker.tier1_score", return_value=None
        ), patch("classifier.worker.lookup_duplicate_verdict", return_value=None), patch(
            "classifier.worker.classify_with_llm",
            side_effect=OSError("deferred: ram below floor"),
        ), patch("classifier.worker.time.sleep") as sleep:
            classify_event(payload, allow_tier2=True)
        sleep.assert_not_called()
        status = conn.execute(
            "SELECT classification_status FROM events WHERE event_id=?",
            (event["event_id"],),
        ).fetchone()[0]
        self.assertEqual(status, "deferred")

    def test_backlog_status_auto_catch_up_gates(self):
        from core.backlog import get_backlog_status
        from core import backlog as backlog_mod

        backlog_mod._cooldown_until = 0.0
        backlog_mod._deferred_skip_until.clear()
        event = make_event("backlog-api-event", timestamp=time.time() - 20 * 60)
        store_event(event)
        conn.execute(
            "UPDATE events SET classification_status='deferred' WHERE event_id=?",
            (event["event_id"],),
        )
        conn.commit()

        status = get_backlog_status()
        self.assertGreaterEqual(status["deferred"], 1)
        self.assertTrue(status["recommend"])

        with patch.object(backlog_mod, "_counts", return_value={
            "deferred": 3,
            "pending": 0,
            "oldest_deferred_ts": time.time(),
            "oldest_deferred_age_secs": 10,
            "recommend": False,
        }), patch.object(backlog_mod, "get_capture_status", return_value={"active": True}):
            self.assertFalse(backlog_mod.catch_up_allowed())
        with patch.object(backlog_mod, "_counts", return_value={
            "deferred": 80,
            "pending": 0,
            "oldest_deferred_ts": time.time() - 1000,
            "oldest_deferred_age_secs": 1000,
            "recommend": True,
        }), patch.object(backlog_mod, "get_capture_status", return_value={"active": True}):
            self.assertTrue(backlog_mod.catch_up_allowed())
        with patch.object(backlog_mod, "_counts", return_value={
            "deferred": 1,
            "pending": 0,
            "oldest_deferred_ts": time.time(),
            "oldest_deferred_age_secs": 1,
            "recommend": False,
        }), patch.object(backlog_mod, "get_capture_status", return_value={"active": False}):
            self.assertTrue(backlog_mod.catch_up_allowed())

    def test_catch_up_timeout_uses_long_cooldown_and_event_skip(self):
        from core import backlog as backlog_mod

        backlog_mod._cooldown_until = 0.0
        backlog_mod._deferred_skip_until.clear()
        backlog_mod.note_catch_up_failure("timed out")
        self.assertGreater(
            backlog_mod._cooldown_until - time.time(),
            backlog_mod.CATCH_UP_COOLDOWN_SECS,
        )
        self.assertFalse(backlog_mod.catch_up_allowed())
        backlog_mod.note_deferred_event_skip("evt-timeout")
        self.assertTrue(backlog_mod.deferred_event_skipped("evt-timeout"))
        backlog_mod._cooldown_until = 0.0
        backlog_mod._deferred_skip_until.clear()

    def test_vision_refresh_backoff_then_give_up(self):
        from core import summarizer as summarizer_mod

        summarizer_mod._refresh_failures.clear()
        sid = "summary-hot-loop"
        self.assertFalse(summarizer_mod._note_refresh_failure(sid, TimeoutError("timed out")))
        self.assertTrue(summarizer_mod._refresh_in_backoff(sid))
        self.assertFalse(summarizer_mod._note_refresh_failure(sid, TimeoutError("timed out")))
        self.assertTrue(
            summarizer_mod._note_refresh_failure(sid, TimeoutError("timed out"))
        )
        summarizer_mod._refresh_failures.clear()


if __name__ == "__main__":
    unittest.main()
