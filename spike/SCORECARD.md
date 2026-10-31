# Framework spike scorecard

Criteria and weights were fixed before either build. Scored criteria get 1–5 points. Run on Oct 28, 2026 with pydantic-ai-slim 2.63.0 and openai-agents 0.24.0.

## Results

| Criterion | Weight | Pydantic AI | OpenAI Agents SDK | Notes |
| --- | --- | --- | --- | --- |
| Works with the Meta API (offline fake) | Must pass | yes 15/15 tests | yes 15/15 tests | Same acceptance suite for both |
| Works with the Meta API (live, `muse-spark-1.3`) | Must pass | yes 2/2 runs | yes 2/2 runs | Typed output, tool calls and web search all worked |
| Policy can be enforced in code | 25% | **5** | 4 | Pydantic AI: one `wrap_tool_execute` hook covers every tool. Agents SDK: a guardrail must be attached to each tool. In both, hosted web search can only be allowed or withheld, not gated per call. |
| Glue code needed | 20% | **5** (87 lines) | 4 (94 lines) | Close |
| Visibility | 20% | **5** | 3 | Pydantic AI: `wrap_model_request` sees every model call, including hosted search calls. Agents SDK: run hooks miss hosted tools, so the spike subclasses the model class, which depends on library internals. |
| Overhead | 15% | 4 | 4 | Analyst requests are nearly identical (Pydantic AI 809 vs 893 input tokens on the first call). Request bodies match field for field. |
| Fit with typed deliverables | 20% | 4 | **5** | Both take Pydantic models. Pydantic AI defaults to tool-based output, which Meta rejects, so it needs `NativeOutput(strict=True)` and `supports_forced_tool_choice=False`. The Agents SDK's default already matches Meta's rules. |
| **Weighted score** | | **4.65** | 4.00 | |

## Live runs

Each run executes the Analyst and the Strategist in parallel. Costs are estimated from token counts at list prices.

| Run | Library | Requests | Input tokens | Output tokens | Searches | Wall time | Est. cost |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Pydantic AI | 4 | 116,481 | 5,581 | 4 | 17.2 s | $0.18 |
| 1 | Agents SDK | 4 | 26,988 | 3,511 | 3 | 8.6 s | $0.06 |
| 2 | Pydantic AI | 4 | 128,773 | 5,195 | 7 | 23.6 s | $0.20 |
| 2 | Agents SDK | 4 | 44,556 | 5,452 | 3 | 12.4 s | $0.09 |

Total spend for both runs: about $0.52.

**Why the Strategist numbers differ.** Almost all tokens come from the Strategist's single web-search request (Pydantic AI 113k–125k input tokens vs 23k–40k for the Agents SDK), because search results count as input. The two libraries send the same request: identical instructions, input, `tool_choice`, and `web_search` tool with `search_context_size: "medium"`. Only the output schema's name and layout differ. The gap comes from how many searches the model chose to run, not from library overhead, though 2 samples are too few to rule out an effect of the schema layout. Either way, PGT must cap search cost itself (see below).

## Decision

**Use Pydantic AI** for the per-role agent loop. It gives the harness one place to enforce policy and one place to observe every model and tool call, which is what PGT's policy gate and event log need. The OpenAI Agents SDK stays the fallback.

Follow-ups for the harness:
- Always use `NativeOutput(strict=True)` with `OpenAIModelProfile(supports_forced_tool_choice=False)`; add a unit test that fails if a request sends `tool_choice` other than `"auto"`.
- Cap Strategist search cost: limit searches per run in the instructions, set `search_context_size: "low"` unless a task needs more, and enforce a per-role cost cap in the policy gate.
- Set `PYDANTIC_AI_NO_BANNER=1` and keep instrumentation off; nothing is exported.
- Recheck the Strategist token gap with the harness's own schema layout once deliverable schemas are final.
