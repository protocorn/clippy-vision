from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.diagnostics import get_diagnostics
from core.local_embeddings import (
    HASH_MODEL_ID,
    MODEL_ID,
    EmbeddingUnavailable,
    embed_texts,
    embedding_status,
    hash_fallback_allowed,
    reset_runtime_state_for_tests,
)
from core.model_download import (
    EMBEDDING_MODEL_RELATIVE,
    EMBEDDING_SNAPSHOT_ALLOW_PATTERNS,
    _snapshot,
    _transfer_model,
    embedding_ready,
    ensure_embedding_model,
    migrate_legacy_embedding_model,
    resolve_embedding_model_dir,
)


def _write_dummy_minilm(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text('{"hidden_size": 384}\n', encoding="utf-8")
    (path / "model.safetensors").write_bytes(b"fake-weights")
    (path / "tokenizer.json").write_text('{"version": "1.0"}\n', encoding="utf-8")
    return path


def _write_allowed_minilm(path: Path) -> Path:
    """Write only the files MiniLM snapshot_download is allowed to fetch."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text('{"hidden_size": 384}\n', encoding="utf-8")
    (path / "config_sentence_transformers.json").write_text("{}\n", encoding="utf-8")
    (path / "tokenizer.json").write_text('{"version": "1.0"}\n', encoding="utf-8")
    (path / "tokenizer_config.json").write_text("{}\n", encoding="utf-8")
    (path / "vocab.txt").write_text("[CLS]\n", encoding="utf-8")
    (path / "special_tokens_map.json").write_text("{}\n", encoding="utf-8")
    (path / "modules.json").write_text("[]\n", encoding="utf-8")
    (path / "sentence_bert_config.json").write_text("{}\n", encoding="utf-8")
    (path / "1_Pooling").mkdir(exist_ok=True)
    (path / "1_Pooling" / "config.json").write_text("{}\n", encoding="utf-8")
    (path / "model.safetensors").write_bytes(b"fake-weights")
    return path


class EmbeddingModelDirTests(unittest.TestCase):
    def test_override_wins(self):
        dest = Path("/custom/minilm")
        resolved = resolve_embedding_model_dir(
            override=str(dest),
            data_dir="/home/ada/.config/Clippy Vision/data",
            project_root=Path("/tmp/.mount_ClippyABC/usr/lib/clippy"),
        )
        self.assertEqual(resolved, dest)

    def test_packaged_data_dir_uses_user_data_models(self):
        user_data = Path("/home/ada/.config/Clippy Vision")
        resolved = resolve_embedding_model_dir(
            data_dir=str(user_data / "data"),
            project_root=Path("/home/ada/.config/Clippy Vision/mcp-runtime-abcd"),
        )
        self.assertEqual(resolved, user_data / EMBEDDING_MODEL_RELATIVE)

    def test_source_tree_data_dir_keeps_repo_models(self):
        with tempfile.TemporaryDirectory(prefix="clippy-src-") as raw:
            repo = Path(raw)
            (repo / "mcp_server.py").write_text("# server\n", encoding="utf-8")
            (repo / "core").mkdir()
            resolved = resolve_embedding_model_dir(
                data_dir=str(repo / "core" / "data"),
                project_root=repo,
            )
            self.assertEqual(resolved, repo / EMBEDDING_MODEL_RELATIVE)

    def test_ephemeral_code_root_uses_platform_user_data(self):
        resolved = resolve_embedding_model_dir(
            project_root=Path("/tmp/.mount_ClippyXYZ/usr/lib/clippy-vision/resources/clippy"),
            platform="linux",
            home=Path("/home/ada"),
            env={},
        )
        self.assertEqual(
            resolved,
            Path("/home/ada/.config/Clippy Vision") / EMBEDDING_MODEL_RELATIVE,
        )

    def test_ephemeral_code_root_honors_xdg_and_appdata(self):
        linux = resolve_embedding_model_dir(
            project_root=Path("/tmp/.mount_ClippyXYZ/resources/clippy"),
            platform="linux",
            home=Path("/home/ada"),
            env={"XDG_CONFIG_HOME": "/home/ada/.xdg"},
        )
        windows = resolve_embedding_model_dir(
            project_root=Path(r"C:\Users\ada\AppData\Roaming\Clippy Vision\mcp-runtime-abcd"),
            platform="win32",
            home=Path(r"C:\Users\ada"),
            env={"APPDATA": r"C:\Users\ada\AppData\Roaming"},
        )
        mac = resolve_embedding_model_dir(
            project_root=Path("/tmp/.mount_ClippyXYZ/resources/clippy"),
            platform="darwin",
            home=Path("/Users/ada"),
            env={},
        )
        self.assertEqual(linux, Path("/home/ada/.xdg/Clippy Vision") / EMBEDDING_MODEL_RELATIVE)
        self.assertEqual(
            windows,
            Path(r"C:\Users\ada\AppData\Roaming") / "Clippy Vision" / EMBEDDING_MODEL_RELATIVE,
        )
        self.assertEqual(
            mac,
            Path("/Users/ada/Library/Application Support/Clippy Vision") / EMBEDDING_MODEL_RELATIVE,
        )

    def test_pytest_style_data_dir_does_not_steal_repo_models(self):
        with tempfile.TemporaryDirectory(prefix="clippy-tests-") as raw:
            repo = Path(raw) / "repo"
            repo.mkdir()
            (repo / "mcp_server.py").write_text("# server\n", encoding="utf-8")
            isolated = Path(raw) / "clippy-tests-abcd"
            isolated.mkdir()
            resolved = resolve_embedding_model_dir(
                data_dir=str(isolated),
                project_root=repo,
            )
            self.assertEqual(resolved, repo / EMBEDDING_MODEL_RELATIVE)


class EmbeddingModelMigrationTests(unittest.TestCase):
    def test_moves_mcp_runtime_copy_once(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-mig-") as raw:
            user_data = Path(raw)
            runtime = user_data / "mcp-runtime-oldhash"
            src = _write_dummy_minilm(runtime / EMBEDDING_MODEL_RELATIVE)
            dest = user_data / EMBEDDING_MODEL_RELATIVE
            marker = src / "from-runtime.marker"
            marker.write_text("keep\n", encoding="utf-8")

            migrated = migrate_legacy_embedding_model(dest, project_root=runtime)
            self.assertEqual(migrated, dest)
            self.assertTrue(embedding_ready(dest))
            self.assertEqual((dest / "from-runtime.marker").read_text(encoding="utf-8"), "keep\n")
            self.assertFalse(src.exists())

            (dest / "from-runtime.marker").write_text("already-stable\n", encoding="utf-8")
            leftover = _write_dummy_minilm(user_data / "mcp-runtime-newer" / EMBEDDING_MODEL_RELATIVE)
            (leftover / "from-runtime.marker").write_text("should-not-replace\n", encoding="utf-8")
            again = migrate_legacy_embedding_model(dest, project_root=runtime)
            self.assertEqual(again, dest)
            self.assertEqual((dest / "from-runtime.marker").read_text(encoding="utf-8"), "already-stable\n")
            self.assertTrue(leftover.exists())

    def test_copies_source_tree_model_without_removing_it(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-copy-") as raw:
            repo = Path(raw) / "repo"
            user_data = Path(raw) / "Clippy Vision"
            src = _write_dummy_minilm(repo / EMBEDDING_MODEL_RELATIVE)
            dest = user_data / EMBEDDING_MODEL_RELATIVE

            migrated = migrate_legacy_embedding_model(dest, project_root=repo)
            self.assertEqual(migrated, dest)
            self.assertTrue(embedding_ready(dest))
            self.assertTrue(embedding_ready(src))

    def test_interrupted_copy_leaves_no_complete_looking_destination(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-interrupt-") as raw:
            src = _write_dummy_minilm(Path(raw) / "src")
            dest = Path(raw) / "all-MiniLM-L6-v2"

            def partial_copy(source, target, **_kwargs):
                target = Path(target)
                target.mkdir(parents=True)
                (target / "config.json").write_text("{}", encoding="utf-8")
                raise OSError("disk full")

            with patch("core.model_download.shutil.copytree", side_effect=partial_copy):
                with self.assertRaises(OSError):
                    _transfer_model(src, dest, prefer_move=False)

            self.assertFalse(embedding_ready(dest))
            self.assertFalse(dest.exists() or (dest.is_dir() and any(dest.iterdir())))
            leftovers = list(dest.parent.glob(f"{dest.name}.tmp-*"))
            self.assertEqual(leftovers, [])

    def test_empty_destination_is_refilled_atomically(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-empty-") as raw:
            repo = Path(raw) / "repo"
            dest = Path(raw) / "Clippy Vision" / EMBEDDING_MODEL_RELATIVE
            dest.mkdir(parents=True)
            (dest / "config.json").write_text("", encoding="utf-8")
            src = _write_dummy_minilm(repo / EMBEDDING_MODEL_RELATIVE)

            migrated = migrate_legacy_embedding_model(dest, project_root=repo)
            self.assertEqual(migrated, dest)
            self.assertTrue(embedding_ready(dest))
            self.assertTrue((dest / "tokenizer.json").is_file())
            self.assertEqual(list(dest.parent.glob(f"{dest.name}.tmp-*")), [])

    def test_failed_download_leaves_no_empty_folder(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-dl-") as raw:
            dest = Path(raw) / "all-MiniLM-L6-v2"
            dest.mkdir()
            (dest / "orphan").write_text("stale\n", encoding="utf-8")

            def fail_snapshot(_repo, local_dir):
                raise OSError("offline")

            with patch("core.model_download.embedding_model_dir", return_value=dest), \
                 patch("core.model_download._snapshot", side_effect=fail_snapshot), \
                 patch(
                     "core.model_download._migrate_legacy_embedding_model_unlocked",
                     return_value=dest,
                 ):
                with self.assertRaises(OSError):
                    ensure_embedding_model()

            self.assertFalse(dest.exists())
            self.assertEqual(list(Path(raw).glob(f"{dest.name}.tmp-*")), [])


class EmbeddingSnapshotAllowPatternsTests(unittest.TestCase):
    EXPECTED_ALLOW_PATTERNS = [
        "config*.json",
        "tokenizer*",
        "vocab.txt",
        "special_tokens_map.json",
        "modules.json",
        "sentence_bert_config.json",
        "1_Pooling/*",
        "model.safetensors",
    ]

    def test_allow_patterns_keep_safetensors_and_skip_other_weight_formats(self):
        self.assertEqual(list(EMBEDDING_SNAPSHOT_ALLOW_PATTERNS), self.EXPECTED_ALLOW_PATTERNS)
        self.assertIn("model.safetensors", EMBEDDING_SNAPSHOT_ALLOW_PATTERNS)
        self.assertNotIn("pytorch_model.bin", EMBEDDING_SNAPSHOT_ALLOW_PATTERNS)
        self.assertNotIn("*.bin", EMBEDDING_SNAPSHOT_ALLOW_PATTERNS)
        self.assertFalse(any("onnx" in pattern for pattern in EMBEDDING_SNAPSHOT_ALLOW_PATTERNS))
        self.assertFalse(any("openvino" in pattern for pattern in EMBEDDING_SNAPSHOT_ALLOW_PATTERNS))
        self.assertFalse(any("tf_model" in pattern or "*.h5" in pattern for pattern in EMBEDDING_SNAPSHOT_ALLOW_PATTERNS))
        self.assertFalse(any("rust_model" in pattern or "*.ot" in pattern for pattern in EMBEDDING_SNAPSHOT_ALLOW_PATTERNS))

    def test_folder_with_only_allowed_files_passes_completeness_check(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-allowed-") as raw:
            path = _write_allowed_minilm(Path(raw) / "all-MiniLM-L6-v2")
            self.assertTrue(embedding_ready(path))
            self.assertFalse((path / "pytorch_model.bin").exists())
            self.assertFalse((path / "onnx").exists())
            self.assertFalse((path / "openvino").exists())
            self.assertFalse((path / "tf_model.h5").exists())
            self.assertFalse((path / "rust_model.ot").exists())

    def test_snapshot_download_is_called_with_allow_patterns(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-snapshot-") as raw:
            dest = Path(raw) / "all-MiniLM-L6-v2"

            def fake_snapshot_download(*, repo_id, local_dir, allow_patterns=None, **_kwargs):
                _write_allowed_minilm(Path(local_dir))
                return str(local_dir)

            with patch("huggingface_hub.snapshot_download", side_effect=fake_snapshot_download) as mock_download:
                result = _snapshot("sentence-transformers/all-MiniLM-L6-v2", dest)

            mock_download.assert_called_once()
            kwargs = mock_download.call_args.kwargs
            self.assertEqual(kwargs["allow_patterns"], self.EXPECTED_ALLOW_PATTERNS)
            self.assertNotIn("pytorch_model.bin", kwargs["allow_patterns"])
            self.assertEqual(result, dest)
            self.assertTrue(embedding_ready(dest))
            self.assertTrue((dest / "model.safetensors").is_file())
            self.assertTrue((dest / "tokenizer.json").is_file())
            self.assertTrue((dest / "1_Pooling" / "config.json").is_file())
            self.assertFalse((dest / "pytorch_model.bin").exists())
            self.assertEqual(list(dest.parent.glob(f"{dest.name}.tmp-*")), [])


class HashFallbackGuardTests(unittest.TestCase):
    def tearDown(self):
        reset_runtime_state_for_tests()

    def test_refuses_hash_when_stored_model_is_minilm(self):
        reset_runtime_state_for_tests()
        with patch("core.local_embeddings._load_bundle", return_value=None), \
             patch("core.local_embeddings._stored_embedding_model_id", return_value=MODEL_ID), \
             patch("core.local_embeddings._ensure_storage_model") as ensure, \
             patch("core.local_embeddings._hash_embedding") as hash_embed, \
             patch("core.local_embeddings.model_dir", return_value=Path("/tmp/missing-minilm")), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            import core.local_embeddings as local_embeddings

            local_embeddings._load_error = RuntimeError("offline")
            with self.assertRaises(EmbeddingUnavailable) as ctx:
                embed_texts(["favorite editor"])
        message = str(ctx.exception)
        self.assertIn("MiniLM embeddings are required", message)
        self.assertIn(MODEL_ID, message)
        self.assertIn("unavailable", message)
        self.assertIn("ERROR", stdout.getvalue())
        self.assertIn("refusing hash fallback", stdout.getvalue())
        ensure.assert_not_called()
        hash_embed.assert_not_called()

    def test_hash_allowed_when_storage_is_already_hash(self):
        self.assertTrue(hash_fallback_allowed(HASH_MODEL_ID))
        self.assertFalse(hash_fallback_allowed(MODEL_ID))
        with patch("core.local_embeddings._stored_embedding_model_id", return_value=None), \
             patch("core.local_embeddings._storage_has_vectors", return_value=False):
            self.assertTrue(hash_fallback_allowed(None))
        with patch("core.local_embeddings._stored_embedding_model_id", return_value=None), \
             patch("core.local_embeddings._storage_has_vectors", return_value=True):
            self.assertFalse(hash_fallback_allowed(None))

    def test_hash_fallback_warns_when_no_minilm_vectors_exist(self):
        reset_runtime_state_for_tests()
        fake = [0.0] * 384
        with patch("core.local_embeddings._load_bundle", return_value=None), \
             patch("core.local_embeddings._stored_embedding_model_id", return_value=None), \
             patch("core.local_embeddings._storage_has_vectors", return_value=False), \
             patch("core.local_embeddings._ensure_storage_model") as ensure, \
             patch("core.local_embeddings._hash_embedding", return_value=fake) as hash_embed, \
             patch("core.local_embeddings.model_dir", return_value=Path("/tmp/missing-minilm")), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            import core.local_embeddings as local_embeddings

            local_embeddings._load_error = RuntimeError("offline")
            vectors = embed_texts(["favorite editor"])
        self.assertEqual(vectors, [fake])
        hash_embed.assert_called_once()
        ensure.assert_called_once_with(HASH_MODEL_ID, None)
        self.assertIn("WARNING", stdout.getvalue())
        self.assertIn("no MiniLM vectors are stored", stdout.getvalue())


class EmbeddingStatusDiagnosticsTests(unittest.TestCase):
    def tearDown(self):
        reset_runtime_state_for_tests()

    def test_embedding_status_flags_missing_folder_without_loading(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-status-") as raw:
            missing = Path(raw) / "missing-minilm"
            with patch("core.local_embeddings.model_dir", return_value=missing), \
                 patch("core.local_embeddings.hash_fallback_allowed", return_value=False):
                reset_runtime_state_for_tests()
                status = embedding_status()
        self.assertFalse(status["bundled"])
        self.assertFalse(status["ready"])
        self.assertFalse(status["loaded"])
        self.assertIsNotNone(status["error"])
        self.assertIn("missing or incomplete", status["error"])

    def test_diagnostics_include_missing_model_error(self):
        status = {
            "provider": "local",
            "model": MODEL_ID,
            "dimension": 384,
            "model_path": "/tmp/missing-minilm",
            "bundled": False,
            "ready": False,
            "loaded": False,
            "error": "MiniLM model is missing or incomplete at /tmp/missing-minilm",
        }
        with patch("core.diagnostics.embedding_status", return_value=status), \
             patch("core.diagnostics.get_capture_status", return_value={}), \
             patch("core.diagnostics.get_capture_settings", return_value={}), \
             patch("core.diagnostics.get_data_stats", return_value={}), \
             patch("core.diagnostics.get_performance_snapshot", return_value={}), \
             patch("core.diagnostics._package_status", return_value=[]), \
             patch("core.diagnostics._permission_status", return_value={}):
            data = get_diagnostics()
        self.assertEqual(data["embeddings"]["error"], status["error"])
        self.assertFalse(data["embeddings"]["bundled"])


if __name__ == "__main__":
    unittest.main()
