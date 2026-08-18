import json
import os
import tempfile
import threading
import unittest
from itertools import count
from pathlib import Path
from unittest.mock import patch

_TEST_DATA = tempfile.TemporaryDirectory(prefix="clippy-provider-tests-")
os.environ["CLIPPY_DATA_DIR"] = _TEST_DATA.name

from core import cli_providers, llm_config, llm_gateway
from core.llm_gateway import LLMGateway


def tearDownModule():
    _TEST_DATA.cleanup()


class ProviderConfigTests(unittest.TestCase):
    def setUp(self):
        self.config_path = Path(_TEST_DATA.name) / llm_config.CONFIG_FILENAME
        self.config_path.unlink(missing_ok=True)

    def test_subscription_aliases_use_cli_defaults(self):
        codex = llm_config.normalize_config({"provider": "codex"})
        claude = llm_config.normalize_config({"provider": "claude-code"})

        self.assertEqual(codex["provider"], "codex_cli")
        self.assertEqual(codex["chat_model"], "default")
        self.assertEqual(claude["provider"], "claude_cli")
        self.assertEqual(claude["chat_model"], "sonnet")

    def test_embedding_model_cannot_be_redirected(self):
        config = llm_config.normalize_config(
            {
                "provider": "gemini_api",
                "api_key": "secret",
                "embedding_model": "remote-embedding-model",
            }
        )

        self.assertEqual(
            config["embedding_model"],
            "local:sentence-transformers/all-MiniLM-L6-v2",
        )
        self.assertEqual(config["base_url"], llm_config.GEMINI_API_BASE_URL)

    def test_saved_api_key_is_redacted_from_public_config(self):
        saved = llm_config.save_llm_config(
            {
                "provider": "openai_compatible",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "local-secret",
                "chat_model": "chat-model",
                "vision_model": "vision-model",
            }
        )
        public = llm_config.public_llm_config(saved)

        self.assertNotIn("api_key", public)
        self.assertTrue(public["api_key_set"])
        self.assertEqual(json.loads(self.config_path.read_text())["api_key"], "local-secret")

    def test_environment_values_override_saved_config(self):
        llm_config.save_llm_config({"provider": "ollama"})
        with patch.dict(
            os.environ,
            {
                "CLIPPY_LLM_PROVIDER": "codex_cli",
                "CLIPPY_CLI_COMMAND": "/custom/codex",
            },
        ):
            config = llm_config.get_llm_config()

        self.assertEqual(config["provider"], "codex_cli")
        self.assertEqual(config["cli_command"], "/custom/codex")

    def test_provider_switch_does_not_reuse_credentials_or_cli_paths(self):
        llm_config.save_llm_config(
            {
                "provider": "openai_compatible",
                "api_key": "provider-a-secret",
                "chat_model": "provider-a-chat",
                "vision_model": "provider-a-vision",
            }
        )
        switched = llm_config.save_llm_config({"provider": "claude_cli"})

        self.assertEqual(switched["api_key"], "")
        self.assertEqual(switched["cli_command"], "")
        self.assertEqual(switched["chat_model"], "sonnet")
        self.assertEqual(switched["vision_model"], "sonnet")
        if os.name != "nt":
            self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

    def test_environment_api_key_is_never_copied_into_saved_config(self):
        with patch.dict(os.environ, {"CLIPPY_LLM_API_KEY": "environment-secret"}):
            llm_config.save_llm_config({"chat_model": "qwen3:4b"})

        saved = json.loads(self.config_path.read_text())
        self.assertEqual(saved["api_key"], "")


