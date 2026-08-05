from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
import unittest
from datetime import datetime
from unittest.mock import patch


os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
import imagehash

from classifier.worker import apply_verdict, apply_vision_verdict
from core.events import Event, WindowMetadata
from core.paths import get_data_dir, get_screenshots_dir
from core.screenshot_search import search_screenshots
from core.screenshot_processor import _group_by_similarity
from core import rag
from core.cli_providers import _build_command, _extract_output
from core.llm_config import normalize_config
from core.local_embeddings import MODEL_DIMENSION, MODEL_ID, embed_text, embedding_status
from core.llm_gateway import gateway
from core.storage import (
    clear_data,
    conn,
    export_data,
    get_data_stats,
    get_user_name,
    store_event,
    store_summary,
    set_user_name,
)
from core.vision import get_screenshots_near
from agent.router import classify_query
from agent.helpers.time_resolver import resolve_temporal_range
from agent.prefetch.specific_recall import detect_artifact_type, specific_recall
from agent.prefetch.topic_search import topic_search
from agent.prefetch.memory_query import memory_query
from agent.memory import get_autobiographical_context
from core.memory_store import save_identity_field, set_introduction
from core.app_settings import get_capture_settings, set_capture_settings
from core.intro_builder import gather_intro_inputs
from core.llm_config import get_llm_config, public_llm_config, save_llm_config
from core.privacy_settings import get_privacy_enabled, set_privacy_enabled, should_redact_window


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


class RuntimeRegressionTests(unittest.TestCase):
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

    def test_memory_and_screenshot_queries_route_without_classifier_checkpoint(self):
        memory_decision, memory_confidence = classify_query("what do you know about me right now?")
        self.assertEqual(memory_decision.primary, "memory_query")
        self.assertIn("topic_search", memory_decision.secondary)
        self.assertEqual(memory_confidence, 1.0)

        screenshot_decision, screenshot_confidence = classify_query(
            "What was I doing in the screenshot from 8/4/2026, 1:12:29 PM?"
        )
        self.assertEqual(screenshot_decision.primary, "specific_recall")
        self.assertIn("time_anchored", screenshot_decision.secondary)
        self.assertEqual(screenshot_confidence, 1.0)

        dated_decision, dated_confidence = classify_query("Show me last Tuesday")
        self.assertEqual(dated_decision.primary, "time_anchored")
        self.assertIn("topic_search", dated_decision.secondary)
        self.assertEqual(dated_confidence, 1.0)

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
        result = topic_search("quasarneedle", q_vec=None)
        self.assertIn("event-level activity fallback", result)
        self.assertIn("quasarneedle", result)

    def test_memory_overview_reports_explicit_and_observed_storage_separately(self):
        set_user_name("Profile Test User")
        set_introduction("I build local-first desktop tools.", source="user")
        save_identity_field("location", "Dubai", source="user", op="override")
        result = memory_query("what do you have stored in your local memory for me?")
        self.assertIn("personal memory inventory", result)
        self.assertIn("display_name: Profile Test User", result)
        self.assertIn('"location": "Dubai"', result)
        self.assertIn("explicit_semantic_facts:", result)
        self.assertIn("activity_events_stored:", result)
        self.assertIn("screenshot_events_stored:", result)
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
        self.assertIn("provider", exported["settings"])
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

    def test_provider_save_returns_effective_environment_override(self):
        original = get_llm_config()
        try:
            with patch.dict(os.environ, {"CLIPPY_LLM_PROVIDER": "codex"}):
                effective = save_llm_config({"provider": "ollama"})
                self.assertEqual(effective["provider"], "codex_cli")
                self.assertIn("provider", public_llm_config()["environment_overrides"])
        finally:
            save_llm_config(original)

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

    def test_subscription_provider_configs_use_local_embeddings(self):
        for provider, chat_model, vision_model in (
            ("codex", "default", "default"),
            ("claude", "sonnet", "sonnet"),
        ):
            config = normalize_config({"provider": provider})
            self.assertEqual(config["provider"], f"{provider}_cli")
            self.assertEqual(config["base_url"], "cli://local")
            self.assertEqual(config["chat_model"], chat_model)
            self.assertEqual(config["vision_model"], vision_model)
            self.assertEqual(config["embedding_model"], "local:sentence-transformers/all-MiniLM-L6-v2")

    def test_legacy_gemini_cli_config_migrates_to_supported_api(self):
        config = normalize_config({
            "provider": "gemini_cli",
            "base_url": "cli://local",
            "chat_model": "auto",
            "vision_model": "auto",
        })
        self.assertEqual(config["provider"], "gemini_api")
        self.assertEqual(config["base_url"], "https://generativelanguage.googleapis.com/v1beta/openai")
        self.assertEqual(config["chat_model"], "gemini-2.5-flash")
        self.assertEqual(config["vision_model"], "gemini-2.5-flash")
        self.assertEqual(config["embedding_model"], "local:sentence-transformers/all-MiniLM-L6-v2")

        fresh = normalize_config({"provider": "gemini_api"})
        self.assertEqual(fresh["base_url"], "https://generativelanguage.googleapis.com/v1beta/openai")
        self.assertEqual(fresh["chat_model"], "gemini-2.5-flash")

    def test_subscription_cli_output_parsers_keep_final_answers(self):
        claude = _extract_output("claude_cli", '{"type":"result","result":"Claude answer"}')
        codex = _extract_output(
            "codex_cli",
            '\n'.join([
                '{"type":"thread.started","thread_id":"test"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"Codex answer"}}',
            ]),
        )
        self.assertEqual(claude, "Claude answer")
        self.assertEqual(codex, "Codex answer")

    def test_subscription_cli_commands_are_read_only_and_support_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = _build_command(
                "codex_cli",
                ["codex"],
                "default",
                root,
                [root / "frame.jpg"],
                None,
            )
        self.assertIn("--sandbox", command)
        self.assertIn("read-only", command)
        self.assertIn("--image", command)

    def test_gateway_routes_subscription_chat_to_cli_adapter(self):
        config = normalize_config({"provider": "claude"})
        expected = {"message": {"role": "assistant", "content": "ok"}}
        with patch("core.llm_gateway.get_llm_config", return_value=config), patch(
            "core.llm_gateway.run_cli_chat", return_value=expected
        ) as run_cli:
            result = gateway.chat([{"role": "user", "content": "hello"}], "sonnet")
        self.assertEqual(result, expected)
        run_cli.assert_called_once()

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

    def test_vision_updates_only_pending_vision_events(self):
        event = make_event("vision-race")
        store_event(event)
        conn.execute("UPDATE events SET classification_status='awaiting_vision' WHERE event_id=?", (event["event_id"],))
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

    def test_phash_bursts_do_not_chain_past_the_time_window(self):
        paths = [Path("1000.jpg"), Path("21000.jpg"), Path("41000.jpg")]
        digest = imagehash.hex_to_hash("0" * 16)
        groups = _group_by_similarity(paths, {path.stem: digest for path in paths})
        self.assertEqual(sorted(len(group) for group in groups), [1, 2])

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
            result = rag.search_event_rag("keyword fallback phrase")
        finally:
            rag.embed_text = original_embed_text
            rag.embed_texts = original_embed_texts
        self.assertIsNotNone(result)
        rows, total = result
        self.assertGreaterEqual(total, 1)
        self.assertIn("keyword-rag", "\n".join(rows))

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


if __name__ == "__main__":
    unittest.main()
