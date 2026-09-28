# Archived: prefetch strategies

These modules used to run after the query router labeled a question. The live app does not call them. MCP tools search sessions and events in `agent/retrieval.py`. A question with a `query` on `recall_memory` still uses `agent/prefetch/memory_query.py`, which stayed in the app.

## Contents

| File | Was |
|------|-----|
| `specific_recall.py` | `agent/prefetch/specific_recall.py` |
| `time_anchor.py` | `agent/prefetch/time_anchor.py` |
| `topic_search.py` | `agent/prefetch/topic_search.py` |
| `detect_recency.py` | `agent/helpers/detect_recency.py` |
| `recall_test.py` | `tests/recall_test.py` — manual script, not pytest |

## Restore

Copy the three strategy files back under `agent/prefetch/`, `detect_recency.py` back under `agent/helpers/`, and point their imports at `agent.prefetch.topic_search` and `agent.helpers.detect_recency` again.
