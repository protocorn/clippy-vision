import json
import queue
import threading
import time
import urllib.error
import urllib.request
from itertools import count

import psutil

from core.cli_providers import is_cli_provider, provider_status
from core.cli_providers import run_chat as run_cli_chat
from core.llm_config import get_llm_config, model_for
from core.local_embeddings import embed_text, embed_texts
from core.model_residency import can_load_text, keep_alive_for, text_unavailable_reason
from core.ollama_client import (
    OllamaError,
    OllamaUnavailable,
    describe_http_error,
)

# --- Throttle configuration (relative to per-device baseline) ---
# Thresholds are NOT hardcoded — they are derived at startup from a short CPU
# baseline measurement, so the gate adapts to each machine's idle load.
#
# "Pressured" = CPU has risen _PAUSE_HEADROOM points above the idle baseline.
# "Recovered" = CPU has fallen back to within _RESUME_HEADROOM of the baseline.
# Hard ceilings prevent the thresholds from being set too high on a busy device.
_BASELINE_SAMPLES   = 4      # number of 1-second samples used to measure idle CPU
_PAUSE_HEADROOM     = 30     # CPU points above baseline that trigger a pause
_RESUME_HEADROOM    = 12     # CPU points above baseline considered "recovered"
_CPU_PAUSE_CEIL     = 92.0   # hard ceiling: never pause above this regardless of baseline
_CPU_RESUME_CEIL    = 80.0   # hard ceiling: resume threshold cap
_CHECK_INTERVAL_S   = 1.0    # seconds between CPU re-checks while paused
_BG_INTER_JOB_SLEEP = 2.0    # seconds to breathe between consecutive BACKGROUND jobs
_MAX_WAIT_SECS      = 180    # escape hatch: force-run after waiting this long regardless
_INTERACTIVE_PREEMPT_SECS = 10.0  # maximum time chat waits behind background work


class Priority:
    INTERACTIVE = 0 # chat agent - user is waiting for a response
    FOREGROUND = 10 # classifiers - image/text processing
    BACKGROUND = 20 # summarization/distillation - background tasks


class Job:
    __slots__ = (
        "url", "payload", "timeout", "event", "result", "error", "enqueued_at",
        "stream", "chunks", "waiting_for_background", "cancel_requested",
        "response", "headers", "stream_format", "local_model",
    )

    def __init__(
        self,
        url: str,
        payload: dict,
        timeout: float,
        stream: bool = False,
        *,
        headers: dict[str, str] | None = None,
        stream_format: str = "ollama_ndjson",
        local_model: bool = True,
    ):
        self.url = url
        self.payload = payload
        self.timeout = timeout
        self.event = threading.Event()
        self.result = None
        self.error = None
        self.enqueued_at = time.monotonic()
        self.stream = stream
        self.chunks = queue.Queue() if stream else None
        self.waiting_for_background = False
        self.cancel_requested = threading.Event()
        self.response = None
        self.headers = headers or {}
        self.stream_format = stream_format
        self.local_model = local_model


