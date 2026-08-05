from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any


CLI_PROVIDERS = {
    "codex_cli": {
        "label": "Codex subscription",
        "command": "codex",
        "login_args": ["--login"],
        "env": "CLIPPY_CODEX_COMMAND",
    },
    "claude_cli": {
        "label": "Claude subscription",
        "command": "claude",
        "login_args": [],
        "env": "CLIPPY_CLAUDE_COMMAND",
    },
}

CLI_DEFAULT_MODELS = {
    "codex_cli": ("default", "default"),
    "claude_cli": ("sonnet", "sonnet"),
}

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CLI_LOCK = threading.Lock()


def is_cli_provider(provider: str) -> bool:
    return str(provider or "").strip().lower() in CLI_PROVIDERS


def provider_label(provider: str) -> str:
    return CLI_PROVIDERS.get(provider, {}).get("label", provider)


def provider_defaults(provider: str) -> tuple[str, str]:
    return CLI_DEFAULT_MODELS.get(provider, ("default", "default"))


def _raw_command(config: dict[str, str]) -> str:
    provider = config.get("provider", "")
    metadata = CLI_PROVIDERS.get(provider)
    if not metadata:
        raise ValueError(f"Unsupported CLI provider: {provider}")
    configured = str(config.get("cli_command") or "").strip()
    if configured:
        return configured
    env_name = metadata["env"]
    return str(os.environ.get(env_name) or metadata["command"]).strip()


def command_parts(config: dict[str, str]) -> list[str]:
    parts = shlex.split(_raw_command(config))
    if not parts:
        raise FileNotFoundError("No CLI command is configured.")
    executable = parts[0]
    resolved = Path(executable).expanduser()
    if resolved.parent != Path("."):
        if not resolved.is_file():
            raise FileNotFoundError(f"CLI executable not found: {executable}")
        parts[0] = str(resolved.resolve())
        return parts
    located = shutil.which(executable)
    if not located:
        raise FileNotFoundError(
            f"{executable} is not installed or is not on PATH."
        )
    parts[0] = located
    return parts


def provider_status(config: dict[str, str]) -> dict[str, Any]:
    provider = config.get("provider", "")
    metadata = CLI_PROVIDERS.get(provider)
    if not metadata:
        return {
            "ok": False,
            "provider": provider,
            "label": provider,
            "installed": False,
            "authenticated": "unknown",
            "error": "Not a CLI provider.",
        }
    try:
        parts = command_parts(config)
        command = " ".join(shlex.quote(part) for part in parts)
        return {
            "ok": True,
            "provider": provider,
            "label": metadata["label"],
            "installed": True,
            "authenticated": "unknown",
            "auth_source": "official_cli",
            "command": command,
            "login_command": " ".join(
                shlex.quote(part) for part in (*parts, *metadata["login_args"])
            ),
            "models": [],
            "capabilities": {
                "chat": {"model": config.get("chat_model", "default"), "available": True},
                "vision": {"model": config.get("vision_model", "default"), "available": True},
            },
            "message": (
                f"{metadata['label']} CLI detected. Sign in from the opened terminal "
                "if this account has not been connected yet."
            ),
        }
    except (FileNotFoundError, ValueError, OSError) as exc:
        return {
            "ok": False,
            "provider": provider,
            "label": metadata["label"],
            "installed": False,
            "authenticated": "unknown",
            "models": [],
            "capabilities": {},
            "error": str(exc),
            "login_command": f"{metadata['command']} {' '.join(metadata['login_args'])}".strip(),
        }


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for item in content:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    pieces.append(text)
        return "".join(pieces)
    return str(content or "")


def _prompt_from_messages(
    messages: list[dict[str, Any]],
    image_paths: list[Path],
    schema: dict | None,
    provider: str,
) -> str:
    sections = [
        "You are the response engine inside Clippy Vision.",
        "Answer the user directly. Do not edit files, run commands, or change the workspace.",
    ]
    if provider == "codex_cli":
        sections.append("Use the supplied activity context as your only Clippy context.")
    if image_paths:
        sections.append(
            "The following local image files are attached for visual analysis:\n"
            + "\n".join(f"- {path}" for path in image_paths)
        )
    for message in messages:
        role = str(message.get("role") or "message").upper()
        content = _message_content(message)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            content += "\nTool calls: " + json.dumps(tool_calls, ensure_ascii=False)
        if content.strip():
            sections.append(f"[{role}]\n{content.strip()}")
    if schema:
        sections.append(
            "Return only valid JSON matching this schema:\n"
            + json.dumps(schema, ensure_ascii=False)
        )
    return "\n\n".join(sections)


def _decode_image(value: Any) -> bytes | None:
    if not isinstance(value, str):
        return None
    if value.startswith("data:"):
        _, encoded = value.split(",", 1)
    else:
        encoded = value
    try:
        return base64.b64decode(encoded, validate=False)
    except (ValueError, binascii.Error):
        return None


