from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import ollama_client


class OllamaBaseUrlResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="clippy-ollama-url-")
        self.data_dir = Path(self._tmpdir.name)
        self._env = patch.dict(
            os.environ,
            {
                "CLIPPY_DATA_DIR": str(self.data_dir),
                "CLIPPY_OLLAMA_URL": "",
                "OLLAMA_HOST": "",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmpdir.cleanup()

    def _write_llm_config(self, **values):
        payload = {"provider": "ollama", "chat_model": "qwen3:8b", **values}
        (self.data_dir / "llm_config.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    def test_clippy_ollama_url_wins_over_host_and_config(self):
        self._write_llm_config(base_url="http://10.0.0.2:11434")
        with patch.dict(
            os.environ,
            {
                "CLIPPY_OLLAMA_URL": "http://192.168.1.5:11434",
                "OLLAMA_HOST": "10.0.0.8:11434",
            },
            clear=False,
        ):
            self.assertEqual(ollama_client.base_url(), "http://192.168.1.5:11434")

    def test_ollama_host_wins_over_llm_config(self):
        self._write_llm_config(base_url="http://10.0.0.2:11434")
        with patch.dict(
            os.environ,
            {"CLIPPY_OLLAMA_URL": "", "OLLAMA_HOST": "http://10.0.0.8:11434"},
            clear=False,
        ):
            self.assertEqual(ollama_client.base_url(), "http://10.0.0.8:11434")

    def test_ollama_host_wins_when_llm_config_is_default(self):
        self._write_llm_config(base_url="http://127.0.0.1:11434")
        with patch.dict(
            os.environ,
            {"CLIPPY_OLLAMA_URL": "", "OLLAMA_HOST": "http://10.0.0.8:11434"},
            clear=False,
        ):
            self.assertEqual(ollama_client.base_url(), "http://10.0.0.8:11434")

    def test_llm_config_base_url_used_when_env_unset(self):
        self._write_llm_config(base_url="http://192.168.1.5:11434")
        self.assertEqual(ollama_client.base_url(), "http://192.168.1.5:11434")

    def test_default_when_nothing_configured(self):
        self.assertEqual(ollama_client.base_url(), ollama_client.DEFAULT_BASE_URL)

    def test_missing_or_invalid_llm_config_falls_back_to_default(self):
        (self.data_dir / "llm_config.json").write_text("{not-json", encoding="utf-8")
        self.assertEqual(ollama_client.base_url(), ollama_client.DEFAULT_BASE_URL)

    def test_wildcard_host_from_llm_config_is_normalized(self):
        self._write_llm_config(base_url="http://0.0.0.0:11434")
        self.assertEqual(ollama_client.base_url(), "http://127.0.0.1:11434")


if __name__ == "__main__":
    unittest.main()
