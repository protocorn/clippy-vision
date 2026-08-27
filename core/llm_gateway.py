import json
import queue
import threading
import time
import urllib.error
import urllib.request
from itertools import count

import psutil

from core.local_embeddings import embed_text, embed_texts
from core.model_residency import can_load_text, keep_alive_for, text_unavailable_reason
from core.ollama_client import (
    OllamaError,
    OllamaUnavailable,
    base_url,
    describe_http_error,
)

OLLAMA_URL = f"{base_url()}/api/chat"

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
        "response",
    )

    def __init__(self, url: str, payload: dict, timeout: float, stream: bool = False):
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


class LLMGateway:
    """Single chokepoint for all Ollama calls. One request in flight at a time,
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
        """Open the request, translating Ollama failures into readable errors."""
        req = urllib.request.Request(
            job.url,
            data=json.dumps(job.payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            return urllib.request.urlopen(req, timeout=job.timeout)
        except urllib.error.HTTPError as err:
            raise OllamaError(f"ollama chat failed — {describe_http_error(err)}") from err
        except urllib.error.URLError as err:
            raise OllamaUnavailable(
                f"ollama not reachable ({err.reason}) — is the Ollama server running?"
            ) from err

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
                    try:
                        job.chunks.put(json.loads(line))
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
            if priority > Priority.INTERACTIVE:
                if not can_load_text():
                    job.error = OSError(f"deferred: {text_unavailable_reason()}")
                else:
                    self._health_gate(job, priority=priority)
            if job.error:
                job.event.set()
                if job.chunks is not None:
                    job.chunks.put(None)
                self.queue.task_done()
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

    def chat(self, messages, model, *, priority=Priority.FOREGROUND, tools=None, format=None, options=None, think=None, timeout=180, keep_alive=None) -> dict | None:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": keep_alive_for(model) if keep_alive is None else keep_alive,
        }
        if tools is not None:
            payload["tools"] = tools
        if format is not None:
            payload["format"] = format
        if options is not None:
            payload["options"] = options
        if think is not None:
            payload["think"] = think

        job = Job(OLLAMA_URL, payload, timeout)
        self._enqueue(job, priority)

        job.event.wait()
        if job.error:
            raise job.error
        return job.result

    def chat_stream(self, messages, model, *, priority=Priority.FOREGROUND, tools=None, format=None, options=None, think=None, timeout=180, keep_alive=None):
        """Yield Ollama NDJSON stream chunks for one chat call (thinking + content deltas)."""
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": keep_alive_for(model) if keep_alive is None else keep_alive,
        }
        if tools is not None:
            payload["tools"] = tools
        if format is not None:
            payload["format"] = format
        if options is not None:
            payload["options"] = options
        if think is not None:
            payload["think"] = think

        job = Job(OLLAMA_URL, payload, timeout, stream=True)
        self._enqueue(job, priority)

        if job.waiting_for_background:
            yield {"_gateway_status": "Waiting for background work"}
        while True:
            chunk = job.chunks.get()
            if chunk is None:
                break
            yield chunk

        if job.error:
            raise job.error

    def embed(self, text, *, embed_model=None, priority=Priority.FOREGROUND, timeout=60, keep_alive=None):
        """Embed text with the bundled MiniLM model, independent of Ollama."""
        if isinstance(text, str):
            return embed_text(text)
        return embed_texts(text)

gateway = LLMGateway()