class CliProviderTests(unittest.TestCase):
    def test_codex_command_is_ephemeral_and_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = cli_providers._build_command(
                "codex_cli",
                ["codex"],
                "default",
                root,
                [],
                None,
            )

        self.assertIn("--ephemeral", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--cd") + 1], str(root))
        self.assertEqual(command[-1], "-")

    def test_claude_command_has_no_session_and_plan_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = cli_providers._build_command(
                "claude_cli",
                ["claude"],
                "sonnet",
                Path(temporary),
                [],
                None,
            )

        self.assertIn("--no-session-persistence", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertIn("--tools=", command)

    def test_cli_output_parsers_handle_codex_and_claude_json(self):
        codex = '\n'.join(
            [
                json.dumps({"type": "thread.started"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "codex answer"},
                    }
                ),
            ]
        )
        claude = json.dumps({"type": "result", "result": "claude answer"})

        self.assertEqual(cli_providers._extract_output("codex_cli", codex), "codex answer")
        self.assertEqual(cli_providers._extract_output("claude_cli", claude), "claude answer")

    def test_codex_login_uses_current_subcommand(self):
        self.assertEqual(cli_providers.CLI_PROVIDERS["codex_cli"]["login_args"], ["login"])

    def test_cli_tool_calls_are_normalized_for_the_agent_loop(self):
        content = json.dumps(
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "search_events",
                            "arguments": json.dumps({"query": "project"}),
                        }
                    }
                ]
            }
        )

        calls = cli_providers._tool_calls_from_text(content)

        self.assertEqual(calls[0]["function"]["name"], "search_events")
        self.assertEqual(calls[0]["function"]["arguments"], {"query": "project"})

    def test_cli_status_checks_official_authentication_command(self):
        completed = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "Logged in", "stderr": ""},
        )()
        with (
            patch.object(cli_providers, "command_parts", return_value=["/bin/codex"]),
            patch.object(cli_providers.subprocess, "run", return_value=completed) as run,
        ):
            status = cli_providers.provider_status(
                {"provider": "codex_cli", "chat_model": "default", "vision_model": "default"}
            )

        self.assertTrue(status["ok"])
        self.assertTrue(status["authenticated"])
        self.assertEqual(run.call_args.args[0], ["/bin/codex", "login", "status"])


class GatewayCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _gateway_with_queue(callback):
        instance = object.__new__(LLMGateway)
        instance._seq = count()
        instance._state_lock = threading.Lock()
        instance._current_job = None
        instance._current_priority = None

        class ImmediateQueue:
            def put(self, item):
                callback(item[2])

        instance.queue = ImmediateQueue()
        return instance

    def test_provider_urls_keep_ollama_and_openai_shapes_separate(self):
        ollama = LLMGateway._provider_urls(
            {"provider": "ollama", "base_url": "http://127.0.0.1:11434"}
        )
        compatible = LLMGateway._provider_urls(
            {"provider": "openai_compatible", "base_url": "http://127.0.0.1:1234"}
        )

        self.assertEqual(ollama[0], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(compatible[0], "http://127.0.0.1:1234/v1/chat/completions")

    def test_openai_messages_translate_images_and_tool_arguments(self):
        converted = LLMGateway._openai_messages(
            [
                {"role": "user", "content": "inspect", "images": ["YWJj"]},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "lookup", "arguments": {"q": "x"}}}
                    ],
                },
                {"role": "tool", "content": "done"},
            ]
        )

        self.assertEqual(converted[0]["content"][1]["type"], "image_url")
        call = converted[1]["tool_calls"][0]
        self.assertEqual(json.loads(call["function"]["arguments"]), {"q": "x"})
        self.assertEqual(converted[2]["tool_call_id"], call["id"])

    def test_openai_response_is_normalized_to_ollama_shape(self):
        normalized = LLMGateway._normalize_openai_response(
            {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
        )

        self.assertEqual(normalized, {"message": {"role": "assistant", "content": "hello"}})

    def test_embed_preserves_single_and_batch_return_shapes(self):
        gateway = object.__new__(LLMGateway)
        with (
            patch.object(llm_gateway, "embed_text", return_value=[0.1, 0.2]) as single,
            patch.object(
                llm_gateway,
                "embed_texts",
                return_value=[[0.1, 0.2], [0.3, 0.4]],
            ) as batch,
        ):
            self.assertEqual(gateway.embed("one"), [0.1, 0.2])
            self.assertEqual(
                gateway.embed(["one", "two"]),
                [[0.1, 0.2], [0.3, 0.4]],
            )

        single.assert_called_once_with("one")
        batch.assert_called_once_with(["one", "two"])

    def test_streaming_openai_tool_fragments_are_assembled(self):
        def complete(job):
            job.chunks.put(
                {
                    "message": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "look", "arguments": '{"q":'},
                            }
                        ]
                    }
                }
            )
            job.chunks.put(
                {
                    "message": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"name": "up", "arguments": '"x"}'},
                            }
                        ]
                    }
                }
            )
            job.chunks.put(None)
            job.event.set()

        gateway = self._gateway_with_queue(complete)
        config = {
            "provider": "openai_compatible",
            "base_url": "http://127.0.0.1:1234/v1",
            "api_key": "",
            "chat_model": "chat-model",
            "vision_model": "vision-model",
        }
        with (
            patch.object(llm_gateway, "get_llm_config", return_value=config),
            patch.object(llm_gateway, "model_for", return_value="chat-model"),
        ):
            chunks = list(gateway.chat_stream([], "chat-model"))

        call = chunks[-1]["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "lookup")
        self.assertEqual(call["function"]["arguments"], {"q": "x"})


if __name__ == "__main__":
    unittest.main()