def _materialize_images(messages: list[dict[str, Any]], root: Path) -> list[Path]:
    paths = []
    index = 0
    for message in messages:
        for image in message.get("images") or []:
            if isinstance(image, str) and Path(image).is_file():
                source = Path(image).resolve()
                target = root / f"image-{index}{source.suffix or '.jpg'}"
                shutil.copyfile(source, target)
            else:
                payload = _decode_image(image)
                if not payload:
                    continue
                target = root / f"image-{index}.jpg"
                target.write_bytes(payload)
            paths.append(target)
            index += 1
    return paths


def _write_schema(root: Path, schema: dict) -> Path:
    path = root / "output-schema.json"
    path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    return path


def _model_args(provider: str, model: str | None) -> list[str]:
    value = str(model or "").strip()
    default, _ = provider_defaults(provider)
    if not value or value in {"default", default, "auto"}:
        return []
    return ["--model", value]


def _build_command(
    provider: str,
    base: list[str],
    model: str | None,
    root: Path,
    image_paths: list[Path],
    schema: dict | None,
) -> list[str]:
    if provider == "codex_cli":
        command = [
            *base,
            "exec",
            "--json",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--cd",
            str(root),
            *_model_args(provider, model),
        ]
        if schema:
            command.extend(["--output-schema", str(_write_schema(root, schema))])
        for image_path in image_paths:
            command.extend(["--image", str(image_path)])
        command.append("-")
        return command

    if provider == "claude_cli":
        command = [
            *base,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--max-turns",
            "1",
            "--permission-mode",
            "plan",
            *_model_args(provider, model),
        ]
        if schema:
            command.extend(["--json-schema", json.dumps(schema, ensure_ascii=False)])
        command.append("--tools=Read" if image_paths else "--tools=")
        return command

    command = [
        *base,
        "-p",
        "",
        "--output-format",
        "json",
        "--approval-mode",
        "plan",
        *_model_args(provider, model),
    ]
    if image_paths:
        command.extend(["--include-directories", str(root)])
    return command


def _text_from_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text_from_value(item) for item in value)
    if isinstance(value, dict):
        for key in ("result", "output_text", "text", "content"):
            if key in value:
                text = _text_from_value(value[key])
                if text:
                    return text
        message = value.get("message")
        if message is not None:
            return _text_from_value(message)
    return ""


def _extract_output(provider: str, stdout: str) -> str:
    cleaned = _ANSI_RE.sub("", stdout or "").strip()
    if not cleaned:
        return ""
    objects = []
    for line in cleaned.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            objects.append(value)
    if not objects:
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError:
            return cleaned
        text = _text_from_value(value)
        return text or json.dumps(value, ensure_ascii=False)

    final = []
    messages = []
    for value in objects:
        if not isinstance(value, dict):
            continue
        event_type = value.get("type")
        if event_type == "result" and value.get("result") is not None:
            final.append(_text_from_value(value["result"]))
            continue
        if event_type == "item.completed":
            item = value.get("item") or {}
            if item.get("type") in {"agent_message", "message", "output_text"}:
                messages.append(_text_from_value(item))
            continue
        if event_type in {"response.output_text.done", "response.completed"}:
            text = _text_from_value(value.get("text") or value.get("response"))
            if text:
                final.append(text)
            continue
        if value.get("role") == "assistant":
            text = _text_from_value(value)
            if text:
                messages.append(text)
            continue
        if value.get("result") is not None:
            final.append(_text_from_value(value["result"]))
    selected = final or messages
    if selected:
        return "\n".join(text for text in selected if text).strip()
    if len(objects) == 1:
        value = objects[0]
        text = _text_from_value(value)
        return text or json.dumps(value, ensure_ascii=False)
    return "\n".join(json.dumps(value, ensure_ascii=False) for value in objects)


def run_chat(
    config: dict[str, str],
    messages: list[dict[str, Any]],
    model: str | None,
    *,
    schema: dict | None = None,
    timeout: float = 240,
) -> dict:
    provider = config.get("provider", "")
    if not is_cli_provider(provider):
        raise ValueError(f"Unsupported CLI provider: {provider}")
    with _CLI_LOCK:
        with tempfile.TemporaryDirectory(prefix="clippy-cli-") as temporary:
            root = Path(temporary)
            image_paths = _materialize_images(messages, root)
            prompt = _prompt_from_messages(messages, image_paths, schema, provider)
            command = _build_command(
                provider,
                command_parts(config),
                model,
                root,
                image_paths,
                schema,
            )
            environment = dict(os.environ)
            environment.update({"NO_COLOR": "1", "TERM": "dumb"})
            try:
                process = subprocess.Popen(
                    command,
                    cwd=root,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                stdout, stderr = process.communicate(prompt, timeout=max(10, timeout))
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.communicate()
                raise TimeoutError(f"{provider_label(provider)} timed out.") from exc
            except OSError as exc:
                raise RuntimeError(f"Could not start {provider_label(provider)}: {exc}") from exc
            if process.returncode != 0:
                detail = (stderr or stdout or "CLI returned an error").strip()
                raise RuntimeError(f"{provider_label(provider)} failed: {detail[-1600:]}")
            content = _extract_output(provider, stdout)
            if not content:
                detail = (stderr or "CLI returned no response").strip()
                raise RuntimeError(f"{provider_label(provider)} returned no response: {detail[-800:]}")
            return {"message": {"role": "assistant", "content": content}}
