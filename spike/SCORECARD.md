# Framework spike scorecard

Criteria and weights come from PRD §8.6. Each scored criterion gets 1–5 points. Offline results come from `uv run pytest spike/tests -q`; live results from `spike/run_live.py`.

| Criterion | Weight | Pydantic AI | OpenAI Agents SDK | Notes |
| --- | --- | --- | --- | --- |
| Works with the Meta API (offline fake) | Must pass | | | |
| Works with the Meta API (live) | Must pass | | | Pending approval for live runs |
| Policy can be enforced in code | 25% | | | |
| Glue code needed (lines) | 20% | | | |
| Visibility (model + tool events) | 20% | | | |
| Overhead (requests, tokens, latency) | 15% | | | |
| Fit with typed deliverables | 20% | | | |
| **Weighted score** | | | | |

## Decision

To be filled in after the live run.
