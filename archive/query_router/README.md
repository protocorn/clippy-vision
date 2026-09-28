# Archived: query router

The live app does not classify questions. MCP calls `search_sessions`, `search_events`, and `recall_memory` directly. This folder is the old MiniLM query router and the lab around it.

## Why archived

In-app ReAct chat was the only caller. After that path was removed, nothing in capture, the timeline, or MCP imported `classify_query` or `should_prefetch`. Setup no longer downloads the router checkpoint.

## Contents

| Path | Was |
|------|-----|
| `router.py` | `agent/router.py` |
| `router_labelling_policy.md` | `docs/router_labelling_policy.md` |
| `router_seed.jsonl` | `core/data/router_seed.jsonl` |
| `scripts/` | `scripts/train_router.py`, `generate_router_data.py`, `test_classifier.py`, `clean_router_data.py` |
| `eval/` | `testing/router_eval/` |
| `models/router_classifier/` | Local checkpoint. Gitignored. Not downloaded by setup. |

`scikit-learn` was removed from `requirements.txt`. `scripts/train_router.py` still imports it. Install it only if you retrain.

Time parsing (`agent/helpers/time_resolver.py`) and memory search (`agent/prefetch/memory_query.py`) stayed in the live app. They are called directly, not through this classifier.

## Restore

1. Copy `router.py` back to `agent/router.py` and point `CLASSIFIER_PATH` at `models/router_classifier/best`.
2. Put the download helpers back in `core/model_download.py` and call them from `ensure_all`.
3. Copy the scripts and `eval/` back to `scripts/` and `testing/router_eval/`.
4. Add `scikit-learn` to `requirements.txt` if you train.
