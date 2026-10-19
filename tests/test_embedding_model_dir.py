from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.diagnostics import get_diagnostics
from core.local_embeddings import (
    HASH_MODEL_ID,
    MODEL_ID,
    EmbeddingUnavailable,
    _load_bundle,
    _looks_like_weights_error,
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
    cleanup_stale_embedding_temps,
    discard_managed_embedding_dir,
    embedding_ready,
    ensure_embedding_model,
    is_user_embedding_override,
    managed_embedding_model_dir,
    migrate_legacy_embedding_model,
    resolve_embedding_model_dir,
    safetensors_file_valid,
)


def _minimal_safetensors_bytes(*, data: bytes | None = None) -> bytes:
    payload = data if data is not None else b"\x00" * 16
    header = {
        "weight": {
            "dtype": "F32",
            "shape": [max(len(payload) // 4, 1)],
            "data_offsets": [0, len(payload)],
        }
    }
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return len(header_bytes).to_bytes(8, "little") + header_bytes + payload


def _write_dummy_minilm(path: Path, *, include_bin: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text('{"hidden_size": 384}\n', encoding="utf-8")
    (path / "model.safetensors").write_bytes(_minimal_safetensors_bytes())
    (path / "tokenizer.json").write_text('{"version": "1.0"}\n', encoding="utf-8")
    if include_bin:
        (path / "pytorch_model.bin").write_bytes(b"legacy-bin-weights")
    return path


def _write_broken_minilm(path: Path, *, safetensors: bytes) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text('{"hidden_size": 384}\n', encoding="utf-8")
    (path / "model.safetensors").write_bytes(safetensors)
    (path / "pytorch_model.bin").write_bytes(b"nonempty-bin-weights")
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
    (path / "model.safetensors").write_bytes(_minimal_safetensors_bytes())
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
            self.assertTrue(safetensors_file_valid(path / "model.safetensors"))
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
            self.assertTrue(safetensors_file_valid(dest / "model.safetensors"))


class SafetensorsWeightsValidationTests(unittest.TestCase):
    def test_zero_byte_safetensors_next_to_bin_is_incomplete_and_refilled(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-zero-st-") as raw:
            dest = _write_broken_minilm(Path(raw) / "dest", safetensors=b"")
            self.assertFalse(safetensors_file_valid(dest / "model.safetensors"))
            self.assertFalse(embedding_ready(dest))

            repo = Path(raw) / "repo"
            src = _write_dummy_minilm(repo / EMBEDDING_MODEL_RELATIVE)
            (src / "from-legacy.marker").write_text("ok\n", encoding="utf-8")

            migrated = migrate_legacy_embedding_model(dest, project_root=repo)
            self.assertEqual(migrated, dest)
            self.assertTrue(embedding_ready(dest))
            self.assertTrue(safetensors_file_valid(dest / "model.safetensors"))
            self.assertEqual((dest / "from-legacy.marker").read_text(encoding="utf-8"), "ok\n")

    def test_truncated_safetensors_next_to_bin_is_incomplete_and_refilled(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-trunc-st-") as raw:
            dest = _write_broken_minilm(Path(raw) / "dest", safetensors=b"\x01\x00\x00")
            self.assertFalse(safetensors_file_valid(dest / "model.safetensors"))
            self.assertFalse(embedding_ready(dest))

            repo = Path(raw) / "repo"
            src = _write_dummy_minilm(repo / EMBEDDING_MODEL_RELATIVE)
            (src / "from-legacy.marker").write_text("ok\n", encoding="utf-8")

            migrated = migrate_legacy_embedding_model(dest, project_root=repo)
            self.assertEqual(migrated, dest)
            self.assertTrue(embedding_ready(dest))
            self.assertTrue(safetensors_file_valid(dest / "model.safetensors"))
            self.assertEqual((dest / "from-legacy.marker").read_text(encoding="utf-8"), "ok\n")

    def test_valid_safetensors_only_is_complete(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-st-only-") as raw:
            dest = _write_dummy_minilm(Path(raw) / "dest")
            self.assertFalse((dest / "pytorch_model.bin").exists())
            self.assertTrue(safetensors_file_valid(dest / "model.safetensors"))
            self.assertTrue(embedding_ready(dest))

            repo = Path(raw) / "repo"
            leftover = _write_dummy_minilm(repo / EMBEDDING_MODEL_RELATIVE)
            (leftover / "should-not-replace").write_text("no\n", encoding="utf-8")
            (dest / "keep.marker").write_text("stable\n", encoding="utf-8")

            migrated = migrate_legacy_embedding_model(dest, project_root=repo)
            self.assertEqual(migrated, dest)
            self.assertTrue(embedding_ready(dest))
            self.assertEqual((dest / "keep.marker").read_text(encoding="utf-8"), "stable\n")
            self.assertFalse((dest / "should-not-replace").exists())

    def test_safetensors_with_offsets_past_eof_is_invalid(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-st-eof-") as raw:
            path = Path(raw) / "model.safetensors"
            header = {
                "weight": {
                    "dtype": "F32",
                    "shape": [4],
                    "data_offsets": [0, 64],
                }
            }
            header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
            path.write_bytes(len(header_bytes).to_bytes(8, "little") + header_bytes + b"\x00" * 4)
            self.assertFalse(safetensors_file_valid(path))


class EmbeddingOverrideProtectionTests(unittest.TestCase):
    def tearDown(self):
        reset_runtime_state_for_tests()

    def test_env_matching_default_is_app_managed(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-default-env-") as raw:
            root = Path(raw)
            data_dir = root / "Clippy Vision" / "data"
            data_dir.mkdir(parents=True)
            default = managed_embedding_model_dir(data_dir=str(data_dir), project_root=root / "repo")
            self.assertFalse(
                is_user_embedding_override(
                    override=str(default),
                    data_dir=str(data_dir),
                    project_root=root / "repo",
                )
            )
            self.assertTrue(
                is_user_embedding_override(
                    override=str(root / "picked-by-user"),
                    data_dir=str(data_dir),
                    project_root=root / "repo",
                )
            )

    def test_broken_override_is_reported_as_error_and_left_untouched(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-override-") as raw:
            root = Path(raw)
            override = root / "user-picked" / "minilm"
            _write_broken_minilm(override, safetensors=b"")
            marker = override / "user-marker"
            marker.write_text("leave-me\n", encoding="utf-8")
            repo = root / "repo"
            repo.mkdir()
            (repo / "mcp_server.py").write_text("# server\n", encoding="utf-8")
            _write_dummy_minilm(repo / EMBEDDING_MODEL_RELATIVE)
            data_dir = root / "Clippy Vision" / "data"
            data_dir.mkdir(parents=True)
            stale_tmp = override.with_name(f"{override.name}.tmp-12345")
            stale_tmp.mkdir()
            (stale_tmp / "keep").write_text("x\n", encoding="utf-8")

            env = {
                "CLIPPY_EMBEDDING_MODEL_DIR": str(override),
                "CLIPPY_DATA_DIR": str(data_dir),
            }
            with patch.dict(os.environ, env, clear=False):
                self.assertTrue(is_user_embedding_override())
                self.assertFalse(embedding_ready(override))

                with self.assertRaises(FileNotFoundError) as ctx:
                    ensure_embedding_model()
                self.assertIn("CLIPPY_EMBEDDING_MODEL_DIR", str(ctx.exception))
                self.assertIn("incomplete", str(ctx.exception))

                migrate_legacy_embedding_model(override, project_root=repo)
                cleanup_stale_embedding_temps(override)
                self.assertFalse(discard_managed_embedding_dir(override))

                reset_runtime_state_for_tests()
                status = embedding_status()

                with patch("core.local_embeddings._stored_embedding_model_id", return_value=MODEL_ID), \
                     patch("core.local_embeddings._ensure_storage_model") as ensure_storage, \
                     patch("core.local_embeddings._hash_embedding") as hash_embed:
                    with self.assertRaises(EmbeddingUnavailable) as embed_ctx:
                        embed_texts(["favorite editor"])
                    ensure_storage.assert_not_called()
                    hash_embed.assert_not_called()

            self.assertFalse(status["ready"])
            self.assertFalse(status["bundled"])
            self.assertIn("CLIPPY_EMBEDDING_MODEL_DIR", status["error"])
            self.assertIn("will not modify", status["error"])
            self.assertIn("CLIPPY_EMBEDDING_MODEL_DIR", str(embed_ctx.exception))
            self.assertEqual(marker.read_text(encoding="utf-8"), "leave-me\n")
            self.assertEqual((override / "model.safetensors").read_bytes(), b"")
            self.assertTrue((override / "pytorch_model.bin").is_file())
            self.assertTrue(stale_tmp.is_dir())
            self.assertTrue((stale_tmp / "keep").is_file())
            self.assertFalse(embedding_ready(override))


class EmbeddingRefillOnceTests(unittest.TestCase):
    def tearDown(self):
        reset_runtime_state_for_tests()

    def test_weights_error_markers(self):
        self.assertTrue(_looks_like_weights_error(RuntimeError("OSError: incomplete metadata")))
        self.assertTrue(_looks_like_weights_error(OSError("header too small")))
        self.assertTrue(_looks_like_weights_error(ValueError("Unable to load weights")))
        self.assertFalse(_looks_like_weights_error(FileNotFoundError("MiniLM model is missing")))

    def test_load_failure_refills_managed_folder_once(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-refill-") as raw:
            dest = _write_dummy_minilm(Path(raw) / "managed")
            loads: list[Path] = []

            def fail_instantiate(path):
                loads.append(path)
                raise RuntimeError("OSError: incomplete metadata, header too small")

            ensures: list[int] = []
            discards: list[Path] = []

            def fake_ensure(*, force: bool = False):
                ensures.append(1)
                return dest

            def fake_discard(path):
                discards.append(path)
                return True

            reset_runtime_state_for_tests()
            with patch("core.local_embeddings.model_dir", return_value=dest), \
                 patch("core.local_embeddings._instantiate_minilm", side_effect=fail_instantiate), \
                 patch("core.model_download.ensure_embedding_model", side_effect=fake_ensure), \
                 patch("core.model_download.discard_managed_embedding_dir", side_effect=fake_discard), \
                 patch("core.model_download.is_user_embedding_override", return_value=False), \
                 patch("core.model_download.embedding_ready", return_value=True), \
                 patch("sys.stdout", new_callable=io.StringIO):
                first = _load_bundle()
                second = _load_bundle()

            self.assertIsNone(first)
            self.assertIsNone(second)
            self.assertEqual(len(loads), 2)
            self.assertEqual(ensures, [1])
            self.assertEqual(discards, [dest])

    def test_load_failure_on_override_does_not_refill(self):
        with tempfile.TemporaryDirectory(prefix="clippy-embed-override-load-") as raw:
            dest = _write_dummy_minilm(Path(raw) / "user-picked")
            (dest / "user-marker").write_text("leave-me\n", encoding="utf-8")
            loads: list[Path] = []

            def fail_instantiate(path):
                loads.append(path)
                raise RuntimeError("OSError: incomplete metadata, header too small")

            reset_runtime_state_for_tests()
            with patch("core.local_embeddings.model_dir", return_value=dest), \
                 patch("core.local_embeddings._instantiate_minilm", side_effect=fail_instantiate), \
                 patch("core.model_download.ensure_embedding_model") as ensure, \
                 patch("core.model_download.discard_managed_embedding_dir") as discard, \
                 patch("core.model_download.is_user_embedding_override", return_value=True), \
                 patch("core.model_download.embedding_ready", return_value=True), \
                 patch("sys.stdout", new_callable=io.StringIO):
                bundle = _load_bundle()
                again = _load_bundle()

            self.assertIsNone(bundle)
            self.assertIsNone(again)
            self.assertEqual(len(loads), 1)
            ensure.assert_not_called()
            discard.assert_not_called()
            self.assertEqual((dest / "user-marker").read_text(encoding="utf-8"), "leave-me\n")
            self.assertTrue(embedding_ready(dest))


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
