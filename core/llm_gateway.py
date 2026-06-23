import json
import queue
import threading
import urllib.request
from itertools import count
from typing import Optional

OLLAMA_URL = "http://localhost:11434/api/chat"
EMBED_URL  = "http://localhost:11434/api/embed"
EMBED_MODEL = "nomic-embed-text"

class Priority:
    INTERACTIVE = 0 # chat agent - user is waiting for a response
    FOREGROUND = 10 # classifiers - image/text processing
    BACKGROUND = 20 # summarization/distillation - background tasks


class Job:
    __slots__ = ("url", "payload", "timeout", "event", "result", "error")

    def __init__(self, url: str, payload: dict, timeout: float):
        self.url = url
        self.payload = payload
        self.timeout = timeout
        self.event = threading.Event()
        self.result = None
        self.error = None

    
class LLMGateway:
    """Single chokepoint for all Ollama calls. One request in flight at a time,
    ordered by priority then submit order."""

    def __init__(self):
        self.queue = queue.PriorityQueue()
        self._seq = count()
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def _worker_loop(self):
        while True:
            priority, seq, job = self.queue.get()

            try:
                req = urllib.request.Request(
                    job.url, 
                    data=json.dumps(job.payload).encode(), 
                    headers={"Content-Type": "application/json"})

                with urllib.request.urlopen(req, timeout=job.timeout) as resp:
                    job.result = json.loads(resp.read())
            except Exception as e:
                job.error = e
            finally:
                job.event.set()
                self.queue.task_done()



    def chat(self, messages, model, *, priority=Priority.FOREGROUND, tools=None, format=None, options=None, think=None, timeout=180) -> Optional[dict]:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
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
        self.queue.put((priority, next(self._seq), job))

        job.event.wait()
        if job.error:
            raise job.error
        return job.result

    def embed(self, text, *, embed_model, priority=Priority.FOREGROUND, timeout=60):
        """Embed a string (or list of strings) through the same serialized queue.
        Returns a single vector for a string input, or a list of vectors for a list."""
        payload = {"model": embed_model, "input": text}
        job = Job(EMBED_URL, payload, timeout)
        self.queue.put((priority, next(self._seq), job))

        job.event.wait()
        if job.error:
            raise job.error
        embeddings = job.result.get("embeddings", [])
        if isinstance(text, str):
            return embeddings[0] if embeddings else []
        return embeddings

gateway = LLMGateway()