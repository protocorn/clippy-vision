"""Grading: deterministic substring checks first, fixed-prompt LLM judge for semantic
cases. Judge calls go through the gateway at temperature 0 and are counted separately
so judging cost never contaminates a strategy's own cost score.
"""

import json

import _paths  # noqa: F401

from core.llm_gateway import Priority, gateway

_JUDGE_MODEL = "qwen3:8b"
_JUDGE_SYS = (
    "You are a strict grader. Given a QUESTION, the FACTS retrieved from a memory system, "
    "and the GROUND_TRUTH answer, decide whether the retrieved facts let someone answer the "
    "question correctly and consistently with the ground truth. "
    "correct=true only if the needed current information is present and not contradicted. "
    'Return JSON {"correct": true|false}.'
)
_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"correct": {"type": "boolean"}},
    "required": ["correct"],
}


class Grader:
    def __init__(self):
        self.judge_calls = 0

    def _forbidden_present(self, query, retrieved_blob):
        return any(f.lower() in retrieved_blob for f in query.get("forbid", []))

    def _judge(self, question, facts, ground_truth):
        self.judge_calls += 1
        body = gateway.chat(
            [
                {"role": "system", "content": _JUDGE_SYS},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": question,
                            "facts": facts,
                            "ground_truth": ground_truth,
                        }
                    ),
                },
            ],
            _JUDGE_MODEL,
            format=_JUDGE_SCHEMA,
            think=False,
            options={"temperature": 0},
            priority=Priority.FOREGROUND,
        )
        content = body["message"]["content"]
        out = json.loads(content) if isinstance(content, str) else content
        return bool(out.get("correct"))

    def grade(self, query, retrieved):
        """Return (correct: bool, supersession_ok: bool)."""
        blob = "\n".join(retrieved).lower()
        forbidden = self._forbidden_present(query, blob)
        supersession_ok = not forbidden

        if query["grade"] == "det":
            expect_ok = all(e.lower() in blob for e in query.get("expect", []))
            correct = expect_ok and not forbidden
        else:  # judge
            judged = self._judge(query["q"], retrieved, query.get("answer", ""))
            correct = judged and not forbidden

        return correct, supersession_ok