class LLMGateway:
    """Single chokepoint for model-provider calls. One request runs at a time,
    ordered by priority then submit order.

    CPU thresholds are derived at startup from this device's idle baseline, so
    the gate adapts automatically to each machine rather than using hardcoded
    numbers. Non-interactive jobs are health-gated: they wait for CPU to settle
    before running. An escape hatch (_MAX_WAIT_SECS) ensures no job waits
    forever. BACKGROUND jobs also insert a short inter-job sleep for thermal
    headroom. INTERACTIVE jobs (user-facing) are never throttled.
    """

    def __init__(self):
        self.queue = queue.PriorityQueue()
        self._seq = count()
        self._state_lock = threading.Lock()
        self._current_job: Job | None = None
        self._current_priority: int | None = None

        baseline = self._measure_cpu_baseline()
        self._cpu_pause_pct  = min(baseline + _PAUSE_HEADROOM,  _CPU_PAUSE_CEIL)
        self._cpu_resume_pct = min(baseline + _RESUME_HEADROOM, _CPU_RESUME_CEIL)
        print(f"[gateway] CPU baseline={baseline:.1f}%  "
              f"pause>{self._cpu_pause_pct:.1f}%  "
              f"resume<{self._cpu_resume_pct:.1f}%")

        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    @staticmethod
    def _measure_cpu_baseline() -> float:
        """Sample CPU% at startup to establish this device's idle baseline.
        The first psutil call always returns 0.0, so we discard it."""
        psutil.cpu_percent()  # discard the initialisation call
        samples = [psutil.cpu_percent(interval=1.0) for _ in range(_BASELINE_SAMPLES)]
        return sum(samples) / len(samples)

    def _is_pressured(self) -> bool:
        return psutil.cpu_percent(interval=0.5) > self._cpu_pause_pct

    def _is_recovered(self) -> bool:
        return psutil.cpu_percent(interval=0.5) < self._cpu_resume_pct

    def _health_gate(self, job: "Job", *, priority: int) -> None:
        """Block until CPU has recovered, or until the job's max-wait deadline
        expires — whichever comes first.

        BACKGROUND jobs never force-run under sustained pressure: they fail soft
        so catch-up can retry later instead of thrashing the machine.
        """
        deadline = job.enqueued_at + _MAX_WAIT_SECS

        if not self._is_pressured():
            return  # fast path: system is healthy, no wait needed

        while time.monotonic() < deadline:
            if job.cancel_requested.is_set():
                job.error = OSError("deferred: preempted by interactive chat")
                return
            time.sleep(_CHECK_INTERVAL_S)
            if self._is_recovered():
                return  # CPU settled, proceed

        waited = time.monotonic() - job.enqueued_at
        if priority >= Priority.BACKGROUND:
            job.error = OSError(
                f"deferred: cpu pressured after {waited:.0f}s — catch-up will retry"
            )
            print(f"[gateway] background job deferred after {waited:.0f}s CPU pressure")
            return

        # FOREGROUND escape hatch — drain classifiers that the user is waiting on
        print(f"[gateway] escape hatch triggered after {waited:.0f}s wait — running despite pressure")

    def _cancel_background_for_chat(self, background_job: Job) -> None:
        """Close the client response so the gateway can move on to chat.

        Ollama stops generating when its client disconnects, so dropping the
        response frees the gateway without waiting out the background timeout.
        """
        with self._state_lock:
            if self._current_job is not background_job:
                return
            if self._current_priority is None or self._current_priority < Priority.BACKGROUND:
                return
            background_job.cancel_requested.set()
            response = background_job.response
        print("[gateway] preempting background job for waiting chat")
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    def _enqueue(self, job: Job, priority: int) -> None:
        """Queue a job and bound interactive wait behind an in-flight background call."""
        if priority == Priority.INTERACTIVE:
            with self._state_lock:
                background_job = (
                    self._current_job
                    if self._current_priority is not None
                    and self._current_priority >= Priority.BACKGROUND
                    else None
                )
            if background_job is not None:
                job.waiting_for_background = True
                timer = threading.Timer(
                    _INTERACTIVE_PREEMPT_SECS,
                    self._cancel_background_for_chat,
                    args=(background_job,),
                )
                timer.daemon = True
                timer.start()
        self.queue.put((priority, next(self._seq), job))

    @staticmethod
    def _open(job: Job):
        """Open a provider request and preserve Ollama's actionable errors."""
        req = urllib.request.Request(
            job.url,
            data=json.dumps(job.payload).encode(),
            headers={"Content-Type": "application/json", **job.headers},
        )
        try:
            return urllib.request.urlopen(req, timeout=job.timeout)
        except urllib.error.HTTPError as err:
            if job.local_model:
                raise OllamaError(
                    f"ollama chat failed — {describe_http_error(err)}"
                ) from err
            detail = err.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"provider request failed with HTTP {err.code}: {detail[-1200:]}"
            ) from err
        except urllib.error.URLError as err:
            if job.local_model:
                raise OllamaUnavailable(
                    f"ollama not reachable ({err.reason}) — is the Ollama server running?"
                ) from err
            raise RuntimeError(f"provider not reachable: {err.reason}") from err

    @staticmethod
    def _provider_urls(config: dict[str, str]) -> tuple[str, str]:
        """Return chat and model-list URLs for the selected HTTP provider."""
        base = config["base_url"].rstrip("/")
        if config["provider"] == "ollama":
            if base.endswith("/api"):
                base = base[:-4]
            return f"{base}/api/chat", f"{base}/api/tags"
        api_base = base if config["provider"] == "gemini_api" or base.endswith("/v1") else f"{base}/v1"
        return f"{api_base}/chat/completions", f"{api_base}/models"

    @staticmethod
    def _auth_headers(config: dict[str, str]) -> dict[str, str]:
        key = (config.get("api_key") or "").strip()
        return {"Authorization": f"Bearer {key}"} if key else {}

    @staticmethod
    def _resolve_model(model: str | None, role: str) -> str:
        requested = str(model or "").strip()
        configured = model_for(role, requested or None)
        defaults = {
            "chat": {"", "qwen3:8b"},
            "vision": {"qwen3-vl:4b"},
        }
        if requested in defaults.get(role, set()):
            return configured
        return requested or configured

    @staticmethod
    def _openai_options(options: dict | None) -> dict:
        if not options:
            return {}
        mapped = {}
        for source, target in (
            ("temperature", "temperature"),
            ("top_p", "top_p"),
            ("num_predict", "max_tokens"),
        ):
            if source in options:
                mapped[target] = options[source]
        return mapped

    @staticmethod
    def _openai_messages(messages: list[dict]) -> list[dict]:
        """Translate Ollama image and tool messages to OpenAI-compatible form."""
        converted = []
        pending_tool_ids = []
        for message in messages:
            if not isinstance(message, dict):
                converted.append(message)
                continue
            converted_message = dict(message)
            images = message.get("images")
            if images:
                parts = []
                text = message.get("content") or ""
                if text:
                    parts.append({"type": "text", "text": text})
                for image in images:
                    value = str(image)
                    if not value.startswith("data:"):
                        value = f"data:image/jpeg;base64,{value}"
                    parts.append({"type": "image_url", "image_url": {"url": value}})
                converted_message.pop("images", None)
                converted_message["content"] = parts

            if message.get("role") == "assistant" and message.get("tool_calls"):
                normalized_calls = []
                for index, call in enumerate(message["tool_calls"]):
                    function = dict(call.get("function") or {})
                    arguments = function.get("arguments", {})
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    call_id = call.get("id") or f"call_{len(converted)}_{index}"
                    pending_tool_ids.append(call_id)
                    function["arguments"] = arguments
                    normalized_calls.append(
                        {
                            "id": call_id,
                            "type": call.get("type", "function"),
                            "function": function,
                        }
                    )
                converted_message["tool_calls"] = normalized_calls

            if message.get("role") == "tool" and not message.get("tool_call_id") and pending_tool_ids:
                converted_message["tool_call_id"] = pending_tool_ids.pop(0)
            converted.append(converted_message)
        return converted

    @staticmethod
    def _openai_response_format(schema: dict) -> dict:
        if isinstance(schema, dict) and schema.get("type") == "json_object":
            return schema
        return {"type": "json_object"}

    @staticmethod
    def _normalize_openai_response(body: dict) -> dict:
        choices = body.get("choices") or []
        message = (choices[0] if choices else {}).get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                item.get("text", "") for item in content if isinstance(item, dict)
            )
        normalized = {"role": message.get("role", "assistant"), "content": content}
        thinking = message.get("reasoning_content") or message.get("reasoning") or message.get("thinking")
        if thinking:
            normalized["thinking"] = thinking
        tool_calls = []
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments.strip() else {}
                except json.JSONDecodeError:
                    arguments = {}
            tool_calls.append(
                {
                    "id": tool_call.get("id"),
                    "type": tool_call.get("type", "function"),
                    "function": {
                        "name": function.get("name", ""),
                        "arguments": arguments,
                    },
                }
            )
        if tool_calls:
            normalized["tool_calls"] = tool_calls
        return {"message": normalized}

    @staticmethod
    def _normalize_openai_stream_chunk(body: dict) -> dict | None:
        choices = body.get("choices") or []
        if not choices:
            return None
        delta = choices[0].get("delta") or {}
        message = {}
        for source, target in (
            ("content", "content"),
            ("reasoning_content", "thinking"),
            ("reasoning", "thinking"),
            ("thinking", "thinking"),
        ):
            value = delta.get(source)
            if isinstance(value, str) and value and target not in message:
                message[target] = value
        tool_calls = []
        for tool_call in delta.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            tool_calls.append(
                {
                    "index": tool_call.get("index", len(tool_calls)),
                    "id": tool_call.get("id"),
                    "type": tool_call.get("type", "function"),
                    "function": {
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", ""),
                    },
                }
            )
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {"message": message} if message else None

    def _run_stream_job(self, job: "Job") -> None:
        try:
            with self._open(job) as resp:
                with self._state_lock:
                    job.response = resp
                while True:
                    if job.cancel_requested.is_set():
                        raise OSError("deferred: preempted by interactive chat")
                    line = resp.readline()
                    if not line:
                        break
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if job.stream_format == "openai_sse":
                        if line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        if line == "[DONE]":
                            break
                    try:
                        parsed = json.loads(line)
                        if isinstance(parsed, dict) and parsed.get("error"):
                            raise RuntimeError(str(parsed["error"]))
                        if job.stream_format == "openai_sse":
                            parsed = self._normalize_openai_stream_chunk(parsed)
                            if parsed is None:
                                continue
                        job.chunks.put(parsed)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            job.error = e
        finally:
            with self._state_lock:
                job.response = None
            job.chunks.put(None)  # sentinel — stream finished
            job.event.set()

    def _worker_loop(self):
        while True:
            priority, seq, job = self.queue.get()
            with self._state_lock:
                self._current_job = job
                self._current_priority = priority

            # Health gate for all non-interactive jobs
            if priority > Priority.INTERACTIVE and job.local_model:
                if not can_load_text():
                    job.error = OSError(f"deferred: {text_unavailable_reason()}")
                else:
                    self._health_gate(job, priority=priority)
            if job.error:
                job.event.set()
                if job.chunks is not None:
                    job.chunks.put(None)
                self.queue.task_done()
                with self._state_lock:
                    self._current_job = None
                    self._current_priority = None
                continue

            if job.stream:
                self._run_stream_job(job)
            else:
                try:
                    with self._open(job) as resp:
                        with self._state_lock:
                            job.response = resp
                        if job.cancel_requested.is_set():
                            raise OSError("deferred: preempted by interactive chat")
                        job.result = json.loads(resp.read())
                except Exception as e:
                    job.error = e
                finally:
                    with self._state_lock:
                        job.response = None
                    job.event.set()

            self.queue.task_done()
            with self._state_lock:
                self._current_job = None
                self._current_priority = None

            # Give the CPU breathing room between consecutive background jobs.
            # This sleep is after event.set() so the caller is already unblocked.
            if priority >= Priority.BACKGROUND:
                time.sleep(_BG_INTER_JOB_SLEEP)

    def chat(
        self,
        messages,
        model,
        *,
        priority=Priority.FOREGROUND,
        tools=None,
        format=None,
        options=None,
        think=None,
        timeout=180,
        keep_alive=None,
    ) -> dict | None:
        config = get_llm_config()
        role = "vision" if "vl" in str(model or "").lower() else "chat"
        resolved_model = self._resolve_model(model, role)
        if is_cli_provider(config["provider"]):
            return run_cli_chat(
                config,
                messages,
                resolved_model,
                schema=format if isinstance(format, dict) else None,
                tools=tools,
                timeout=timeout,
            )

        chat_url, _ = self._provider_urls(config)
        local_model = config["provider"] == "ollama"
        if local_model:
            payload = {
                "model": resolved_model,
                "messages": messages,
                "stream": False,
                "keep_alive": keep_alive_for(resolved_model)
                if keep_alive is None
                else keep_alive,
            }
            if tools is not None:
                payload["tools"] = tools
            if format is not None:
                payload["format"] = format
            if options is not None:
                payload["options"] = options
            if think is not None:
                payload["think"] = think
        else:
            payload = {
                "model": resolved_model,
                "messages": self._openai_messages(messages),
                "stream": False,
            }
            if tools is not None:
                payload["tools"] = tools
            if format is not None:
                payload["response_format"] = self._openai_response_format(format)
            payload.update(self._openai_options(options))

        job = Job(
            chat_url,
            payload,
            timeout,
            headers=self._auth_headers(config),
            local_model=local_model,
        )
        self._enqueue(job, priority)
        job.event.wait()
        if job.error:
            raise job.error
        if isinstance(job.result, dict) and job.result.get("error"):
            raise RuntimeError(str(job.result["error"]))
        return job.result if local_model else self._normalize_openai_response(job.result or {})

    def chat_stream(
        self,
        messages,
        model,
        *,
        priority=Priority.FOREGROUND,
        tools=None,
        format=None,
        options=None,
        think=None,
        timeout=180,
        keep_alive=None,
    ):
        """Yield normalized streaming chunks from the selected provider."""
        config = get_llm_config()
        role = "vision" if "vl" in str(model or "").lower() else "chat"
        resolved_model = self._resolve_model(model, role)
        if is_cli_provider(config["provider"]):
            yield run_cli_chat(
                config,
                messages,
                resolved_model,
                schema=format if isinstance(format, dict) else None,
                tools=tools,
                timeout=timeout,
            )
            return

        chat_url, _ = self._provider_urls(config)
        local_model = config["provider"] == "ollama"
        if local_model:
            payload = {
                "model": resolved_model,
                "messages": messages,
                "stream": True,
                "keep_alive": keep_alive_for(resolved_model)
                if keep_alive is None
                else keep_alive,
            }
            if tools is not None:
                payload["tools"] = tools
            if format is not None:
                payload["format"] = format
            if options is not None:
                payload["options"] = options
            if think is not None:
                payload["think"] = think
            stream_format = "ollama_ndjson"
        else:
            payload = {
                "model": resolved_model,
                "messages": self._openai_messages(messages),
                "stream": True,
            }
            if tools is not None:
                payload["tools"] = tools
            if format is not None:
                payload["response_format"] = self._openai_response_format(format)
            payload.update(self._openai_options(options))
            stream_format = "openai_sse"

        job = Job(
            chat_url,
            payload,
            timeout,
            stream=True,
            headers=self._auth_headers(config),
            stream_format=stream_format,
            local_model=local_model,
        )
        self._enqueue(job, priority)
        if job.waiting_for_background:
            yield {"_gateway_status": "Waiting for background work"}

        tool_call_parts = {}
        while True:
            chunk = job.chunks.get()
            if chunk is None:
                break
            if stream_format == "openai_sse":
                message = chunk.get("message") or {}
                for call in message.pop("tool_calls", []):
                    index = call.get("index", len(tool_call_parts))
                    current = tool_call_parts.setdefault(
                        index,
                        {
                            "id": call.get("id"),
                            "type": call.get("type", "function"),
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    current["id"] = call.get("id") or current["id"]
                    function = call.get("function") or {}
                    current["function"]["name"] += function.get("name", "")
                    current["function"]["arguments"] += function.get("arguments", "")
                if not message:
                    continue
            yield chunk

        if job.error:
            raise job.error
        if tool_call_parts:
            calls = []
            for call in tool_call_parts.values():
                arguments = call["function"]["arguments"]
                try:
                    call["function"]["arguments"] = (
                        json.loads(arguments) if arguments else {}
                    )
                except json.JSONDecodeError:
                    call["function"]["arguments"] = {}
                calls.append(call)
            yield {"message": {"tool_calls": calls}}

    def embed(self, text, *, embed_model=None, priority=Priority.FOREGROUND, timeout=60, keep_alive=None):
        """Embed text with the bundled MiniLM model, independent of Ollama."""
        if isinstance(text, str):
            return embed_text(text)
        return embed_texts(text)

    def test_connection(self, config: dict[str, str] | None = None) -> dict:
        current = get_llm_config() if config is None else config
        if is_cli_provider(current["provider"]):
            return provider_status(current)
        result = self.capabilities(current)
        return {
            "ok": result["ok"],
            "provider": result["provider"],
            "url": result["url"],
            "models": result.get("models", []),
            "capabilities": result.get("capabilities", {}),
            **({"error": result["error"]} if result.get("error") else {}),
        }

    def capabilities(self, config: dict[str, str] | None = None) -> dict:
        current = get_llm_config() if config is None else config
        if is_cli_provider(current["provider"]):
            return provider_status(current)
        _, models_url = self._provider_urls(current)
        request = urllib.request.Request(
            models_url,
            headers=self._auth_headers(current),
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read())
            if current["provider"] == "ollama":
                models = [
                    str(item.get("name") or "")
                    for item in payload.get("models", [])
                    if item.get("name")
                ]
            else:
                models = [
                    str(item.get("id") or "")
                    for item in payload.get("data", [])
                    if item.get("id")
                ]
            model_set = {model.lower() for model in models}

            def availability(configured: str) -> bool | None:
                if not model_set:
                    return None
                target = configured.lower()
                return target in model_set or any(
                    target.split(":")[0] == item.split(":")[0]
                    for item in model_set
                )

            return {
                "ok": True,
                "provider": current["provider"],
                "url": models_url,
                "models": models[:200],
                "capabilities": {
                    role: {
                        "model": current[field],
                        "available": availability(current[field]),
                    }
                    for role, field in (
                        ("chat", "chat_model"),
                        ("vision", "vision_model"),
                    )
                },
            }
        except Exception as exc:
            return {
                "ok": False,
                "provider": current["provider"],
                "url": models_url,
                "models": [],
                "capabilities": {},
                "error": str(exc),
            }

gateway = LLMGateway()
