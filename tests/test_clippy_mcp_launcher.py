from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class ClippyMcpLauncherEnvTests(unittest.TestCase):
    def _probe_env(self, *, data_dir: str | None, extra_env: dict[str, str] | None = None) -> dict:
        with tempfile.TemporaryDirectory(prefix="clippy-mcp-launcher-") as raw:
            root = Path(raw)
            (root / "scripts").mkdir()
            if os.name == "nt":
                launcher_name = "clippy-mcp.cmd"
            else:
                launcher_name = "clippy-mcp"
            shutil.copy(REPO_ROOT / "scripts" / launcher_name, root / "scripts" / launcher_name)
            if os.name != "nt":
                launcher = root / "scripts" / launcher_name
                launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)

            (root / "mcp_server.py").write_text(
                "import json, os\n"
                "print(json.dumps({\n"
                "    'CLIPPY_DATA_DIR': os.environ.get('CLIPPY_DATA_DIR'),\n"
                "    'CLIPPY_EMBEDDING_MODEL_DIR': os.environ.get('CLIPPY_EMBEDDING_MODEL_DIR'),\n"
                "    'CLIPPY_OLLAMA_URL': os.environ.get('CLIPPY_OLLAMA_URL'),\n"
                "}))\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.pop("CLIPPY_EMBEDDING_MODEL_DIR", None)
            env.pop("CLIPPY_OLLAMA_URL", None)
            env.pop("OLLAMA_HOST", None)
            env.pop("CLIPPY_DATA_DIR", None)
            env["HOME"] = str(root)
            env["USERPROFILE"] = str(root)
            env["APPDATA"] = str(root / "AppData" / "Roaming")
            env["XDG_CONFIG_HOME"] = str(root / ".config")
            env["CLIPPY_PYTHON"] = sys.executable
            if data_dir is not None:
                env["CLIPPY_DATA_DIR"] = data_dir
            if extra_env:
                env.update(extra_env)

            result = subprocess.run(
                [str(root / "scripts" / launcher_name)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            return json.loads(result.stdout)

    def test_source_tree_data_dir_keeps_repo_models(self):
        with tempfile.TemporaryDirectory(prefix="clippy-mcp-tree-") as raw:
            root = Path(raw)
            (root / "scripts").mkdir()
            (root / "core" / "data").mkdir(parents=True)
            if os.name == "nt":
                launcher_name = "clippy-mcp.cmd"
            else:
                launcher_name = "clippy-mcp"
            shutil.copy(REPO_ROOT / "scripts" / launcher_name, root / "scripts" / launcher_name)
            if os.name != "nt":
                launcher = root / "scripts" / launcher_name
                launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
            (root / "mcp_server.py").write_text(
                "import json, os\n"
                "print(json.dumps({\n"
                "    'CLIPPY_DATA_DIR': os.environ.get('CLIPPY_DATA_DIR'),\n"
                "    'CLIPPY_EMBEDDING_MODEL_DIR': os.environ.get('CLIPPY_EMBEDDING_MODEL_DIR'),\n"
                "    'CLIPPY_OLLAMA_URL': os.environ.get('CLIPPY_OLLAMA_URL'),\n"
                "}))\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.pop("CLIPPY_EMBEDDING_MODEL_DIR", None)
            env.pop("CLIPPY_OLLAMA_URL", None)
            env.pop("OLLAMA_HOST", None)
            env.pop("CLIPPY_DATA_DIR", None)
            env["HOME"] = str(root)
            env["USERPROFILE"] = str(root)
            env["APPDATA"] = str(root / "AppData" / "Roaming")
            env["XDG_CONFIG_HOME"] = str(root / ".config")
            env["CLIPPY_PYTHON"] = sys.executable
            result = subprocess.run(
                [str(root / "scripts" / launcher_name)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            payload = json.loads(result.stdout)
            expected = root / "models" / "embeddings" / "all-MiniLM-L6-v2"
            self.assertEqual(Path(payload["CLIPPY_EMBEDDING_MODEL_DIR"]), expected)
            self.assertEqual(Path(payload["CLIPPY_DATA_DIR"]), root / "core" / "data")
            self.assertEqual(payload["CLIPPY_OLLAMA_URL"], "http://127.0.0.1:11434")

    def test_user_data_data_dir_sets_sibling_models(self):
        with tempfile.TemporaryDirectory(prefix="clippy-user-") as raw:
            user_data = Path(raw) / "Clippy Vision"
            data_dir = user_data / "data"
            data_dir.mkdir(parents=True)
            payload = self._probe_env(data_dir=str(data_dir))
            self.assertEqual(
                Path(payload["CLIPPY_EMBEDDING_MODEL_DIR"]),
                user_data / "models" / "embeddings" / "all-MiniLM-L6-v2",
            )
            self.assertEqual(Path(payload["CLIPPY_DATA_DIR"]), data_dir)

    def test_override_is_preserved(self):
        override = Path(tempfile.mkdtemp(prefix="clippy-embed-override-")) / "custom-minilm"
        try:
            payload = self._probe_env(
                data_dir=str(Path(tempfile.gettempdir()) / "Clippy Vision" / "data"),
                extra_env={"CLIPPY_EMBEDDING_MODEL_DIR": str(override)},
            )
            self.assertEqual(Path(payload["CLIPPY_EMBEDDING_MODEL_DIR"]), override)
        finally:
            shutil.rmtree(override.parent, ignore_errors=True)

    def test_llm_config_base_url_is_exported(self):
        with tempfile.TemporaryDirectory(prefix="clippy-ollama-cfg-") as raw:
            data_dir = Path(raw) / "Clippy Vision" / "data"
            data_dir.mkdir(parents=True)
            (data_dir / "llm_config.json").write_text(
                json.dumps({"provider": "ollama", "base_url": "http://192.168.1.5:11434"}),
                encoding="utf-8",
            )
            payload = self._probe_env(data_dir=str(data_dir))
            self.assertEqual(payload["CLIPPY_OLLAMA_URL"], "http://192.168.1.5:11434")

    def test_clippy_ollama_url_override_is_preserved(self):
        payload = self._probe_env(
            data_dir=str(Path(tempfile.gettempdir()) / "Clippy Vision" / "data"),
            extra_env={"CLIPPY_OLLAMA_URL": "http://10.0.0.8:11434"},
        )
        self.assertEqual(payload["CLIPPY_OLLAMA_URL"], "http://10.0.0.8:11434")

    def test_ollama_host_is_not_shadowed(self):
        payload = self._probe_env(
            data_dir=str(Path(tempfile.gettempdir()) / "Clippy Vision" / "data"),
            extra_env={"OLLAMA_HOST": "192.168.1.9:11434"},
        )
        self.assertIsNone(payload["CLIPPY_OLLAMA_URL"])


if __name__ == "__main__":
    unittest.main()
